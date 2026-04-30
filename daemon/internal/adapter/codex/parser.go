// Package codex parses OpenAI Codex CLI's JSONL transcripts into
// completed (user_message, agent_response) turns.
//
// Codex stores one rollout per file at:
//
//	~/.codex/sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl
//
// Schema (learned from real files; not all of this is documented):
//
//   - Each line is one entry with top-level "type" and "payload".
//     Useful entry shapes:
//     · type=session_meta                        — first line, has cwd, session id
//     · type=event_msg, payload.type=task_started  — turn boundary OPEN
//     · type=event_msg, payload.type=user_message  — the prompt text
//     · type=event_msg, payload.type=agent_message — assistant chunk for the user
//     · type=event_msg, payload.type=task_complete — turn boundary CLOSE
//     · type=response_item, payload.type=function_call  — tool call, with args
//     · everything else (token_count, reasoning, turn_context, custom_tool_call_*,
//       mcp_tool_call_*, message with role=user/assistant/developer)
//       is intentionally IGNORED for the public timeline:
//         - reasoning is private CoT; we never publish it
//         - response_item.message duplicates event_msg in messier form
//         - mcp_tool_call_* / exec_command_end are noisy operational events
//
// Turn shape:
//   - user_message  = the event_msg.user_message text
//   - agent_response_full = concatenation of (function_call summaries
//     and agent_message chunks), in order
//   - agent_response_at = timestamp of task_complete

package codex

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/redact"
)

// AgentName is the value written to turns.agent for Codex turns.
const AgentName = "codex"

type rawEntry struct {
	Type      string          `json:"type"`
	Timestamp time.Time       `json:"timestamp"`
	Payload   json.RawMessage `json:"payload"`
}

type sessionMetaPayload struct {
	ID  string `json:"id"`
	CWD string `json:"cwd"`
}

type eventMsgPayload struct {
	Type    string `json:"type"`
	Message string `json:"message"`         // for user_message / agent_message
	LastMsg string `json:"last_agent_message"` // for task_complete
	TurnID  string `json:"turn_id"`
}

type functionCallPayload struct {
	Type      string `json:"type"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"` // JSON-encoded string
}

// ParseFile reads a complete Codex jsonl and returns every closed turn.
func ParseFile(path string) ([]adapter.Turn, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()
	return Parse(f, sessionIDFromFilename(path))
}

// Parse consumes a JSONL stream and returns completed turns.
// fallbackSessionID is used if session_meta is missing.
func Parse(r io.Reader, fallbackSessionID string) ([]adapter.Turn, error) {
	sc := bufio.NewScanner(r)
	// Codex outputs can be large (long tool args + agent text). 8 MiB
	// per-line ceiling.
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)

	sm := &stateMachine{fallbackSession: fallbackSessionID}
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e rawEntry
		if err := json.Unmarshal(line, &e); err != nil {
			// One bad line shouldn't kill the whole file. Skip.
			continue
		}
		if err := sm.feed(&e); err != nil {
			return nil, err
		}
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("scan jsonl: %w", err)
	}
	return sm.turns, nil
}

type stateMachine struct {
	fallbackSession string
	sessionID       string
	cwd             string
	turns           []adapter.Turn

	// current turn under construction (between task_started and task_complete)
	hasOpen     bool
	curUserText string
	curUserAt   time.Time
	curResponse strings.Builder
	curLastAt   time.Time
}

func (sm *stateMachine) feed(e *rawEntry) error {
	switch e.Type {
	case "session_meta":
		var p sessionMetaPayload
		if err := json.Unmarshal(e.Payload, &p); err == nil {
			if sm.sessionID == "" && p.ID != "" {
				sm.sessionID = p.ID
			}
			if sm.cwd == "" && p.CWD != "" {
				sm.cwd = p.CWD
			}
		}
		return nil

	case "event_msg":
		return sm.handleEventMsg(e)

	case "response_item":
		return sm.handleResponseItem(e)

	default:
		// turn_context, compacted, etc. — not relevant for the timeline
		return nil
	}
}

func (sm *stateMachine) handleEventMsg(e *rawEntry) error {
	var p eventMsgPayload
	if err := json.Unmarshal(e.Payload, &p); err != nil {
		return nil // malformed; skip
	}
	switch p.Type {
	case "task_started":
		// Discard any half-open turn (defensive; shouldn't happen
		// because well-formed transcripts always close before
		// re-opening).
		if sm.hasOpen {
			sm.discardOpen()
		}
		sm.hasOpen = true
		sm.curUserAt = e.Timestamp
		sm.curResponse.Reset()
		sm.curLastAt = time.Time{}
		// curUserText is filled by the next event_msg.user_message

	case "user_message":
		if !sm.hasOpen {
			return nil
		}
		// First user_message in a turn is the prompt. Subsequent
		// user_messages within the same turn (rare; only seen on edits)
		// override the prompt with the latest non-empty value.
		if p.Message != "" {
			redacted, _ := redact.Redact(p.Message)
			sm.curUserText = redacted
		}

	case "agent_message":
		if !sm.hasOpen {
			return nil
		}
		if p.Message == "" {
			return nil // skip empty/aborted chunks
		}
		if sm.curResponse.Len() > 0 {
			sm.curResponse.WriteString("\n\n")
		}
		redacted, _ := redact.Redact(p.Message)
		sm.curResponse.WriteString(redacted)
		sm.curLastAt = e.Timestamp

	case "task_complete":
		if !sm.hasOpen {
			return nil
		}
		// If we never accumulated any agent_message but task_complete
		// brought one along, use it as a fallback.
		if sm.curResponse.Len() == 0 && p.LastMsg != "" {
			redacted, _ := redact.Redact(p.LastMsg)
			sm.curResponse.WriteString(redacted)
		}
		sm.curLastAt = e.Timestamp
		sm.closeCurrent()
	}
	return nil
}

func (sm *stateMachine) handleResponseItem(e *rawEntry) error {
	if !sm.hasOpen {
		return nil
	}
	// We only care about function_call here. function_call_output,
	// reasoning, message, custom_tool_call* are intentionally
	// dropped — see package doc.
	var head struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(e.Payload, &head); err != nil {
		return nil
	}
	if head.Type != "function_call" {
		return nil
	}
	var p functionCallPayload
	if err := json.Unmarshal(e.Payload, &p); err != nil {
		return nil
	}
	summary := summarizeFunctionCall(p)
	if summary == "" {
		return nil
	}
	if sm.curResponse.Len() > 0 {
		sm.curResponse.WriteString("\n\n")
	}
	sm.curResponse.WriteString(summary)
	sm.curLastAt = e.Timestamp
	return nil
}

func (sm *stateMachine) closeCurrent() {
	full, _ := redact.Redact(sm.curResponse.String())
	turn := adapter.Turn{
		Agent:             AgentName,
		AgentSessionID:    sm.pickSessionID(),
		ProjectPath:       sm.cwd,
		TurnIndex:         len(sm.turns),
		UserMessage:       sm.curUserText,
		AgentResponseFull: full,
		UserMessageAt:     sm.curUserAt,
		AgentResponseAt:   sm.curLastAt,
	}
	sm.turns = append(sm.turns, turn)
	sm.discardOpen()
}

func (sm *stateMachine) discardOpen() {
	sm.hasOpen = false
	sm.curUserText = ""
	sm.curUserAt = time.Time{}
	sm.curResponse.Reset()
	sm.curLastAt = time.Time{}
}

func (sm *stateMachine) pickSessionID() string {
	if sm.sessionID != "" {
		return sm.sessionID
	}
	return sm.fallbackSession
}

// summarizeFunctionCall renders a Codex function_call as a one-line
// hint matching CC's [Tool] format so the LLM downstream sees a
// uniform shape.
//
// Codex's most common tool is "exec_command"; we render it as
//
//	[Bash] command=<cmd> | workdir=<path>
//
// (Calling it "Bash" rather than "exec_command" because Codex's
// exec_command IS effectively running shell commands, and it makes
// the timeline read consistently regardless of which agent produced
// the turn.)  Other Codex tools render as
//
//	[<name>] key1=value1 | key2=value2
//
// with up to 3 priority fields per known tool.
func summarizeFunctionCall(p functionCallPayload) string {
	var args map[string]any
	if p.Arguments != "" {
		_ = json.Unmarshal([]byte(p.Arguments), &args)
	}
	displayName := p.Name
	if displayName == "exec_command" {
		displayName = "Bash"
	}
	hints := pickHints(displayName, args)
	if len(hints) == 0 {
		return fmt.Sprintf("[%s]", displayName)
	}
	return fmt.Sprintf("[%s] %s", displayName, strings.Join(hints, " | "))
}

// pickHints prioritizes "what got done" fields per known tool, falling
// back to a generic order. Mirrors the structure of claudecode parser's
// structuredHints, but with Codex tool names.
func pickHints(toolName string, in map[string]any) []string {
	if len(in) == 0 {
		return nil
	}
	perTool := map[string][]string{
		"Bash":          {"cmd", "command", "workdir"},
		"exec_command":  {"cmd", "command", "workdir"},
		"apply_patch":   {"input"},
		"shell":         {"command", "cmd"},
		"read_file":     {"path", "file_path"},
		"update_plan":   {"explanation"},
		"web_fetch":     {"url"},
		"view_image":    {"path"},
	}
	priority, ok := perTool[toolName]
	if !ok {
		priority = []string{"command", "cmd", "file_path", "path", "url", "query", "description"}
	}

	out := make([]string, 0, 3)
	used := map[string]bool{}
	for _, k := range priority {
		if len(out) >= 3 {
			break
		}
		v, ok := in[k]
		if !ok {
			continue
		}
		if s := scalarToString(v); s != "" {
			out = append(out, fmt.Sprintf("%s=%s", k, truncate(s, 120)))
			used[k] = true
		}
	}
	if len(out) < 3 {
		keys := make([]string, 0, len(in))
		for k := range in {
			if !used[k] {
				keys = append(keys, k)
			}
		}
		// tiny n; selection sort
		for i := 0; i < len(keys); i++ {
			for j := i + 1; j < len(keys); j++ {
				if keys[j] < keys[i] {
					keys[i], keys[j] = keys[j], keys[i]
				}
			}
		}
		for _, k := range keys {
			if len(out) >= 3 {
				break
			}
			if s := scalarToString(in[k]); s != "" {
				out = append(out, fmt.Sprintf("%s=%s", k, truncate(s, 120)))
			}
		}
	}
	return out
}

func scalarToString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case float64:
		return fmt.Sprintf("%v", x)
	case bool:
		return fmt.Sprintf("%v", x)
	}
	return ""
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// sessionIDFromFilename extracts the UUID-like tail from a Codex jsonl
// filename. Codex names are
//
//	rollout-<RFC3339>-<UUID>.jsonl
//
// e.g. rollout-2026-04-30T02-54-57-019dda98-243a-7560-a4a7-100e1d3534b3.jsonl.
// We take the last 5 hyphen-joined groups (the UUID) as the session_id.
// session_meta usually overrides this.
func sessionIDFromFilename(path string) string {
	base := filepath.Base(path)
	base = strings.TrimSuffix(base, ".jsonl")
	parts := strings.Split(base, "-")
	if len(parts) < 5 {
		return base
	}
	return strings.Join(parts[len(parts)-5:], "-")
}
