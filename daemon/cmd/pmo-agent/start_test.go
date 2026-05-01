package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/rawtranscript"
	"github.com/superlion8/pmo_agent/daemon/internal/store"
)

func TestRawTranscriptBaselineSkipsExistingQuietFilesAndUploadsNewFiles(t *testing.T) {
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	now := time.Date(2026, 5, 2, 10, 0, 0, 0, time.UTC)

	existing := filepath.Join(root, "existing.jsonl")
	writeClaudeRawTranscript(t, existing, "existing-session", repo)
	setMTime(t, existing, now.Add(-time.Minute))

	st, err := store.OpenAt(filepath.Join(root, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	var uploads atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Fatalf("method = %s, want POST", r.Method)
		}
		uploads.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"storage_path":"u/claude_code/session.jsonl.gz"}`))
	}))
	defer srv.Close()

	cli := rawtranscript.NewClient(srv.URL, "pmo_test_token")
	src := rawTranscriptSource{agent: rawtranscript.AgentClaudeCode, root: root}

	if err := seedRawTranscriptBaselines(st, []rawTranscriptSource{src}); err != nil {
		t.Fatal(err)
	}
	uploadReadyRawTranscripts(context.Background(), st, cli, src, now)
	if got := uploads.Load(); got != 0 {
		t.Fatalf("uploads after baseline = %d, want 0", got)
	}

	newFile := filepath.Join(root, "new.jsonl")
	writeClaudeRawTranscript(t, newFile, "new-session", repo)
	setMTime(t, newFile, now.Add(-time.Minute))

	uploadReadyRawTranscripts(context.Background(), st, cli, src, now)
	if got := uploads.Load(); got != 1 {
		t.Fatalf("uploads after new file = %d, want 1", got)
	}

	uploadReadyRawTranscripts(context.Background(), st, cli, src, now)
	if got := uploads.Load(); got != 1 {
		t.Fatalf("uploads after unchanged new file = %d, want still 1", got)
	}
}

func writeClaudeRawTranscript(t *testing.T, path, sessionID, cwd string) {
	t.Helper()
	body := `{"type":"user","sessionId":"` + sessionID + `","cwd":"` + filepath.ToSlash(cwd) + `","message":{"role":"user","content":"hi"}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func setMTime(t *testing.T, path string, mtime time.Time) {
	t.Helper()
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}
