package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/config"
	"github.com/superlion8/pmo_agent/daemon/internal/store"
	"github.com/superlion8/pmo_agent/daemon/internal/uploader"
	"github.com/superlion8/pmo_agent/daemon/internal/watcher"
)

// runStart is the daemon's main loop. Foreground only for v0 (per spec
// §4.2). Ctrl-C / SIGTERM exits cleanly.
func runStart(args []string) error {
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	rootFlag := fs.String("root", "", "Claude Code transcripts root (default: ~/.claude/projects)")
	if err := fs.Parse(args); err != nil {
		return err
	}

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

	wt, err := watcher.New(*rootFlag)
	if err != nil {
		return err
	}

	cli := uploader.New(cfg.ServerURL, cfg.Token)

	// Cancel everything on Ctrl-C / SIGTERM.
	ctx, cancel := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Counters for status line.
	var sent, deduped, failed atomic.Int64
	go reportLoop(ctx, &sent, &deduped, &failed)

	// Consumer goroutine: takes turns from watcher, pushes to backend.
	doneConsume := make(chan struct{})
	go func() {
		defer close(doneConsume)
		for t := range wt.Turns() {
			outcome, err := uploadOne(ctx, st, cli, t)
			if err != nil {
				if errors.Is(err, context.Canceled) {
					return
				}
				failed.Add(1)
				fmt.Fprintln(os.Stderr, "upload:", err)
				continue
			}
			switch outcome {
			case outcomeSent:
				sent.Add(1)
				fmt.Printf("pmo-agent: + turn %d/%s/%d (%s…)\n",
					t.TurnIndex, shortID(t.AgentSessionID), len(t.UserMessage),
					oneLineStart(t.UserMessage, 50))
			case outcomeDeduped:
				deduped.Add(1)
			case outcomeAlreadyKnown:
				// silent: caught by local store, no network call
			}
		}
	}()

	// Errors channel: drain so watcher never blocks. Print non-fatal.
	go func() {
		for e := range wt.Errors() {
			fmt.Fprintln(os.Stderr, "watcher:", e)
		}
	}()

	// Print startup banner
	fmt.Printf("pmo-agent: watching transcripts (server=%s, token=pmo_…%s)\n",
		cfg.ServerURL, lastN(cfg.Token, 4))
	fmt.Println("pmo-agent: press Ctrl-C to stop")

	if err := wt.Run(ctx); err != nil {
		return fmt.Errorf("watcher: %w", err)
	}
	<-doneConsume

	fmt.Printf("\npmo-agent: stopped. uploaded=%d deduped=%d failed=%d\n",
		sent.Load(), deduped.Load(), failed.Load())

	// Local closure over the counters; must be inside this scope:
	// (intentional empty body; values already printed.)
	return nil
}

type outcome int

const (
	outcomeAlreadyKnown outcome = iota
	outcomeSent
	outcomeDeduped
)

// uploadOne checks the local store, posts the turn, and marks it.
// Returns one of outcome* on success; bubbles errors up.
func uploadOne(ctx context.Context, st *store.Store, cli *uploader.Client, t adapter.Turn) (outcome, error) {
	already, err := st.IsUploaded(t.Agent, t.AgentSessionID, t.TurnIndex)
	if err != nil {
		return outcomeAlreadyKnown, err
	}
	if already {
		return outcomeAlreadyKnown, nil
	}
	res, err := cli.Upload(ctx, t)
	if err != nil {
		return outcomeAlreadyKnown, err
	}
	if err := st.MarkUploaded(t.Agent, t.AgentSessionID, t.TurnIndex, res.TurnID); err != nil {
		return outcomeAlreadyKnown, err
	}
	if res.Deduped {
		return outcomeDeduped, nil
	}
	return outcomeSent, nil
}

func shortID(s string) string {
	if len(s) <= 8 {
		return s
	}
	return s[:8]
}

func oneLineStart(s string, n int) string {
	out := make([]rune, 0, n)
	for _, r := range s {
		if r == '\n' || r == '\r' {
			r = ' '
		}
		out = append(out, r)
		if len(out) >= n {
			break
		}
	}
	return string(out)
}

// reportLoop prints a one-line status every 10s while running.
func reportLoop(ctx context.Context, sent, deduped, failed *atomic.Int64) {
	t := time.NewTicker(10 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			fmt.Printf("pmo-agent: uploaded=%d deduped=%d failed=%d\n",
				sent.Load(), deduped.Load(), failed.Load())
		}
	}
}
