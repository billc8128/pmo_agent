package codex

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestParse_SimpleTurn(t *testing.T) {
	stream := join(
		sessionMeta("sess-1", "/tmp/proj"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("hello", "2026-04-30T01:00:00Z"),
		agentMessage("hi there", "2026-04-30T01:00:02Z"),
		taskComplete("hi there", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	if turns[0].UserMessage != "hello" {
		t.Errorf("user_message = %q", turns[0].UserMessage)
	}
	if turns[0].AgentResponseFull != "hi there" {
		t.Errorf("agent_response_full = %q", turns[0].AgentResponseFull)
	}
	if turns[0].AgentSessionID != "sess-1" {
		t.Errorf("session_id = %q, want sess-1", turns[0].AgentSessionID)
	}
	if turns[0].ProjectPath != "/tmp/proj" {
		t.Errorf("project_path = %q", turns[0].ProjectPath)
	}
}

func TestParse_OpenTurnIsNotEmitted(t *testing.T) {
	// task_started + user + assistant text but NO task_complete → not a turn yet.
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("hi", "2026-04-30T01:00:00Z"),
		agentMessage("hello", "2026-04-30T01:00:01Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 0 {
		t.Fatalf("open turn must not emit; got %d", len(turns))
	}
}

func TestParse_TaskCompleteWithoutResponseIsNotEmitted(t *testing.T) {
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("hi", "2026-04-30T01:00:00Z"),
		taskComplete("", "2026-04-30T01:00:01Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 0 {
		t.Fatalf("empty task_complete must not emit a turn; got %d", len(turns))
	}
}

func TestParse_MultipleAgentMessagesConcatenate(t *testing.T) {
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("plan it", "2026-04-30T01:00:00Z"),
		agentMessage("first part", "2026-04-30T01:00:01Z"),
		agentMessage("second part", "2026-04-30T01:00:02Z"),
		taskComplete("second part", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	resp := turns[0].AgentResponseFull
	if !strings.Contains(resp, "first part") || !strings.Contains(resp, "second part") {
		t.Errorf("expected both parts; got %q", resp)
	}
}

func TestParse_FunctionCallRendered(t *testing.T) {
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("run ls", "2026-04-30T01:00:00Z"),
		functionCall("exec_command", `{"cmd":"ls -la","workdir":"/tmp"}`, "2026-04-30T01:00:01Z"),
		agentMessage("done", "2026-04-30T01:00:02Z"),
		taskComplete("done", "2026-04-30T01:00:03Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	resp := turns[0].AgentResponseFull
	// exec_command should render as [Bash] for cross-agent consistency
	if !strings.Contains(resp, "[Bash]") {
		t.Errorf("expected [Bash] tag (renamed from exec_command); got %q", resp)
	}
	if !strings.Contains(resp, "cmd=ls -la") {
		t.Errorf("expected cmd hint; got %q", resp)
	}
	if !strings.Contains(resp, "done") {
		t.Errorf("expected final text; got %q", resp)
	}
}

func TestParse_EmptyAgentMessageSkipped(t *testing.T) {
	// Aborted turns sometimes have an agent_message with empty body;
	// task_complete brings the real text via last_agent_message.
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("go", "2026-04-30T01:00:00Z"),
		agentMessage("", "2026-04-30T01:00:01Z"),
		taskComplete("recovered text", "2026-04-30T01:00:02Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	if turns[0].AgentResponseFull != "recovered text" {
		t.Errorf("expected fallback to last_agent_message; got %q", turns[0].AgentResponseFull)
	}
}

func TestParse_SkipsReasoningAndOtherNoise(t *testing.T) {
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("hi", "2026-04-30T01:00:00Z"),
		responseItem("reasoning", `{"type":"reasoning","content":["thinking..."]}`, "2026-04-30T01:00:01Z"),
		responseItem("function_call_output", `{"type":"function_call_output","output":"lots of text"}`, "2026-04-30T01:00:02Z"),
		responseItem("message", `{"type":"message","role":"developer","content":[{"type":"input_text","text":"system prompt"}]}`, "2026-04-30T01:00:03Z"),
		agentMessage("answer", "2026-04-30T01:00:04Z"),
		taskComplete("answer", "2026-04-30T01:00:05Z"),
	)
	turns := mustParse(t, stream)
	if len(turns) != 1 {
		t.Fatalf("want 1 turn, got %d", len(turns))
	}
	resp := turns[0].AgentResponseFull
	if strings.Contains(resp, "thinking") || strings.Contains(resp, "lots of text") || strings.Contains(resp, "system prompt") {
		t.Errorf("noise leaked into response: %q", resp)
	}
	if !strings.Contains(resp, "answer") {
		t.Errorf("expected real answer; got %q", resp)
	}
}

func TestParse_TurnIndexesAreSequential(t *testing.T) {
	stream := join(
		sessionMeta("s", "/tmp"),
		taskStarted("2026-04-30T01:00:00Z"),
		userMessage("a", "2026-04-30T01:00:00Z"),
		agentMessage("A", "2026-04-30T01:00:01Z"),
		taskComplete("A", "2026-04-30T01:00:02Z"),
		taskStarted("2026-04-30T01:00:10Z"),
		userMessage("b", "2026-04-30T01:00:10Z"),
		agentMessage("B", "2026-04-30T01:00:11Z"),
		taskComplete("B", "2026-04-30T01:00:12Z"),
		taskStarted("2026-04-30T01:00:20Z"),
		userMessage("c", "2026-04-30T01:00:20Z"),
		agentMessage("C", "2026-04-30T01:00:21Z"),
		taskComplete("C", "2026-04-30T01:00:22Z"),
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
	turns, err := Parse(strings.NewReader(stream), "fallback")
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	out := make([]turnsT, len(turns))
	for i, tt := range turns {
		out[i] = turnsT{
			UserMessage:       tt.UserMessage,
			AgentResponseFull: tt.AgentResponseFull,
			TurnIndex:         tt.TurnIndex,
			AgentSessionID:    tt.AgentSessionID,
			ProjectPath:       tt.ProjectPath,
		}
	}
	return out
}

type turnsT struct {
	UserMessage       string
	AgentResponseFull string
	TurnIndex         int
	AgentSessionID    string
	ProjectPath       string
}

func sessionMeta(id, cwd string) string {
	return jsonline(map[string]any{
		"type":      "session_meta",
		"timestamp": "2026-04-30T00:59:50Z",
		"payload": map[string]any{
			"id":  id,
			"cwd": cwd,
		},
	})
}

func taskStarted(ts string) string {
	return jsonline(map[string]any{
		"type":      "event_msg",
		"timestamp": ts,
		"payload": map[string]any{
			"type":    "task_started",
			"turn_id": "t-" + ts,
		},
	})
}

func userMessage(text, ts string) string {
	return jsonline(map[string]any{
		"type":      "event_msg",
		"timestamp": ts,
		"payload": map[string]any{
			"type":    "user_message",
			"message": text,
		},
	})
}

func agentMessage(text, ts string) string {
	return jsonline(map[string]any{
		"type":      "event_msg",
		"timestamp": ts,
		"payload": map[string]any{
			"type":    "agent_message",
			"message": text,
		},
	})
}

func taskComplete(lastMsg, ts string) string {
	return jsonline(map[string]any{
		"type":      "event_msg",
		"timestamp": ts,
		"payload": map[string]any{
			"type":               "task_complete",
			"last_agent_message": lastMsg,
		},
	})
}

func functionCall(name, argsJSON, ts string) string {
	return jsonline(map[string]any{
		"type":      "response_item",
		"timestamp": ts,
		"payload": map[string]any{
			"type":      "function_call",
			"name":      name,
			"arguments": argsJSON,
			"call_id":   "call_" + ts,
		},
	})
}

// responseItem builds a generic response_item with the given inner payload JSON.
// Used to inject noise types we want skipped (reasoning, function_call_output, etc).
func responseItem(_ string, payloadJSON, ts string) string {
	var payload any
	_ = json.Unmarshal([]byte(payloadJSON), &payload)
	return jsonline(map[string]any{
		"type":      "response_item",
		"timestamp": ts,
		"payload":   payload,
	})
}

func jsonline(m map[string]any) string {
	b, err := json.Marshal(m)
	if err != nil {
		panic(err)
	}
	return string(b) + "\n"
}

func join(parts ...string) string { return strings.Join(parts, "") }
