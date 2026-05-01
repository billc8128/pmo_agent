// Package store wraps the daemon's local SQLite database
// (~/.pmo-agent/state.db).
//
// Responsibilities:
//   - Track which turns have already been uploaded, keyed by
//     (agent, session_id, turn_index). This is the same key the server
//     dedupes on, so client-side dedup avoids needless POSTs.
//   - Track raw JSONL transcript snapshots that have already been uploaded.

package store

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite" // registers the "sqlite" driver
)

// DefaultPath is the canonical state.db location: $HOME/.pmo-agent/state.db.
// Unit tests use OpenAt to point at a temp dir.
func DefaultPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("locate home: %w", err)
	}
	dir := filepath.Join(home, ".pmo-agent")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create %s: %w", dir, err)
	}
	return filepath.Join(dir, "state.db"), nil
}

// Store is a thin wrapper around *sql.DB. It owns the connection
// lifecycle and hides SQL from callers.
type Store struct {
	db *sql.DB
}

// Open opens the default state.db, creating and migrating it if needed.
func Open() (*Store, error) {
	p, err := DefaultPath()
	if err != nil {
		return nil, err
	}
	return OpenAt(p)
}

// OpenAt opens a state.db at an explicit path. Used by tests.
func OpenAt(path string) (*Store, error) {
	// _journal=WAL: durable across crashes without per-write fsync penalty.
	// _busy_timeout=5000: avoid spurious "database is locked" if a long
	// upload coincides with a status read.
	dsn := "file:" + path + "?_journal=WAL&_busy_timeout=5000"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// modernc/sqlite is goroutine-safe per *sql.DB but a single
	// connection serializes writes; one is enough for daemon throughput.
	db.SetMaxOpenConns(1)

	s := &Store{db: db}
	if err := s.migrate(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return s, nil
}

// Close releases the database handle.
func (s *Store) Close() error { return s.db.Close() }

// migrate is forward-only: add new schema versions by appending stmts.
// We use PRAGMA user_version to track the current version cheaply.
func (s *Store) migrate() error {
	var ver int
	if err := s.db.QueryRow("PRAGMA user_version").Scan(&ver); err != nil {
		return err
	}
	steps := []string{
		// version 1: uploaded_turns
		`CREATE TABLE IF NOT EXISTS uploaded_turns (
			agent            TEXT NOT NULL,
			session_id       TEXT NOT NULL,
			turn_index       INTEGER NOT NULL,
			server_turn_id   INTEGER,           -- nullable: server may dedupe and return null
			uploaded_at      TEXT NOT NULL,     -- RFC3339
			PRIMARY KEY (agent, session_id, turn_index)
		);`,
		// version 2: raw JSONL transcript upload state
		`CREATE TABLE IF NOT EXISTS uploaded_transcripts (
			agent             TEXT NOT NULL,
			session_id        TEXT NOT NULL,
			local_path        TEXT NOT NULL,
			sha256            TEXT NOT NULL,
			byte_size         INTEGER NOT NULL,
			compressed_size   INTEGER NOT NULL,
			local_mtime       TEXT NOT NULL,
			uploaded_at       TEXT NOT NULL,
			PRIMARY KEY (agent, session_id)
		);
		CREATE INDEX IF NOT EXISTS uploaded_transcripts_path
			ON uploaded_transcripts (agent, local_path);`,
		// version 3: local raw JSONL file state. This records files seen at
		// daemon startup too, so old historical files are not uploaded by default.
		`CREATE TABLE IF NOT EXISTS raw_transcript_files (
			agent             TEXT NOT NULL,
			local_path        TEXT NOT NULL,
			byte_size         INTEGER NOT NULL,
			local_mtime       TEXT NOT NULL,
			seen_at           TEXT NOT NULL,
			PRIMARY KEY (agent, local_path)
		);
		INSERT OR REPLACE INTO raw_transcript_files
		    (agent, local_path, byte_size, local_mtime, seen_at)
		SELECT agent, local_path, byte_size, local_mtime, uploaded_at
		FROM uploaded_transcripts;`,
	}
	for i := ver; i < len(steps); i++ {
		if _, err := s.db.Exec(steps[i]); err != nil {
			return fmt.Errorf("migration step %d: %w", i+1, err)
		}
		if _, err := s.db.Exec(fmt.Sprintf("PRAGMA user_version = %d", i+1)); err != nil {
			return fmt.Errorf("bump user_version to %d: %w", i+1, err)
		}
	}
	return nil
}

type TranscriptFileState struct {
	ByteSize   int64
	LocalMTime string
}

// TranscriptPathState returns the last raw transcript state recorded for
// a local file path. It lets the daemon skip unchanged historical files.
func (s *Store) TranscriptPathState(agent, localPath string) (TranscriptFileState, bool, error) {
	const q = `
		SELECT byte_size, local_mtime
		FROM raw_transcript_files
		WHERE agent = ? AND local_path = ?
		LIMIT 1
	`
	var state TranscriptFileState
	err := s.db.QueryRow(q, agent, localPath).Scan(&state.ByteSize, &state.LocalMTime)
	if errors.Is(err, sql.ErrNoRows) {
		return TranscriptFileState{}, false, nil
	}
	if err != nil {
		return TranscriptFileState{}, false, fmt.Errorf("query raw_transcript_files: %w", err)
	}
	return state, true, nil
}

// MarkRawTranscriptFileSeen records the current local file state without
// implying the raw transcript was uploaded to the server.
func (s *Store) MarkRawTranscriptFileSeen(agent, localPath string, byteSize int64, localMTime time.Time) error {
	const q = `
		INSERT INTO raw_transcript_files
		    (agent, local_path, byte_size, local_mtime, seen_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(agent, local_path) DO UPDATE
		SET byte_size   = excluded.byte_size,
		    local_mtime = excluded.local_mtime,
		    seen_at     = excluded.seen_at
	`
	_, err := s.db.Exec(
		q,
		agent,
		localPath,
		byteSize,
		localMTime.UTC().Format(time.RFC3339Nano),
		time.Now().UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		return fmt.Errorf("insert raw_transcript_files: %w", err)
	}
	return nil
}

// TranscriptSHA returns the last successfully uploaded raw transcript
// hash for an agent session.
func (s *Store) TranscriptSHA(agent, sessionID string) (string, bool, error) {
	const q = `SELECT sha256 FROM uploaded_transcripts WHERE agent = ? AND session_id = ?`
	var sha string
	err := s.db.QueryRow(q, agent, sessionID).Scan(&sha)
	if errors.Is(err, sql.ErrNoRows) {
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("query uploaded_transcripts: %w", err)
	}
	return sha, true, nil
}

// MarkTranscriptUploaded records the content hash of the latest raw JSONL
// snapshot accepted by the server.
func (s *Store) MarkTranscriptUploaded(
	agent,
	sessionID,
	localPath,
	sha string,
	byteSize,
	compressedSize int64,
	localMTime time.Time,
) error {
	const q = `
		INSERT INTO uploaded_transcripts
		    (agent, session_id, local_path, sha256, byte_size, compressed_size, local_mtime, uploaded_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(agent, session_id) DO UPDATE
		SET local_path      = excluded.local_path,
		    sha256          = excluded.sha256,
		    byte_size       = excluded.byte_size,
		    compressed_size = excluded.compressed_size,
		    local_mtime     = excluded.local_mtime,
		    uploaded_at     = excluded.uploaded_at
	`
	_, err := s.db.Exec(
		q,
		agent,
		sessionID,
		localPath,
		sha,
		byteSize,
		compressedSize,
		localMTime.UTC().Format(time.RFC3339Nano),
		time.Now().UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		return fmt.Errorf("insert uploaded_transcripts: %w", err)
	}
	return s.MarkRawTranscriptFileSeen(agent, localPath, byteSize, localMTime)
}

// IsUploaded returns true if a turn with this triple is already in the
// local "done" table.
func (s *Store) IsUploaded(agent, sessionID string, turnIndex int) (bool, error) {
	const q = `SELECT 1 FROM uploaded_turns WHERE agent = ? AND session_id = ? AND turn_index = ?`
	var v int
	err := s.db.QueryRow(q, agent, sessionID, turnIndex).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("query uploaded_turns: %w", err)
	}
	return true, nil
}

// MarkUploaded records that a turn has been accepted by the server.
// serverTurnID is null when the server deduped the row.
func (s *Store) MarkUploaded(agent, sessionID string, turnIndex int, serverTurnID *int64) error {
	const q = `
		INSERT INTO uploaded_turns (agent, session_id, turn_index, server_turn_id, uploaded_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(agent, session_id, turn_index) DO UPDATE
		SET server_turn_id = excluded.server_turn_id,
		    uploaded_at    = excluded.uploaded_at
	`
	// RFC3339Nano (not RFC3339) so rapid back-to-back uploads sort
	// reliably: text-sorted ISO-8601 with nanos is monotonic.
	_, err := s.db.Exec(q, agent, sessionID, turnIndex, serverTurnID, time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return fmt.Errorf("insert uploaded_turns: %w", err)
	}
	return nil
}

// RecentUploads returns the most recent N uploaded turns, newest first.
// Used by `pmo-agent status` (Milestone 1.6).
type Upload struct {
	Agent        string
	SessionID    string
	TurnIndex    int
	ServerTurnID *int64
	UploadedAt   time.Time
}

func (s *Store) RecentUploads(limit int) ([]Upload, error) {
	const q = `
		SELECT agent, session_id, turn_index, server_turn_id, uploaded_at
		FROM uploaded_turns
		ORDER BY uploaded_at DESC
		LIMIT ?
	`
	rows, err := s.db.Query(q, limit)
	if err != nil {
		return nil, fmt.Errorf("query recent: %w", err)
	}
	defer rows.Close()
	var out []Upload
	for rows.Next() {
		var u Upload
		var ts string
		if err := rows.Scan(&u.Agent, &u.SessionID, &u.TurnIndex, &u.ServerTurnID, &ts); err != nil {
			return nil, err
		}
		u.UploadedAt, _ = time.Parse(time.RFC3339Nano, ts)
		out = append(out, u)
	}
	return out, rows.Err()
}
