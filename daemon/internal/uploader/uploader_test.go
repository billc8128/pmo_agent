package uploader

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
)

// fakeServer wires an http handler into an httptest.Server and returns
// a Client pointed at it. Each test gets its own.
func fakeServer(t *testing.T, handler http.HandlerFunc) (*Client, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return New(srv.URL, "pmo_test_token"), srv
}

func sampleTurn() adapter.Turn {
	return adapter.Turn{
		Agent:             "claude_code",
		AgentSessionID:    "session-x",
		ProjectPath:       "/tmp/proj",
		ProjectRoot:       "/tmp",
		TurnIndex:         3,
		UserMessage:       "hi",
		AgentResponseFull: "yo",
		UserMessageAt:     time.Date(2026, 4, 30, 1, 0, 0, 0, time.UTC),
		AgentResponseAt:   time.Date(2026, 4, 30, 1, 0, 5, 0, time.UTC),
	}
}

func TestUpload_HappyPath(t *testing.T) {
	var got Payload
	c, _ := fakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		// Verify request shape.
		if r.Method != http.MethodPost {
			t.Errorf("method = %s", r.Method)
		}
		if r.URL.Path != "/functions/v1/ingest" {
			t.Errorf("path = %s", r.URL.Path)
		}
		if got, want := r.Header.Get("Authorization"), "Bearer pmo_test_token"; got != want {
			t.Errorf("auth = %q, want %q", got, want)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("content-type = %q", ct)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatalf("decode payload: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"turn_id":42,"deduped":false}`))
	})

	res, err := c.Upload(context.Background(), sampleTurn())
	if err != nil {
		t.Fatalf("Upload: %v", err)
	}
	if !res.OK || res.Deduped || res.TurnID == nil || *res.TurnID != 42 {
		t.Errorf("result = %+v, want ok=true turn_id=42 deduped=false", res)
	}
	// Spot-check payload conversion.
	if got.Agent != "claude_code" || got.TurnIndex != 3 {
		t.Errorf("payload mis-serialized: %+v", got)
	}
	if got.AgentResponseAt == nil || *got.AgentResponseAt == "" {
		t.Errorf("agent_response_at should be present, got nil")
	}
	if got.ProjectRoot == nil || *got.ProjectRoot != "/tmp" {
		t.Errorf("project_root = %v, want /tmp", got.ProjectRoot)
	}
}

func TestUpload_DedupeReplyOK(t *testing.T) {
	c, _ := fakeServer(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true,"turn_id":null,"deduped":true}`))
	})
	res, err := c.Upload(context.Background(), sampleTurn())
	if err != nil {
		t.Fatalf("Upload: %v", err)
	}
	if !res.Deduped || res.TurnID != nil {
		t.Errorf("expected deduped=true turn_id=null, got %+v", res)
	}
}

func TestUpload_4xxIsPermanent(t *testing.T) {
	c, _ := fakeServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid token"}`))
	})
	_, err := c.Upload(context.Background(), sampleTurn())
	if !errors.Is(err, ErrPermanent) {
		t.Errorf("4xx should be permanent; got %v", err)
	}
	if errors.Is(err, ErrTransient) {
		t.Errorf("4xx must not be transient")
	}
}

func TestUpload_5xxIsTransient(t *testing.T) {
	c, _ := fakeServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`oops`))
	})
	_, err := c.Upload(context.Background(), sampleTurn())
	if !errors.Is(err, ErrTransient) {
		t.Errorf("5xx should be transient; got %v", err)
	}
}

func TestUpload_429IsTransient(t *testing.T) {
	c, _ := fakeServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	})
	_, err := c.Upload(context.Background(), sampleTurn())
	if !errors.Is(err, ErrTransient) {
		t.Errorf("429 should be transient; got %v", err)
	}
}

func TestFromTurn_NullableFields(t *testing.T) {
	zero := adapter.Turn{
		Agent:          "claude_code",
		AgentSessionID: "s",
		TurnIndex:      0,
		UserMessage:    "hi",
		UserMessageAt:  time.Date(2026, 4, 30, 1, 0, 0, 0, time.UTC),
	}
	p := FromTurn(zero)
	if p.ProjectPath != nil {
		t.Error("project_path should be nil when empty")
	}
	if p.ProjectRoot != nil {
		t.Error("project_root should be nil when empty")
	}
	if p.AgentResponseFull != nil {
		t.Error("agent_response_full should be nil when empty")
	}
	if p.AgentResponseAt != nil {
		t.Error("agent_response_at should be nil when zero")
	}

	// Marshal and verify the nulls show up as JSON null.
	b, _ := json.Marshal(p)
	s := string(b)
	for _, want := range []string{
		`"project_path":null`,
		`"project_root":null`,
		`"agent_response_full":null`,
		`"agent_response_at":null`,
	} {
		if !strings.Contains(s, want) {
			t.Errorf("payload should contain %s; full body: %s", want, s)
		}
	}
}
