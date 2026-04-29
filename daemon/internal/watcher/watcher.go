// Package watcher polls the Claude Code transcripts root and emits
// completed turns. Per spec §4.4.
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
//   - On startup, snapshot every existing jsonl's max turn_index as a
//     "high-water mark" — but emit nothing. Per spec §1, no backfill.
//   - Each Interval, walk root/<slug>/<uuid>.jsonl. For files we've
//     never seen, treat them as new (high-water = -1) and emit any
//     closed turns that appear. For files whose (size, mtime) changed
//     since last poll, re-parse and emit only turns whose index is
//     strictly greater than the high-water mark.
//   - The watcher does NOT consult the local store. Upstream code
//     dedupes via store.IsUploaded; the watcher is purely a
//     "this-file-changed → these-are-the-new-completed-turns" function.

package watcher

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/adapter/claudecode"
)

// DefaultInterval is how often the watcher rescans. Tuned for "feels
// real-time" in the public timeline demo without burning CPU. Tests
// pass a smaller value via NewWithInterval.
const DefaultInterval = 2 * time.Second

// ClaudeCodeRoot returns ~/.claude/projects/.
func ClaudeCodeRoot() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude", "projects"), nil
}

// Watcher emits adapter.Turn values as the daemon learns about them.
//
// Lifecycle: New → Run(ctx) (blocks until ctx done). The Turns channel
// is closed when Run returns.
type Watcher struct {
	root     string
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

// New creates a Watcher rooted at the Claude Code transcripts dir with
// the production polling interval. Pass an empty string for root to
// use ~/.claude/projects.
func New(root string) (*Watcher, error) {
	return NewWithInterval(root, DefaultInterval)
}

// NewWithInterval is like New but lets tests use a smaller interval.
// Also used by anyone who wants to tune the latency/CPU tradeoff.
func NewWithInterval(root string, interval time.Duration) (*Watcher, error) {
	if root == "" {
		r, err := ClaudeCodeRoot()
		if err != nil {
			return nil, err
		}
		root = r
	}
	return &Watcher{
		root:     root,
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

// Run blocks until ctx is canceled.
func (w *Watcher) Run(ctx context.Context) error {
	defer close(w.turns)

	if err := os.MkdirAll(w.root, 0o700); err != nil {
		return fmt.Errorf("ensure %s: %w", w.root, err)
	}

	// Per spec §1 ("only turns produced after the daemon starts running
	// are captured"): seed each existing jsonl's high-water mark to
	// its current max turn_index, so historical turns are NOT
	// backfilled. New turns appended after startup will exceed the
	// mark and be emitted.
	w.seedHighWaterMarks()

	// Run one tick immediately, then every interval. The first tick
	// catches jsonl files that appeared between seeding and now.
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

// seedHighWaterMarks parses every existing jsonl ONCE at startup,
// records the highest turn_index seen plus current size/mtime, but
// does NOT emit any turn. This anchors w.state[path] so future polls
// skip pre-existing history.
func (w *Watcher) seedHighWaterMarks() {
	matches, err := filepath.Glob(filepath.Join(w.root, "*", "*.jsonl"))
	if err != nil {
		w.tryReportErr(err)
		return
	}
	for _, p := range matches {
		st, err := w.snapshot(p)
		if err != nil {
			w.tryReportErr(fmt.Errorf("seed stat %s: %w", p, err))
			continue
		}
		turns, err := claudecode.ParseFile(p)
		if err != nil {
			w.tryReportErr(fmt.Errorf("seed parse %s: %w", p, err))
			st.highWater = -1 // be conservative; emit nothing for this file until it changes
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
	matches, err := filepath.Glob(filepath.Join(w.root, "*", "*.jsonl"))
	if err != nil {
		w.tryReportErr(fmt.Errorf("glob: %w", err))
		return
	}
	for _, p := range matches {
		cur, err := w.snapshot(p)
		if err != nil {
			// File could have been deleted between glob and stat;
			// non-fatal.
			continue
		}
		w.mu.Lock()
		prev, known := w.state[p]
		w.mu.Unlock()

		if known && cur.size == prev.size && cur.mtime.Equal(prev.mtime) {
			continue // unchanged
		}
		// Either new file (use -1 high-water → emit all) or changed
		// file (use stored high-water).
		high := -1
		if known {
			high = prev.highWater
		}
		newHigh := w.parseAndEmitFrom(p, high)

		// Update state with current size/mtime and (possibly bumped)
		// high-water mark.
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
// high. Returns the new high-water mark (max(high, last emitted index)).
func (w *Watcher) parseAndEmitFrom(path string, high int) int {
	turns, err := claudecode.ParseFile(path)
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
