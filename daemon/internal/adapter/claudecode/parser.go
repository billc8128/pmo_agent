// Package claudecode parses Claude Code's JSONL transcripts into
// completed (user_message, agent_response) turns. Per spec §4.4.
//
// Schema notes (learned from real ~/.claude/projects/<slug>/<uuid>.jsonl
// files; not all of these are documented in the spec):
//
//   - Each line is one entry. Entries are ordered, not strictly chained
//     (parentUuid exists but we treat order as authoritative for MVP).
//   - Top-level "type" can be: user, assistant, attachment, system,
//     last-prompt, permission-mode, file-history-snapshot, ...
//     We only care about "user" and "assistant".
//   - user.message.content is EITHER a string (simple prompt) OR a list
//     of blocks (typically one block of type "tool_result"). Only the
//     string form (or list whose first block is "text") is a real
//     prompt that starts a new turn; tool_result-style user entries are
//     just plumbing for the previous turn.
//   - assistant.message.content is always a list, with blocks of type
//     "text" and "tool_use". A "final" assistant block (one that closes
//     a turn) has no tool_use blocks.
//   - "isSidechain": true marks entries from sub-agent (Task) calls.
//     We skip them entirely for MVP — they'd otherwise produce
//     confusing nested turns in the public timeline.

package claudecode

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/redact"
)

// AgentName is the value written to turns.agent for Claude Code turns.
const AgentName = "claude_code"

// rawEntry is the minimal subset of fields we need from each JSONL line.
// We unmarshal Message into a typed struct rather than json.RawMessage
// because the shape varies (content can be string or list) and we want
// the type system to force us to handle both.
type rawEntry struct {
	Type        string    `json:"type"`
	UUID        string    `json:"uuid"`
	ParentUUID  string    `json:"parentUuid"`
	IsSidechain bool      `json:"isSidechain"`
	SessionID   string    `json:"sessionId"`
	Timestamp   time.Time `json:"timestamp"`
	CWD         string    `json:"cwd"`
	Message     *rawMsg   `json:"message"`
}

type rawMsg struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

type contentBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text,omitempty"`
	Name      string          `json:"name,omitempty"`       // for tool_use
	Input     json.RawMessage `json:"input,omitempty"`      // for tool_use
	ToolUseID string          `json:"tool_use_id,omitempty"` // for tool_result
}

// ParseFile reads the entire JSONL file at path and returns every
// completed turn it contains. Used for the offline path in Milestone
// 1.3 and 1.4. Milestone 1.5 will reuse the same parsing logic in a
// streaming form.
//
// Turns whose closing assistant entry has not yet been written are NOT
// returned — they'll only be emitted once the file grows enough for the
// state machine to confirm closure.
func ParseFile(path string) ([]adapter.Turn, error) {
	r, err := openFile(path)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return Parse(r, sessionIDFromFilename(path))
}

// Parse consumes a JSONL stream and returns completed turns. The
// fallbackSessionID is used when an entry has no sessionId field
// (unusual but defensive).
func Parse(r io.Reader, fallbackSessionID string) ([]adapter.Turn, error) {
	sc := bufio.NewScanner(r)
	// CC entries can be large (assistant text + tool inputs); bump the
	// per-line buffer ceiling to 4 MiB.
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)

	sm := &stateMachine{fallbackSession: fallbackSessionID}
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e rawEntry
		if err := json.Unmarshal(line, &e); err != nil {
			// Skip malformed lines defensively — do not abort the file.
			// A single bad line shouldn't kill an upload pass.
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

// stateMachine: feed entries in order; out come completed turns.
type stateMachine struct {
	fallbackSession string

	turns []adapter.Turn

	// current turn under construction
	hasOpen      bool
	curUserText  string
	curUserAt    time.Time
	curSessionID string
	curCWD       string
	curResponse  strings.Builder
	curLastAt    time.Time
}

func (sm *stateMachine) feed(e *rawEntry) error {
	if e.IsSidechain {
		return nil // sub-agent traffic; not for the public timeline
	}
	switch e.Type {
	case "user":
		return sm.handleUser(e)
	case "assistant":
		return sm.handleAssistant(e)
	default:
		return nil
	}
}

func (sm *stateMachine) handleUser(e *rawEntry) error {
	if e.Message == nil {
		return nil
	}
	prompt, isPrompt, err := extractUserPrompt(e.Message.Content)
	if err != nil {
		return err
	}
	if !isPrompt {
		return nil // tool_result reply; not a new turn boundary
	}
	// A new prompt closes any half-open turn (assistant without final
	// non-tool_use block — shouldn't happen mid-stream, but defensive)
	// by simply discarding it: an unfinished turn is not yet a turn.
	if sm.hasOpen {
		sm.discardOpen()
	}
	prompt, _ = redact.Redact(prompt)
	sm.hasOpen = true
	sm.curUserText = prompt
	sm.curUserAt = e.Timestamp
	sm.curSessionID = pickSessionID(e, sm.fallbackSession)
	sm.curCWD = e.CWD
	sm.curResponse.Reset()
	sm.curLastAt = time.Time{}
	return nil
}

func (sm *stateMachine) handleAssistant(e *rawEntry) error {
	if !sm.hasOpen || e.Message == nil {
		return nil
	}
	var blocks []contentBlock
	if err := json.Unmarshal(e.Message.Content, &blocks); err != nil {
		// Some assistants might emit string content; if so, treat as
		// one text block.
		var asString string
		if json.Unmarshal(e.Message.Content, &asString) == nil {
			blocks = []contentBlock{{Type: "text", Text: asString}}
		} else {
			return nil
		}
	}

	hasToolUse := false
	for _, b := range blocks {
		switch b.Type {
		case "text":
			if sm.curResponse.Len() > 0 {
				sm.curResponse.WriteString("\n\n")
			}
			sm.curResponse.WriteString(b.Text)
		case "tool_use":
			hasToolUse = true
			summary := summarizeToolUse(b)
			if sm.curResponse.Len() > 0 {
				sm.curResponse.WriteString("\n\n")
			}
			sm.curResponse.WriteString(summary)
		case "thinking":
			// Skip — never publish reasoning traces.
			continue
		}
	}
	sm.curLastAt = e.Timestamp

	if !hasToolUse {
		sm.closeCurrent()
	}
	return nil
}

func (sm *stateMachine) closeCurrent() {
	full, _ := redact.Redact(sm.curResponse.String())
	turn := adapter.Turn{
		Agent:             AgentName,
		AgentSessionID:    sm.curSessionID,
		ProjectPath:       sm.curCWD,
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
	sm.curSessionID = ""
	sm.curCWD = ""
	sm.curResponse.Reset()
	sm.curLastAt = time.Time{}
}

// extractUserPrompt distinguishes "real user prompt" from "tool_result
// plumbing reply". Returns (text, isPrompt, error).
//
// A real prompt is either:
//   - content is a JSON string (the simple case), OR
//   - content is a list whose first non-empty block has type "text"
//
// A tool_result reply is a list whose first block has type
// "tool_result". We surface that as isPrompt=false so the caller skips it.
func extractUserPrompt(raw json.RawMessage) (string, bool, error) {
	if len(raw) == 0 {
		return "", false, nil
	}
	// Try string first.
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s, true, nil
	}
	var blocks []contentBlock
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return "", false, fmt.Errorf("unrecognized user content shape: %w", err)
	}
	for _, b := range blocks {
		switch b.Type {
		case "text":
			return b.Text, true, nil
		case "tool_result":
			return "", false, nil
		}
	}
	return "", false, nil
}

// summarizeToolUse renders a tool_use block as a structured one-line
// hint that gives downstream summarizers (the LLM behind agent_summary)
// enough information to describe what was actually done — not just the
// tool name, but the action's target.
//
// Format (chosen so the LLM can parse it without help):
//
//	[Bash] command=git commit -m "feat: ..." | description=commit milestone work
//	[Edit] path=daemon/internal/watcher/watcher.go
//	[Read] path=docs/specs/2026-04-29-mvp-design.md
//	[Write] path=backend/supabase/migrations/0002_summarize_trigger.sql
//	[Grep] pattern=fsnotify | path=internal/
//	[WebFetch] url=https://example.com
//	[Task] description=Refactor watcher to polling
//	[TodoWrite] todos=(N items)
//
// Tool name is shown as-is (no "Tool:" prefix — saves tokens, looks
// cleaner). Up to 3 useful input fields are surfaced in order of
// "most likely to identify what happened". Long values are truncated
// at 120 chars (we previously had 80 — too aggressive when commands
// like `git commit -m "..."` carry the actual intent).
func summarizeToolUse(b contentBlock) string {
	var inputs map[string]any
	if len(b.Input) > 0 {
		_ = json.Unmarshal(b.Input, &inputs)
	}
	hints := structuredHints(b.Name, inputs)
	if len(hints) == 0 {
		return fmt.Sprintf("[%s]", b.Name)
	}
	return fmt.Sprintf("[%s] %s", b.Name, strings.Join(hints, " | "))
}

// structuredHints picks up to 3 input fields likely to describe the
// action. Per-tool overrides come first (so we know "Bash → command"
// is more salient than "Bash → description"), then a generic fallback.
func structuredHints(toolName string, in map[string]any) []string {
	if len(in) == 0 {
		return nil
	}

	// Per-tool field priority. Each list is "most-salient first".
	// Tools not listed here fall through to the generic preferred list.
	perTool := map[string][]string{
		"Bash":           {"command", "description"},
		"Edit":           {"file_path", "old_string"},
		"Write":          {"file_path"},
		"Read":           {"file_path"},
		"Grep":           {"pattern", "path", "glob"},
		"Glob":           {"pattern"},
		"WebFetch":       {"url", "prompt"},
		"WebSearch":      {"query"},
		"Task":           {"description", "subagent_type"},
		"TodoWrite":      {"_todo_count"}, // synthetic, see below
		"NotebookEdit":   {"notebook_path", "new_source"},
		"BashOutput":     {"bash_id"},
		"KillShell":      {"shell_id"},
		"ExitPlanMode":   {"plan"},
		"SlashCommand":   {"command"},
		"AskUserQuestion": {"questions"},
	}
	priority, ok := perTool[toolName]
	if !ok {
		priority = []string{"command", "file_path", "path", "pattern", "query", "url", "description"}
	}

	out := make([]string, 0, 3)
	used := make(map[string]bool)

	// Synthetic field for TodoWrite: count of todos.
	if toolName == "TodoWrite" {
		if v, ok := in["todos"]; ok {
			if arr, ok := v.([]any); ok {
				return []string{fmt.Sprintf("todos=(%d items)", len(arr))}
			}
		}
	}

	// Walk priority order first.
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

	// Fill remaining slots from any other scalar inputs, alphabetical.
	if len(out) < 3 {
		keys := make([]string, 0, len(in))
		for k := range in {
			if !used[k] {
				keys = append(keys, k)
			}
		}
		// tiny n; selection sort to avoid the sort import bloat
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

func pickSessionID(e *rawEntry, fallback string) string {
	if e.SessionID != "" {
		return e.SessionID
	}
	return fallback
}

// sessionIDFromFilename extracts the UUID part of foo/bar/<uuid>.jsonl.
// CC stores one session per file; the filename is the canonical id.
func sessionIDFromFilename(path string) string {
	// Trim directory.
	for i := len(path) - 1; i >= 0; i-- {
		if path[i] == '/' {
			path = path[i+1:]
			break
		}
	}
	// Trim ".jsonl".
	if strings.HasSuffix(path, ".jsonl") {
		return path[:len(path)-len(".jsonl")]
	}
	return path
}

func openFile(path string) (io.ReadCloser, error) {
	if path == "" {
		return nil, errors.New("empty path")
	}
	return openFileImpl(path)
}
