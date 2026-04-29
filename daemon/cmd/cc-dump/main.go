// cc-dump is a one-off helper for Milestone 1.3 verification: parse a
// real Claude Code JSONL and print every detected turn. NOT shipped with
// the daemon; deleted before commit.

package main

import (
	"fmt"
	"os"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter/claudecode"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: cc-dump <path-to-jsonl>")
		os.Exit(2)
	}
	turns, err := claudecode.ParseFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	fmt.Printf("Parsed %d turn(s)\n\n", len(turns))
	for i, t := range turns {
		fmt.Printf("─── Turn #%d ─── session=%s cwd=%s\n", i, short(t.AgentSessionID, 8), t.ProjectPath)
		fmt.Printf("  user_at: %s | resp_at: %s\n", t.UserMessageAt.Format("15:04:05"), t.AgentResponseAt.Format("15:04:05"))
		fmt.Printf("  USER  (%d chars): %s\n", len(t.UserMessage), oneLine(t.UserMessage, 100))
		fmt.Printf("  AGENT (%d chars): %s\n", len(t.AgentResponseFull), oneLine(t.AgentResponseFull, 200))
		fmt.Println()
	}
}

func short(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func oneLine(s string, n int) string {
	out := make([]rune, 0, n)
	for _, r := range s {
		if r == '\n' || r == '\r' {
			r = ' '
		}
		out = append(out, r)
		if len(out) >= n {
			out = append(out, '…')
			break
		}
	}
	return string(out)
}
