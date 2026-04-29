// Package uploader posts completed turns to the backend's
// /functions/v1/ingest endpoint.
//
// Contract (mirrors backend/supabase/functions/ingest/index.ts):
//
//	POST {server_url}/functions/v1/ingest
//	Authorization: Bearer pmo_<plaintext>
//	Content-Type: application/json
//	body: see Payload below
//
// Response:
//
//	200 {"ok":true, "turn_id": <int|null>, "deduped": <bool>}
//	4xx/5xx {"ok":false, "error": "<message>"}
//
// The uploader does NOT call the local Store — that's the watcher's
// job (mark-on-success). Keeping the uploader pure makes it trivially
// retryable and unit-testable.

package uploader

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
)

// Client is the HTTP-level uploader. Construct with New and reuse it —
// it holds an http.Client with a sane timeout.
type Client struct {
	serverURL string
	token     string
	hc        *http.Client
}

// New returns an uploader bound to a specific server + PAT. serverURL
// must NOT include a trailing slash.
func New(serverURL, token string) *Client {
	return &Client{
		serverURL: strings.TrimRight(serverURL, "/"),
		token:     token,
		hc:        &http.Client{Timeout: 30 * time.Second},
	}
}

// Result is what the server returned for a successfully accepted POST.
type Result struct {
	OK      bool   `json:"ok"`
	TurnID  *int64 `json:"turn_id"`
	Deduped bool   `json:"deduped"`
	Error   string `json:"error,omitempty"`
}

// Payload is the wire format. We construct it from adapter.Turn rather
// than letting adapter.Turn marshal directly — the wire shape is a
// stable contract with the server, separate from the in-memory type.
type Payload struct {
	Agent             string  `json:"agent"`
	AgentSessionID    string  `json:"agent_session_id"`
	ProjectPath       *string `json:"project_path"`
	TurnIndex         int     `json:"turn_index"`
	UserMessage       string  `json:"user_message"`
	AgentResponseFull *string `json:"agent_response_full"`
	UserMessageAt     string  `json:"user_message_at"`
	AgentResponseAt   *string `json:"agent_response_at"`
}

// FromTurn converts an adapter.Turn into a wire Payload. Empty optional
// fields (project_path, agent_response_full, agent_response_at) become
// JSON null rather than empty strings, so the server's NULL semantics
// in Postgres are preserved.
func FromTurn(t adapter.Turn) Payload {
	p := Payload{
		Agent:          t.Agent,
		AgentSessionID: t.AgentSessionID,
		TurnIndex:      t.TurnIndex,
		UserMessage:    t.UserMessage,
		UserMessageAt:  t.UserMessageAt.UTC().Format(time.RFC3339Nano),
	}
	if t.ProjectPath != "" {
		s := t.ProjectPath
		p.ProjectPath = &s
	}
	if t.AgentResponseFull != "" {
		s := t.AgentResponseFull
		p.AgentResponseFull = &s
	}
	if !t.AgentResponseAt.IsZero() {
		s := t.AgentResponseAt.UTC().Format(time.RFC3339Nano)
		p.AgentResponseAt = &s
	}
	return p
}

// Upload POSTs a single turn. Errors are categorized:
//   - permanent (4xx that aren't 429): caller should NOT retry; the
//     turn is malformed or the token is bad. Surface as ErrPermanent.
//   - transient (network, 5xx, 429): caller MAY retry with backoff.
//     Surfaced as ErrTransient.
//
// Both wrap the original error, so errors.Is / errors.As work as expected.
func (c *Client) Upload(ctx context.Context, turn adapter.Turn) (*Result, error) {
	body, err := json.Marshal(FromTurn(turn))
	if err != nil {
		return nil, fmt.Errorf("marshal payload: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		c.serverURL+"/functions/v1/ingest", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, transient(fmt.Errorf("http do: %w", err))
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 429 || resp.StatusCode >= 500 {
		return nil, transient(fmt.Errorf("server %d: %s", resp.StatusCode, snippet(respBody)))
	}
	if resp.StatusCode >= 400 {
		return nil, permanent(fmt.Errorf("server %d: %s", resp.StatusCode, snippet(respBody)))
	}

	var r Result
	if err := json.Unmarshal(respBody, &r); err != nil {
		// 200 with garbage body is genuinely weird; treat as transient
		// (perhaps a proxy error page) so we retry rather than swallow.
		return nil, transient(fmt.Errorf("decode body: %w (body=%s)", err, snippet(respBody)))
	}
	if !r.OK {
		return nil, permanent(fmt.Errorf("server returned ok=false: %s", r.Error))
	}
	return &r, nil
}

// Sentinel errors so callers can decide retry policy.
var (
	ErrPermanent = errors.New("upload: permanent failure")
	ErrTransient = errors.New("upload: transient failure")
)

func permanent(err error) error { return fmt.Errorf("%w: %v", ErrPermanent, err) }
func transient(err error) error { return fmt.Errorf("%w: %v", ErrTransient, err) }

func snippet(b []byte) string {
	const max = 200
	if len(b) > max {
		return string(b[:max]) + "..."
	}
	return string(b)
}
