package rawtranscript

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBuildSnapshotClaudeCode(t *testing.T) {
	dir := t.TempDir()
	jsonlPath := filepath.Join(dir, "fallback-session.jsonl")
	body := joinLines(
		`{"type":"user","sessionId":"sess-1","cwd":"`+slash(filepath.Join(dir, "repo"))+`","timestamp":"2026-05-01T00:00:00Z","message":{"role":"user","content":"hi"}}`,
		`{"type":"assistant","sessionId":"sess-1","cwd":"`+slash(filepath.Join(dir, "repo"))+`","timestamp":"2026-05-01T00:00:01Z","message":{"role":"assistant","content":[{"type":"text","text":"yo"}]}}`,
	)
	if err := os.WriteFile(jsonlPath, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	snap, err := BuildSnapshot(AgentClaudeCode, jsonlPath)
	if err != nil {
		t.Fatalf("BuildSnapshot: %v", err)
	}
	if snap.AgentSessionID != "sess-1" {
		t.Fatalf("session id = %q, want sess-1", snap.AgentSessionID)
	}
	if snap.ProjectPath != filepath.Join(dir, "repo") {
		t.Fatalf("project path = %q", snap.ProjectPath)
	}
	if snap.LineCount != 2 {
		t.Fatalf("line count = %d, want 2", snap.LineCount)
	}
	wantSHA := sha256.Sum256([]byte(body))
	if snap.SHA256 != hex.EncodeToString(wantSHA[:]) {
		t.Fatalf("sha = %q, want %q", snap.SHA256, hex.EncodeToString(wantSHA[:]))
	}
	gotBody := gunzip(t, snap.GzipBytes)
	if string(gotBody) != body {
		t.Fatalf("gunzip body mismatch: %q", string(gotBody))
	}
}

func TestBuildSnapshotCodex(t *testing.T) {
	dir := t.TempDir()
	jsonlPath := filepath.Join(dir, "rollout-2026-04-30T02-54-57-019dda98-243a-7560-a4a7-100e1d3534b3.jsonl")
	body := joinLines(
		`{"type":"session_meta","timestamp":"2026-05-01T00:00:00Z","payload":{"id":"codex-session","cwd":"`+slash(filepath.Join(dir, "repo"))+`"}}`,
		`{"type":"event_msg","timestamp":"2026-05-01T00:00:01Z","payload":{"type":"user_message","message":"hi"}}`,
	)
	if err := os.WriteFile(jsonlPath, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	snap, err := BuildSnapshot(AgentCodex, jsonlPath)
	if err != nil {
		t.Fatalf("BuildSnapshot: %v", err)
	}
	if snap.AgentSessionID != "codex-session" {
		t.Fatalf("session id = %q, want codex-session", snap.AgentSessionID)
	}
	if snap.ProjectPath != filepath.Join(dir, "repo") {
		t.Fatalf("project path = %q", snap.ProjectPath)
	}
}

func TestClientUploadSendsGzipAndMetadata(t *testing.T) {
	body := []byte("hello\nworld\n")
	gz := gzipBytes(t, body)
	snap := Snapshot{
		Agent:          AgentClaudeCode,
		AgentSessionID: "sess-1",
		ProjectPath:    "/tmp/repo",
		ProjectRoot:    "/tmp/repo",
		LocalPath:      "/tmp/repo/sess-1.jsonl",
		ByteSize:       int64(len(body)),
		CompressedSize: int64(len(gz)),
		LineCount:      2,
		SHA256:         "abc123",
		GzipBytes:      gz,
	}

	var gotMeta UploadMetadata
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/functions/v1/upload_transcript" {
			t.Fatalf("path = %s", r.URL.Path)
		}
		if got, want := r.Header.Get("Authorization"), "Bearer pmo_test"; got != want {
			t.Fatalf("authorization = %q, want %q", got, want)
		}
		if got, want := r.Header.Get("Content-Type"), "application/gzip"; got != want {
			t.Fatalf("content-type = %q, want %q", got, want)
		}
		rawMeta, err := base64.StdEncoding.DecodeString(r.Header.Get(metadataHeader))
		if err != nil {
			t.Fatalf("decode metadata: %v", err)
		}
		if err := json.Unmarshal(rawMeta, &gotMeta); err != nil {
			t.Fatalf("unmarshal metadata: %v", err)
		}
		gotBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"storage_path":"u/claude_code/sess-1.jsonl.gz"}`))
	}))
	defer srv.Close()

	client := NewClient(srv.URL, "pmo_test")
	res, err := client.Upload(context.Background(), snap)
	if err != nil {
		t.Fatalf("Upload: %v", err)
	}
	if !res.OK || res.StoragePath == "" {
		t.Fatalf("result = %+v", res)
	}
	if gotMeta.Agent != AgentClaudeCode || gotMeta.AgentSessionID != "sess-1" || gotMeta.ByteSize != int64(len(body)) {
		t.Fatalf("metadata = %+v", gotMeta)
	}
	if !bytes.Equal(gotBody, gz) {
		t.Fatal("uploaded body does not match gzip bytes")
	}
}

func TestReadyJSONLFilesWaitsForQuietMTime(t *testing.T) {
	root := t.TempDir()
	nested := filepath.Join(root, "nested")
	if err := os.MkdirAll(nested, 0o700); err != nil {
		t.Fatal(err)
	}
	ready := filepath.Join(nested, "ready.jsonl")
	recent := filepath.Join(nested, "recent.jsonl")
	ignored := filepath.Join(nested, "notes.txt")
	for _, p := range []string{ready, recent, ignored} {
		if err := os.WriteFile(p, []byte("x\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	now := time.Date(2026, 5, 1, 1, 0, 0, 0, time.UTC)
	if err := os.Chtimes(ready, now.Add(-time.Minute), now.Add(-time.Minute)); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(recent, now.Add(-5*time.Second), now.Add(-5*time.Second)); err != nil {
		t.Fatal(err)
	}

	got := ReadyJSONLFiles(root, 30*time.Second, now)
	if len(got) != 1 || got[0] != ready {
		t.Fatalf("ReadyJSONLFiles = %v, want [%s]", got, ready)
	}
}

func gzipBytes(t *testing.T, b []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(b); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func gunzip(t *testing.T, b []byte) []byte {
	t.Helper()
	zr, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer zr.Close()
	out, err := io.ReadAll(zr)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func joinLines(lines ...string) string {
	var buf bytes.Buffer
	for _, line := range lines {
		buf.WriteString(line)
		buf.WriteByte('\n')
	}
	return buf.String()
}

func slash(p string) string {
	return filepath.ToSlash(p)
}
