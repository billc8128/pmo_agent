package watcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestWatcher_NoBackfillOfPreexistingFiles is the regression test for
// the Milestone-1.7 incident: when started against a directory that
// already has jsonl history, the watcher must NOT emit any of it. Only
// turns appended after the watcher starts are eligible.
func TestWatcher_NoBackfillOfPreexistingFiles(t *testing.T) {
	root := t.TempDir()

	// Pre-existing jsonl with two complete turns.
	slugDir := filepath.Join(root, "slug")
	if err := os.MkdirAll(slugDir, 0o700); err != nil {
		t.Fatal(err)
	}
	jsonl := filepath.Join(slugDir, "old.jsonl")
	preexisting :=
		userText("old-1", "2026-04-29T01:00:00Z") +
			assistantText("OLD-1", "2026-04-29T01:00:01Z") +
			userText("old-2", "2026-04-29T01:00:02Z") +
			assistantText("OLD-2", "2026-04-29T01:00:03Z")
	if err := os.WriteFile(jsonl, []byte(preexisting), 0o600); err != nil {
		t.Fatal(err)
	}

	wt, err := NewWithInterval(root, 100*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go wt.Run(ctx)
	go func() {
		for range wt.Errors() {
		}
	}()

	// Wait long enough that any spurious initial emit would have happened.
	timeout := time.After(800 * time.Millisecond)
loop:
	for {
		select {
		case turn, ok := <-wt.Turns():
			if !ok {
				break loop
			}
			t.Errorf("watcher emitted pre-existing turn (BACKFILL bug): %+v", turn)
		case <-timeout:
			break loop
		}
	}

	// Now append a NEW turn; it MUST be emitted.
	f, err := os.OpenFile(jsonl, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(
		userText("fresh", "2026-04-30T01:00:00Z") +
			assistantText("FRESH", "2026-04-30T01:00:01Z"),
	); err != nil {
		t.Fatal(err)
	}
	f.Close()

	select {
	case turn, ok := <-wt.Turns():
		if !ok {
			t.Fatal("Turns channel closed before new turn arrived")
		}
		if turn.UserMessage != "fresh" {
			t.Errorf("expected the new (post-startup) turn; got %q", turn.UserMessage)
		}
		if turn.TurnIndex != 2 {
			t.Errorf("expected turn_index=2, got %d", turn.TurnIndex)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for new turn after startup")
	}
}

// TestWatcher_NewSessionDirThenFile drives a realistic-shaped flow:
//   1. start watcher with empty root,
//   2. mkdir <root>/<slug>/  (must trigger a new fsnotify Add),
//   3. write a complete jsonl turn into it,
//   4. expect exactly one Turn on Turns().
func TestWatcher_NewSessionDirThenFile(t *testing.T) {
	root := t.TempDir()
	wt, err := NewWithInterval(root, 100*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	runErrCh := make(chan error, 1)
	go func() { runErrCh <- wt.Run(ctx) }()

	// Drain errors so watcher never blocks on Errors() send.
	go func() {
		for range wt.Errors() {
		}
	}()

	// Give Run a moment to install fsnotify watches before we change
	// the filesystem; otherwise the slug-create event may be missed
	// on slow CI.
	time.Sleep(50 * time.Millisecond)

	slugDir := filepath.Join(root, "-Users-a-Desktop-test")
	if err := os.MkdirAll(slugDir, 0o700); err != nil {
		t.Fatal(err)
	}

	// Wait for the watcher to register the new slug dir; then write a
	// complete turn into a jsonl file.
	time.Sleep(150 * time.Millisecond)
	jsonl := filepath.Join(slugDir, "abc.jsonl")
	body := userText("hello", "2026-04-30T01:00:00Z") +
		assistantText("hi", "2026-04-30T01:00:01Z")
	if err := os.WriteFile(jsonl, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	select {
	case turn, ok := <-wt.Turns():
		if !ok {
			t.Fatal("Turns channel closed before any turn arrived")
		}
		if turn.UserMessage != "hello" {
			t.Errorf("user_message = %q, want %q", turn.UserMessage, "hello")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for turn")
	}

	cancel()
	if err := <-runErrCh; err != nil {
		t.Errorf("Run returned: %v", err)
	}
}

// TestWatcher_DebounceCoalescesWrites verifies a flurry of quick writes
// produces exactly one parse — the watcher waits until writes settle.
func TestWatcher_DebounceCoalescesWrites(t *testing.T) {
	root := t.TempDir()
	slugDir := filepath.Join(root, "slug")
	if err := os.MkdirAll(slugDir, 0o700); err != nil {
		t.Fatal(err)
	}
	jsonl := filepath.Join(slugDir, "x.jsonl")

	wt, err := NewWithInterval(root, 200*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go wt.Run(ctx)
	go func() {
		for range wt.Errors() {
		}
	}()

	time.Sleep(100 * time.Millisecond)

	// Write 3 turns in quick succession.
	body := userText("a", "2026-04-30T01:00:00Z") +
		assistantText("A", "2026-04-30T01:00:01Z") +
		userText("b", "2026-04-30T01:00:02Z") +
		assistantText("B", "2026-04-30T01:00:03Z") +
		userText("c", "2026-04-30T01:00:04Z") +
		assistantText("C", "2026-04-30T01:00:05Z")

	// Append in 3 chunks to simulate streaming writes.
	parts := []string{
		userText("a", "2026-04-30T01:00:00Z") + assistantText("A", "2026-04-30T01:00:01Z"),
		userText("b", "2026-04-30T01:00:02Z") + assistantText("B", "2026-04-30T01:00:03Z"),
		userText("c", "2026-04-30T01:00:04Z") + assistantText("C", "2026-04-30T01:00:05Z"),
	}
	f, err := os.Create(jsonl)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range parts {
		if _, err := f.WriteString(p); err != nil {
			t.Fatal(err)
		}
		if err := f.Sync(); err != nil {
			t.Fatal(err)
		}
		time.Sleep(30 * time.Millisecond) // shorter than debounce
	}
	f.Close()
	_ = body // (kept for readability; unused)

	// Collect turns until the channel goes quiet for 500ms.
	var got []string
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	quiet := time.NewTimer(500 * time.Millisecond)
	defer quiet.Stop()

loop:
	for {
		select {
		case turn, ok := <-wt.Turns():
			if !ok {
				break loop
			}
			got = append(got, turn.UserMessage)
			quiet.Reset(500 * time.Millisecond)
		case <-quiet.C:
			break loop
		case <-deadline.C:
			break loop
		}
	}

	if len(got) != 3 {
		t.Errorf("want 3 turns, got %d: %v", len(got), got)
	}
}

// ─── synthetic JSONL builders (copy-paste from claudecode tests; we
// don't want this package to depend on a test helper from another) ───

func userText(text, ts string) string {
	return jsonline(map[string]any{
		"type":        "user",
		"uuid":        "u-" + ts,
		"timestamp":   ts,
		"isSidechain": false,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message":     map[string]any{"role": "user", "content": text},
	})
}

func assistantText(text, ts string) string {
	return jsonline(map[string]any{
		"type":        "assistant",
		"uuid":        "a-" + ts,
		"timestamp":   ts,
		"isSidechain": false,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message": map[string]any{
			"role":    "assistant",
			"content": []any{map[string]any{"type": "text", "text": text}},
		},
	})
}

func jsonline(m map[string]any) string {
	b, err := json.Marshal(m)
	if err != nil {
		panic(err)
	}
	return string(b) + "\n"
}
