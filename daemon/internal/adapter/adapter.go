// Package adapter defines the Turn type that all agent-specific watchers
// emit. Per spec §4.6.

package adapter

import "time"

// Turn is one (user_message, agent_response) pair, ready to upload.
//
// Field meanings mirror columns in public.turns. The values reaching the
// uploader have already been redacted; the adapter is responsible for
// passing user-facing text through the redact package before placing it
// in this struct.
type Turn struct {
	// Agent is "claude_code" or "codex".
	Agent string

	// AgentSessionID is the native session id from the agent. For Claude
	// Code that's the UUID portion of the .jsonl filename. UI grouping
	// only — no active/closed semantics.
	AgentSessionID string

	// ProjectPath is the cwd where the agent was running, when known.
	ProjectPath string

	// TurnIndex is the 0-based position of this turn within its session.
	TurnIndex int

	// UserMessage is the user's prompt text, redacted.
	UserMessage string

	// AgentResponseFull is the agent's reply, redacted, with tool_use
	// blocks rendered as one-line summaries and tool_result blocks
	// dropped entirely.
	AgentResponseFull string

	// UserMessageAt is the timestamp on the user's prompt entry.
	UserMessageAt time.Time

	// AgentResponseAt is the timestamp of the final assistant entry that
	// closed the turn. Zero if not yet closed.
	AgentResponseAt time.Time
}
