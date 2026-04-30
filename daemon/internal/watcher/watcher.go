// Package watcher polls a transcripts directory and emits completed
// turns through an injected parser. Per spec §4.4 + §4.6.
//
// Why polling instead of fsnotify?
//
//   We tried fsnotify first. On macOS (kqueue), Claude Code's jsonl
//   write pattern reliably fails to deliver WRITE events to subscribed
//   watchers — verified empirically: a daemon running for several
//   minutes saw zero events while the active session's jsonl grew by
//   four full turns. os.Stat sees mtime/size changes immediately,
//   because they're inode metadata and don't depend on event-coalescing
//   policy in the kernel.
//
//   Trade-off accepted: a fixed 2s polling cadence (vs. fsnotify's
//   sub-millisecond ideal) in exchange for "we will never miss a turn
//   because the OS swallowed the event." For a public-timeline demo,
//   2s end-to-end latency is invisible; data correctness is not.
//
// Design:
//
//   - The Watcher is parser-agnostic: callers pass a Parser function
//     mapping a jsonl path to a slice of completed adapter.Turn. This
//     lets one Watcher serve any agent (Claude Code, Codex, ...). Run
//     two Watchers in parallel for two agents — they share nothing.
//   - On startup, snapshot every existing jsonl's max turn_index as a
//     "high-water mark" — but emit nothing. Per spec §1, no backfill.
//   - Each Interval, walk root recursively for *.jsonl. For files
//     we've never seen, treat them as new (high-water = -1). For
//     files whose (size, mtime) changed since last poll, re-parse
//     and emit only turns whose index strictly exceeds the high-water
//     mark.
//   - The watcher does NOT consult the local store. Upstream code
//     dedupes via store.IsUploaded; the watcher is purely a
//     "this-file-changed → these-are-the-new-completed-turns" function.

package watcher

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
)

// DefaultInterval is how often the watcher rescans. Tuned for "feels
// real-time" in the public timeline demo without burning CPU. Tests
// pass a smaller value via NewWithInterval.
const DefaultInterval = 2 * time.Second

// Parser is the function signature each adapter exports. It takes a
// path to a complete jsonl file and returns every closed turn it
// contains, in order.
//
// claudecode.ParseFile and codex.ParseFile both satisfy this signature.
type Parser func(path string) ([]adapter.Turn, error)

// ClaudeCodeRoot returns ~/.claude/projects/.
func ClaudeCodeRoot() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude", "projects"), nil
}

// CodexRoot returns ~/.codex/sessions/.
func CodexRoot() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".codex", "sessions"), nil
}

// Watcher emits adapter.Turn values as the daemon learns about them.
type Watcher struct {
	name     string // human-readable label for logs (e.g. "claude_code")
	root     string
	parse    Parser
	interval time.Duration

	mu sync.Mutex
	// Per-file state we carry across polls.
	state map[string]fileState

	turns chan adapter.Turn
	errs  chan error
}

type fileState struct {
	highWater int   // last emitted turn_index; -1 means none yet
	size      int64 // last seen size, used to skip unchanged files
	mtime     time.Time
}

// New creates a Watcher with the production polling interval.
func New(name, root string, parse Parser) (*Watcher, error) {
	return NewWithInterval(name, root, parse, DefaultInterval)
}

// NewWithInterval lets tests use a smaller interval.
func NewWithInterval(name, root string, parse Parser, interval time.Duration) (*Watcher, error) {
	if root == "" {
		return nil, fmt.Errorf("watcher %q: empty root", name)
	}
	if parse == nil {
		return nil, fmt.Errorf("watcher %q: nil parser", name)
	}
	return &Watcher{
		name:     name,
		root:     root,
		parse:    parse,
		interval: interval,
		state:    make(map[string]fileState),
		turns:    make(chan adapter.Turn, 1024),
		errs:     make(chan error, 64),
	}, nil
}

// Turns returns the channel completed turns are pushed to.
func (w *Watcher) Turns() <-chan adapter.Turn { return w.turns }

// Errors returns non-fatal errors (parse failures, transient IO). Drain
// it or you'll block; the watcher does not block on send.
func (w *Watcher) Errors() <-chan error { return w.errs }

// Name returns the human-readable label, useful when multiplexing
// multiple watchers' logs.
func (w *Watcher) Name() string { return w.name }

// Run blocks until ctx is canceled.
func (w *Watcher) Run(ctx context.Context) error {
	defer close(w.turns)

	if err := os.MkdirAll(w.root, 0o700); err != nil {
		return fmt.Errorf("watcher %q ensure %s: %w", w.name, w.root, err)
	}

	w.seedHighWaterMarks()

	tick := time.NewTimer(0)
	defer tick.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-tick.C:
			w.poll()
			tick.Reset(w.interval)
		}
	}
}

// findJSONL walks root recursively and returns every *.jsonl. Codex
// stores transcripts at root/<year>/<month>/<day>/foo.jsonl, while
// Claude Code uses root/<slug>/foo.jsonl — a single recursive walk
// handles both.
func (w *Watcher) findJSONL() []string {
	var out []string
	_ = filepath.WalkDir(w.root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			// Permission errors on a sub-tree shouldn't kill the walk.
			return nil
		}
		if d.IsDir() {
			return nil
		}
		if strings.HasSuffix(path, ".jsonl") {
			out = append(out, path)
		}
		return nil
	})
	return out
}

// seedHighWaterMarks parses every existing jsonl ONCE at startup,
// records the highest turn_index seen plus current size/mtime, but
// does NOT emit any turn.
func (w *Watcher) seedHighWaterMarks() {
	for _, p := range w.findJSONL() {
		st, err := w.snapshot(p)
		if err != nil {
			w.tryReportErr(fmt.Errorf("seed stat %s: %w", p, err))
			continue
		}
		turns, err := w.parse(p)
		if err != nil {
			w.tryReportErr(fmt.Errorf("seed parse %s: %w", p, err))
			st.highWater = -1
		} else if len(turns) > 0 {
			st.highWater = turns[len(turns)-1].TurnIndex
		} else {
			st.highWater = -1
		}
		w.mu.Lock()
		w.state[p] = st
		w.mu.Unlock()
	}
}

// poll scans the transcripts root once. For each jsonl whose size or
// mtime has changed (or which we've never seen), parse and emit new
// turns.
func (w *Watcher) poll() {
	for _, p := range w.findJSONL() {
		cur, err := w.snapshot(p)
		if err != nil {
			// File could have been deleted between walk and stat.
			continue
		}
		w.mu.Lock()
		prev, known := w.state[p]
		w.mu.Unlock()

		if known && cur.size == prev.size && cur.mtime.Equal(prev.mtime) {
			continue // unchanged
		}
		high := -1
		if known {
			high = prev.highWater
		}
		newHigh := w.parseAndEmitFrom(p, high)

		w.mu.Lock()
		w.state[p] = fileState{
			highWater: newHigh,
			size:      cur.size,
			mtime:     cur.mtime,
		}
		w.mu.Unlock()
	}
}

// parseAndEmitFrom re-parses path and pushes any turn whose index >
// high. Returns the new high-water mark.
func (w *Watcher) parseAndEmitFrom(path string, high int) int {
	turns, err := w.parse(path)
	if err != nil {
		w.tryReportErr(fmt.Errorf("parse %s: %w", path, err))
		return high
	}
	for _, t := range turns {
		if t.TurnIndex <= high {
			continue
		}
		select {
		case w.turns <- t:
			high = t.TurnIndex
		default:
			w.tryReportErr(fmt.Errorf("turn buffer full; dropped turn %d of %s", t.TurnIndex, path))
			// Don't bump high-water if we couldn't deliver: next poll
			// will retry this turn.
		}
	}
	return high
}

func (w *Watcher) snapshot(path string) (fileState, error) {
	info, err := os.Stat(path)
	if err != nil {
		return fileState{}, err
	}
	return fileState{
		size:  info.Size(),
		mtime: info.ModTime(),
	}, nil
}

func (w *Watcher) tryReportErr(err error) {
	select {
	case w.errs <- err:
	default:
	}
}
