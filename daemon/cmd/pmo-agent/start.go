package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/adapter"
	"github.com/superlion8/pmo_agent/daemon/internal/adapter/claudecode"
	"github.com/superlion8/pmo_agent/daemon/internal/adapter/codex"
	"github.com/superlion8/pmo_agent/daemon/internal/config"
	"github.com/superlion8/pmo_agent/daemon/internal/notify"
	"github.com/superlion8/pmo_agent/daemon/internal/projectroot"
	"github.com/superlion8/pmo_agent/daemon/internal/rawtranscript"
	"github.com/superlion8/pmo_agent/daemon/internal/store"
	"github.com/superlion8/pmo_agent/daemon/internal/uploader"
	"github.com/superlion8/pmo_agent/daemon/internal/watcher"
)

const (
	rawTranscriptScanInterval       = 30 * time.Second
	rawTranscriptQuietFor           = 20 * time.Second
	maxRawTranscriptCompressedBytes = 50 * 1024 * 1024
)

// runStart is the daemon's main loop. Foreground only for v0 (per spec
// §4.2). Ctrl-C / SIGTERM exits cleanly.
//
// Two watchers run in parallel: one for Claude Code transcripts at
// ~/.claude/projects/ and one for Codex at ~/.codex/sessions/. Their
// Turn channels are merged into a single uploader pipeline.
func runStart(args []string) error {
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	ccRootFlag := fs.String("cc-root", "", "Claude Code transcripts root (default: ~/.claude/projects)")
	cxRootFlag := fs.String("codex-root", "", "Codex transcripts root (default: ~/.codex/sessions)")
	uploadRawJSONL := fs.Bool("upload-raw-jsonl", rawJSONLDefaultEnabled(), "upload gzip-compressed raw JSONL snapshots for debugging/search")
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

	// Resolve roots, falling back to canonical paths.
	ccRoot := *ccRootFlag
	if ccRoot == "" {
		if r, err := watcher.ClaudeCodeRoot(); err == nil {
			ccRoot = r
		}
	}
	cxRoot := *cxRootFlag
	if cxRoot == "" {
		if r, err := watcher.CodexRoot(); err == nil {
			cxRoot = r
		}
	}

	ccWatcher, err := watcher.New("claude_code", ccRoot, claudecode.ParseFile)
	if err != nil {
		return fmt.Errorf("claude_code watcher: %w", err)
	}
	cxWatcher, err := watcher.New("codex", cxRoot, codex.ParseFile)
	if err != nil {
		return fmt.Errorf("codex watcher: %w", err)
	}

	cli := uploader.New(cfg.ServerURL, cfg.Token)
	rawCli := rawtranscript.NewClient(cfg.ServerURL, cfg.Token)

	// Cancel everything on Ctrl-C / SIGTERM.
	ctx, cancel := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Counters for status line.
	var sent, deduped, failed atomic.Int64
	go reportLoop(ctx, &sent, &deduped, &failed)

	// Run both watchers in parallel.
	var watcherWG sync.WaitGroup
	watcherWG.Add(2)
	go func() {
		defer watcherWG.Done()
		if err := ccWatcher.Run(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "claude_code watcher:", err)
		}
	}()
	go func() {
		defer watcherWG.Done()
		if err := cxWatcher.Run(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "codex watcher:", err)
		}
	}()

	// Merge both watchers' Turns into a single channel for the uploader.
	merged := mergeTurns(ccWatcher.Turns(), cxWatcher.Turns())

	// Drain non-fatal errors from both watchers.
	go drainErrors("claude_code", ccWatcher.Errors())
	go drainErrors("codex", cxWatcher.Errors())

	if *uploadRawJSONL {
		go runRawTranscriptUploadLoop(ctx, st, rawCli, []rawTranscriptSource{
			{agent: rawtranscript.AgentClaudeCode, root: ccRoot},
			{agent: rawtranscript.AgentCodex, root: cxRoot},
		})
	}

	// Consumer: takes turns, pushes to backend.
	doneConsume := make(chan struct{})
	go func() {
		defer close(doneConsume)
		for t := range merged {
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
				fmt.Printf("pmo-agent: + %s turn %d/%s (%d chars: %s…)\n",
					t.Agent, t.TurnIndex, shortID(t.AgentSessionID), len(t.UserMessage),
					oneLineStart(t.UserMessage, 50))
				notify.UploadProgress(1)
			case outcomeDeduped:
				deduped.Add(1)
			case outcomeAlreadyKnown:
				// silent: caught by local store, no network call
			}
		}
	}()

	// Print startup banner
	fmt.Printf("pmo-agent: watching transcripts (server=%s, token=pmo_…%s)\n",
		cfg.ServerURL, lastN(cfg.Token, 4))
	fmt.Printf("pmo-agent:   claude_code root=%s\n", ccRoot)
	fmt.Printf("pmo-agent:   codex       root=%s\n", cxRoot)
	if *uploadRawJSONL {
		fmt.Println("pmo-agent:   raw JSONL upload=enabled")
	} else {
		fmt.Println("pmo-agent:   raw JSONL upload=disabled")
	}
	fmt.Println("pmo-agent: press Ctrl-C to stop")

	// Visible "we're up" feedback. Especially valuable when the daemon
	// runs under launchd (no terminal output is ever seen).
	notify.StartedListening()

	// Block until both watchers finish (which they will when ctx is canceled).
	watcherWG.Wait()
	<-doneConsume

	fmt.Printf("\npmo-agent: stopped. uploaded=%d deduped=%d failed=%d\n",
		sent.Load(), deduped.Load(), failed.Load())

	return nil
}

func rawJSONLDefaultEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("PMO_AGENT_UPLOAD_RAW_JSONL")))
	return v != "0" && v != "false" && v != "no"
}

type rawTranscriptSource struct {
	agent string
	root  string
}

func runRawTranscriptUploadLoop(
	ctx context.Context,
	st *store.Store,
	cli *rawtranscript.Client,
	sources []rawTranscriptSource,
) {
	timer := time.NewTimer(5 * time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			for _, src := range sources {
				uploadReadyRawTranscripts(ctx, st, cli, src, time.Now())
			}
			timer.Reset(rawTranscriptScanInterval)
		}
	}
}

func uploadReadyRawTranscripts(
	ctx context.Context,
	st *store.Store,
	cli *rawtranscript.Client,
	src rawTranscriptSource,
	now time.Time,
) {
	for _, path := range rawtranscript.ReadyJSONLFiles(src.root, rawTranscriptQuietFor, now) {
		info, err := os.Stat(path)
		if err != nil {
			fmt.Fprintln(os.Stderr, "raw transcript:", err)
			continue
		}
		if info.Size() == 0 {
			continue
		}
		state, ok, err := st.TranscriptPathState(src.agent, path)
		if err != nil {
			fmt.Fprintln(os.Stderr, "raw transcript:", err)
			continue
		}
		if ok && state.ByteSize == info.Size() && state.LocalMTime == info.ModTime().UTC().Format(time.RFC3339Nano) {
			continue
		}
		snap, err := rawtranscript.BuildSnapshot(src.agent, path)
		if err != nil {
			fmt.Fprintln(os.Stderr, "raw transcript:", err)
			continue
		}
		if snap.CompressedSize > maxRawTranscriptCompressedBytes {
			fmt.Fprintf(os.Stderr, "raw transcript: skip %s: compressed size %s exceeds 50MB\n", path, humanSize(snap.CompressedSize))
			continue
		}
		prevSHA, ok, err := st.TranscriptSHA(snap.Agent, snap.AgentSessionID)
		if err != nil {
			fmt.Fprintln(os.Stderr, "raw transcript:", err)
			continue
		}
		if ok && prevSHA == snap.SHA256 {
			if err := st.MarkTranscriptUploaded(
				snap.Agent,
				snap.AgentSessionID,
				snap.LocalPath,
				snap.SHA256,
				snap.ByteSize,
				snap.CompressedSize,
				snap.LastMTime,
			); err != nil {
				fmt.Fprintln(os.Stderr, "raw transcript:", err)
			}
			continue
		}
		if _, err := cli.Upload(ctx, snap); err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			fmt.Fprintln(os.Stderr, "raw transcript upload:", err)
			continue
		}
		if err := st.MarkTranscriptUploaded(
			snap.Agent,
			snap.AgentSessionID,
			snap.LocalPath,
			snap.SHA256,
			snap.ByteSize,
			snap.CompressedSize,
			snap.LastMTime,
		); err != nil {
			fmt.Fprintln(os.Stderr, "raw transcript:", err)
			continue
		}
		fmt.Printf("pmo-agent: raw %s session %s uploaded (%s gz)\n",
			snap.Agent, shortID(snap.AgentSessionID), humanSize(snap.CompressedSize))
	}
}

// mergeTurns fan-ins two Turn channels into one. The merged channel
// closes when both inputs have closed.
func mergeTurns(ins ...<-chan adapter.Turn) <-chan adapter.Turn {
	out := make(chan adapter.Turn, 1024)
	var wg sync.WaitGroup
	wg.Add(len(ins))
	for _, ch := range ins {
		ch := ch
		go func() {
			defer wg.Done()
			for t := range ch {
				out <- t
			}
		}()
	}
	go func() {
		wg.Wait()
		close(out)
	}()
	return out
}

// drainErrors consumes a watcher's error channel, printing each error
// with a label so multiplexed logs are readable.
func drainErrors(label string, ch <-chan error) {
	for e := range ch {
		fmt.Fprintf(os.Stderr, "%s watcher: %v\n", label, e)
	}
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
	if t.ProjectRoot == "" {
		t.ProjectRoot = projectroot.Resolve(t.ProjectPath)
	}
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
