package claudecode

import (
	"strings"
	"testing"
)

// Each test feeds a synthetic JSONL stream into Parse and asserts on the
// resulting turns. Synthetic fixtures (rather than checked-in real
// transcripts) keep the tests readable and fast, and avoid leaking
// private session content into git.

func TestParse_SimpleTurn(t *testing.T) {
	stream := join(
		userText("hello", "2026-04-30T01:00:00Z"),
		assistantText("hi there", "2026-04-30T01:00:02Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	if turns[0].UserMessage != "hello" {
		t.Errorf("user_message = %q, want %q", turns[0].UserMessage, "hello")
	}
	if turns[0].AgentResponseFull != "hi there" {
		t.Errorf("agent_response_full = %q, want %q", turns[0].AgentResponseFull, "hi there")
	}
	if turns[0].TurnIndex != 0 {
		t.Errorf("turn_index = %d, want 0", turns[0].TurnIndex)
	}
}

func TestParse_OpenTurnIsNotEmitted(t *testing.T) {
	// User prompt arrives but no assistant response yet — must NOT emit a turn.
	stream := userText("hello", "2026-04-30T01:00:00Z")
	turns := mustParse(t, stream)
	if len(turns) != 0 {
		t.Fatalf("open turn must not be emitted; got %d", len(turns))
	}
}

func TestParse_ToolUseAccumulatesBeforeFinal(t *testing.T) {
	// Common case: user → assistant(tool_use) → user(tool_result) → assistant(text final).
	stream := join(
		userText("run ls", "2026-04-30T01:00:00Z"),
		assistantToolUse("Bash", `{"command":"ls -la"}`, "2026-04-30T01:00:01Z"),
		userToolResult("toolu_x", "results...", "2026-04-30T01:00:02Z"),
		assistantText("done", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	resp := turns[0].AgentResponseFull
	if !strings.Contains(resp, "[Bash]") {
		t.Errorf("response should contain tool tag; got %q", resp)
	}
	if !strings.Contains(resp, "command=ls -la") {
		t.Errorf("response should render command input; got %q", resp)
	}
	if !strings.Contains(resp, "done") {
		t.Errorf("response should contain final text; got %q", resp)
	}
}

func TestParse_SidechainSkipped(t *testing.T) {
	// A sidechain user prompt should not begin a turn; subsequent
	// non-sidechain content is independent.
	stream := join(
		userTextSidechain("from subagent", "2026-04-30T01:00:00Z"),
		assistantTextSidechain("subagent reply", "2026-04-30T01:00:01Z"),
		userText("real prompt", "2026-04-30T01:00:02Z"),
		assistantText("real reply", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	if turns[0].UserMessage != "real prompt" {
		t.Errorf("user_message = %q, want %q", turns[0].UserMessage, "real prompt")
	}
}

func TestParse_NewPromptDiscardsHalfOpenTurn(t *testing.T) {
	// Pathological: assistant emits text then a new user prompt arrives
	// without any tool_use cycle. The half-open turn (with no closing
	// final assistant) should be discarded; only the new turn closes.
	stream := join(
		userText("first", "2026-04-30T01:00:00Z"),
		assistantToolUse("Bash", `{"command":"ls"}`, "2026-04-30T01:00:01Z"),
		// no tool_result, no final assistant — instead a new prompt:
		userText("second", "2026-04-30T01:00:02Z"),
		assistantText("done", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 closed turn, got %d", len(turns))
	}
	if turns[0].UserMessage != "second" {
		t.Errorf("expected the second prompt to be the surviving turn; got %q", turns[0].UserMessage)
	}
}

func TestParse_ToolResultUserDoesNotStartTurn(t *testing.T) {
	// A user entry whose first content block is tool_result must NOT
	// be treated as a new prompt.
	stream := join(
		userText("first", "2026-04-30T01:00:00Z"),
		assistantToolUse("Read", `{"file_path":"/tmp/x"}`, "2026-04-30T01:00:01Z"),
		userToolResult("toolu_y", "content...", "2026-04-30T01:00:02Z"),
		assistantText("ok", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want exactly 1 turn (tool_result must not split it); got %d", len(turns))
	}
}

func TestParse_TurnIndexesAreSequential(t *testing.T) {
	stream := join(
		userText("a", "2026-04-30T01:00:00Z"),
		assistantText("A", "2026-04-30T01:00:01Z"),
		userText("b", "2026-04-30T01:00:02Z"),
		assistantText("B", "2026-04-30T01:00:03Z"),
		userText("c", "2026-04-30T01:00:04Z"),
		assistantText("C", "2026-04-30T01:00:05Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 3 {
		t.Fatalf("want 3 turns, got %d", len(turns))
	}
	for i, tu := range turns {
		if tu.TurnIndex != i {
			t.Errorf("turn[%d].TurnIndex = %d, want %d", i, tu.TurnIndex, i)
		}
	}
}

// ─── synthetic JSONL builders ─────────────────────────────────────────

func mustParse(t *testing.T, stream string) []turnsT {
	t.Helper()
	turns, err := Parse(strings.NewReader(stream), "test-session")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	out := make([]turnsT, len(turns))
	for i, tt := range turns {
		out[i] = turnsT{
			UserMessage:       tt.UserMessage,
			AgentResponseFull: tt.AgentResponseFull,
			TurnIndex:         tt.TurnIndex,
		}
	}
	return out
}

// turnsT is a stripped-down view of adapter.Turn used only by tests, so
// we don't import the adapter package here just for the type and so
// tests aren't sensitive to fields we don't care about.
type turnsT struct {
	UserMessage       string
	AgentResponseFull string
	TurnIndex         int
}

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

func userTextSidechain(text, ts string) string {
	return jsonline(map[string]any{
		"type":        "user",
		"uuid":        "u-" + ts,
		"timestamp":   ts,
		"isSidechain": true,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message":     map[string]any{"role": "user", "content": text},
	})
}

func userToolResult(toolUseID, payload, ts string) string {
	return jsonline(map[string]any{
		"type":        "user",
		"uuid":        "u-" + ts,
		"timestamp":   ts,
		"isSidechain": false,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message": map[string]any{
			"role": "user",
			"content": []any{map[string]any{
				"type":         "tool_result",
				"tool_use_id":  toolUseID,
				"content":      payload,
			}},
		},
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

func assistantTextSidechain(text, ts string) string {
	return jsonline(map[string]any{
		"type":        "assistant",
		"uuid":        "a-" + ts,
		"timestamp":   ts,
		"isSidechain": true,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message": map[string]any{
			"role":    "assistant",
			"content": []any{map[string]any{"type": "text", "text": text}},
		},
	})
}

func assistantToolUse(name, inputJSON, ts string) string {
	return jsonline(map[string]any{
		"type":        "assistant",
		"uuid":        "a-" + ts,
		"timestamp":   ts,
		"isSidechain": false,
		"sessionId":   "test-session",
		"cwd":         "/tmp",
		"message": map[string]any{
			"role": "assistant",
			"content": []any{map[string]any{
				"type":  "tool_use",
				"id":    "toolu_" + ts,
				"name":  name,
				"input": rawJSON(inputJSON),
			}},
		},
	})
}

// rawJSON converts a JSON-encoded string into the matching parsed value.
// Marshalling it back will produce the same JSON, which is what tests want.
func rawJSON(s string) any {
	// We rely on encoding/json to round-trip; a small helper saves us
	// from littering tests with explicit map[string]any literals.
	return jsonRoundTrip(s)
}

func jsonRoundTrip(s string) any {
	var v any
	_ = jsonUnmarshal(s, &v)
	return v
}

func join(parts ...string) string { return strings.Join(parts, "") }
