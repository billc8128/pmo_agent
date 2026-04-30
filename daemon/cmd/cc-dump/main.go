// cc-dump is a developer tool for the daemon's offline path.
//
// Usage:
//
//	cc-dump <path-to-jsonl>             # parse and print
//	cc-dump -upload <path-to-jsonl>     # parse, upload to backend, mark in state.db
//
// Behavior in -upload mode:
//   - Reads ~/.pmo-agent/config.toml for server + token (run `pmo-agent
//     login` first if missing).
//   - Opens ~/.pmo-agent/state.db.
//   - For each parsed turn, skips if already in state.db, otherwise
//     POSTs to /functions/v1/ingest, then marks uploaded.
//   - Does NOT watch for new entries; that's `pmo-agent start` (1.5).
//
// This binary is the dogfood tool for Milestone 1.4. The same wiring
// graduates into the daemon's run loop in 1.5.

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/adapter/claudecode"
	"github.com/superlion8/pmo_agent/daemon/internal/adapter/codex"
	"github.com/superlion8/pmo_agent/daemon/internal/config"
	"github.com/superlion8/pmo_agent/daemon/internal/store"
	"github.com/superlion8/pmo_agent/daemon/internal/uploader"
)

func main() {
	upload := flag.Bool("upload", false, "upload turns to the backend (default: print only)")
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: cc-dump [-upload] <path-to-jsonl>")
	}
	flag.Parse()
	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(2)
	}
	path := flag.Arg(0)

	parser := pickParser(path)
	turns, err := parser(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "parse:", err)
		os.Exit(1)
	}
	fmt.Printf("Parsed %d turn(s) from %s\n\n", len(turns), path)

	if !*upload {
		printTurns(turns)
		return
	}

	if err := runUpload(turns); err != nil {
		fmt.Fprintln(os.Stderr, "upload:", err)
		os.Exit(1)
	}
}

func runUpload(turns []adapter.Turn) error {
	cfg, err := config.Load()
	if err != nil {
		return fmt.Errorf("load config (run `pmo-agent login` first?): %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return err
	}

	st, err := store.Open()
	if err != nil {
		return fmt.Errorf("open state: %w", err)
	}
	defer st.Close()

	cli := uploader.New(cfg.ServerURL, cfg.Token)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	var sent, deduped, skipped int
	for _, t := range turns {
		already, err := st.IsUploaded(t.Agent, t.AgentSessionID, t.TurnIndex)
		if err != nil {
			return err
		}
		if already {
			skipped++
			continue
		}

		res, err := cli.Upload(ctx, t)
		if err != nil {
			// Permanent: stop and surface. Transient: stop too — for the
			// dev tool, simpler is better; the daemon (1.5) will retry.
			if errors.Is(err, uploader.ErrPermanent) {
				return fmt.Errorf("permanent failure on turn[%d]: %w", t.TurnIndex, err)
			}
			return fmt.Errorf("transient failure on turn[%d]: %w", t.TurnIndex, err)
		}
		if err := st.MarkUploaded(t.Agent, t.AgentSessionID, t.TurnIndex, res.TurnID); err != nil {
			return fmt.Errorf("mark uploaded: %w", err)
		}
		if res.Deduped {
			deduped++
		} else {
			sent++
		}
		fmt.Printf("  turn[%d] %s deduped=%v server_id=%s\n",
			t.TurnIndex, oneLine(t.UserMessage, 60), res.Deduped, ptrIntStr(res.TurnID))
	}
	fmt.Printf("\nDone: %d sent, %d deduped, %d already-known.\n", sent, deduped, skipped)
	return nil
}

func printTurns(turns []adapter.Turn) {
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

func ptrIntStr(p *int64) string {
	if p == nil {
		return "<dedup>"
	}
	return fmt.Sprintf("%d", *p)
}

// pickParser routes a path to the right adapter based on filename hint.
// Codex jsonl filenames contain "rollout-" and live under .codex/sessions;
// everything else defaults to claudecode.
func pickParser(path string) func(string) ([]adapter.Turn, error) {
	if strings.Contains(path, ".codex/sessions") || strings.Contains(path, "rollout-") {
		return codex.ParseFile
	}
	return claudecode.ParseFile
}
