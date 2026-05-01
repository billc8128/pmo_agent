// Package rawtranscript uploads raw agent JSONL session files as gzip
// snapshots for debugging and future search.
package rawtranscript

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/projectroot"
)

const (
	AgentClaudeCode = "claude_code"
	AgentCodex      = "codex"

	metadataHeader = "X-PMO-Transcript-Metadata"
)

// Snapshot is one complete raw JSONL file compressed for upload.
type Snapshot struct {
	Agent          string
	AgentSessionID string
	ProjectPath    string
	ProjectRoot    string
	LocalPath      string
	ByteSize       int64
	CompressedSize int64
	LineCount      int
	SHA256         string
	LastMTime      time.Time
	GzipBytes      []byte
}

// UploadMetadata is serialized into a request header while the gzip
// payload stays as the HTTP body.
type UploadMetadata struct {
	Agent          string `json:"agent"`
	AgentSessionID string `json:"agent_session_id"`
	ProjectPath    string `json:"project_path,omitempty"`
	ProjectRoot    string `json:"project_root,omitempty"`
	LocalPath      string `json:"local_path,omitempty"`
	ByteSize       int64  `json:"byte_size"`
	CompressedSize int64  `json:"compressed_size"`
	LineCount      int    `json:"line_count"`
	SHA256         string `json:"sha256"`
	LastMTime      string `json:"last_mtime,omitempty"`
}

// BuildSnapshot reads path, infers its session metadata, gzips the raw
// JSONL bytes, and hashes the uncompressed content.
func BuildSnapshot(agent, path string) (Snapshot, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Snapshot{}, fmt.Errorf("read transcript %s: %w", path, err)
	}
	info, err := os.Stat(path)
	if err != nil {
		return Snapshot{}, fmt.Errorf("stat transcript %s: %w", path, err)
	}
	sessionID, cwd := inferMetadata(agent, path, b)
	if sessionID == "" {
		return Snapshot{}, fmt.Errorf("infer session id for %s", path)
	}
	gz, err := gzipPayload(b)
	if err != nil {
		return Snapshot{}, err
	}
	sum := sha256.Sum256(b)
	root := ""
	if cwd != "" {
		root = projectroot.Resolve(cwd)
	}
	return Snapshot{
		Agent:          agent,
		AgentSessionID: sessionID,
		ProjectPath:    cwd,
		ProjectRoot:    root,
		LocalPath:      path,
		ByteSize:       int64(len(b)),
		CompressedSize: int64(len(gz)),
		LineCount:      countJSONLLines(b),
		SHA256:         hex.EncodeToString(sum[:]),
		LastMTime:      info.ModTime(),
		GzipBytes:      gz,
	}, nil
}

func gzipPayload(b []byte) ([]byte, error) {
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(b); err != nil {
		_ = zw.Close()
		return nil, fmt.Errorf("gzip transcript: %w", err)
	}
	if err := zw.Close(); err != nil {
		return nil, fmt.Errorf("gzip transcript: %w", err)
	}
	return buf.Bytes(), nil
}

func countJSONLLines(b []byte) int {
	if len(b) == 0 {
		return 0
	}
	n := bytes.Count(b, []byte{'\n'})
	if b[len(b)-1] != '\n' {
		n++
	}
	return n
}

// JSONLFiles returns all transcript files under root.
func JSONLFiles(root string) []string {
	var out []string
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".jsonl") {
			return nil
		}
		out = append(out, path)
		return nil
	})
	sort.Strings(out)
	return out
}

// ReadyJSONLFiles returns transcript files whose mtime has been quiet
// for at least quietFor. This prevents uploading a full file on every
// small append while the agent is actively writing the session.
func ReadyJSONLFiles(root string, quietFor time.Duration, now time.Time) []string {
	var out []string
	for _, path := range JSONLFiles(root) {
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		if now.Sub(info.ModTime()) >= quietFor {
			out = append(out, path)
		}
	}
	return out
}

func inferMetadata(agent, path string, b []byte) (sessionID, cwd string) {
	switch agent {
	case AgentClaudeCode:
		sessionID = claudeSessionIDFromFilename(path)
		for _, line := range bytes.Split(b, []byte{'\n'}) {
			if len(bytes.TrimSpace(line)) == 0 {
				continue
			}
			var row struct {
				SessionID string `json:"sessionId"`
				CWD       string `json:"cwd"`
			}
			if json.Unmarshal(line, &row) != nil {
				continue
			}
			if row.SessionID != "" {
				sessionID = row.SessionID
			}
			if row.CWD != "" {
				cwd = row.CWD
			}
			if sessionID != "" && cwd != "" {
				break
			}
		}
	case AgentCodex:
		sessionID = codexSessionIDFromFilename(path)
		for _, line := range bytes.Split(b, []byte{'\n'}) {
			if len(bytes.TrimSpace(line)) == 0 {
				continue
			}
			var row struct {
				Type    string          `json:"type"`
				Payload json.RawMessage `json:"payload"`
			}
			if json.Unmarshal(line, &row) != nil || row.Type != "session_meta" {
				continue
			}
			var meta struct {
				ID  string `json:"id"`
				CWD string `json:"cwd"`
			}
			if json.Unmarshal(row.Payload, &meta) != nil {
				continue
			}
			if meta.ID != "" {
				sessionID = meta.ID
			}
			if meta.CWD != "" {
				cwd = meta.CWD
			}
			break
		}
	}
	return sessionID, cwd
}

func claudeSessionIDFromFilename(path string) string {
	base := filepath.Base(path)
	return strings.TrimSuffix(base, ".jsonl")
}

func codexSessionIDFromFilename(path string) string {
	base := filepath.Base(path)
	base = strings.TrimSuffix(base, ".jsonl")
	parts := strings.Split(base, "-")
	if len(parts) < 5 {
		return base
	}
	return strings.Join(parts[len(parts)-5:], "-")
}

// Client posts raw transcript snapshots to the backend.
type Client struct {
	serverURL string
	token     string
	hc        *http.Client
}

func NewClient(serverURL, token string) *Client {
	return &Client{
		serverURL: strings.TrimRight(serverURL, "/"),
		token:     token,
		hc:        &http.Client{Timeout: 60 * time.Second},
	}
}

type Result struct {
	OK          bool   `json:"ok"`
	StoragePath string `json:"storage_path"`
	Error       string `json:"error,omitempty"`
}

func (c *Client) Upload(ctx context.Context, snap Snapshot) (*Result, error) {
	meta := UploadMetadata{
		Agent:          snap.Agent,
		AgentSessionID: snap.AgentSessionID,
		ProjectPath:    snap.ProjectPath,
		ProjectRoot:    snap.ProjectRoot,
		LocalPath:      snap.LocalPath,
		ByteSize:       snap.ByteSize,
		CompressedSize: snap.CompressedSize,
		LineCount:      snap.LineCount,
		SHA256:         snap.SHA256,
	}
	if !snap.LastMTime.IsZero() {
		meta.LastMTime = snap.LastMTime.UTC().Format(time.RFC3339Nano)
	}
	metaJSON, err := json.Marshal(meta)
	if err != nil {
		return nil, fmt.Errorf("marshal transcript metadata: %w", err)
	}
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.serverURL+"/functions/v1/upload_transcript",
		bytes.NewReader(snap.GzipBytes),
	)
	if err != nil {
		return nil, fmt.Errorf("build transcript upload request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/gzip")
	req.Header.Set(metadataHeader, base64.StdEncoding.EncodeToString(metaJSON))

	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upload transcript: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("upload transcript: server %d: %s", resp.StatusCode, snippet(body))
	}
	var out Result
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("decode transcript upload response: %w (body=%s)", err, snippet(body))
	}
	if !out.OK {
		return nil, fmt.Errorf("upload transcript: %s", out.Error)
	}
	return &out, nil
}

func snippet(b []byte) string {
	const max = 200
	if len(b) > max {
		return string(b[:max]) + "..."
	}
	return string(b)
}
