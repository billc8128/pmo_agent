package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/superlion8/pmo_agent/daemon/internal/config"
	"github.com/superlion8/pmo_agent/daemon/internal/store"
	"github.com/superlion8/pmo_agent/daemon/internal/watcher"
)

// runStatus prints a snapshot of what the daemon knows: config, recent
// uploads from the local SQLite, and the discovered Claude Code
// transcript files. Doesn't talk to the network or to a running daemon.
func runStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	limit := fs.Int("n", 10, "number of recent uploads to show")
	if err := fs.Parse(args); err != nil {
		return err
	}

	// Section 0: service
	fmt.Println("─── service ───")
	if IsServiceLoaded() {
		fmt.Println("  launchd:    loaded (running in background)")
	} else {
		fmt.Println("  launchd:    not loaded — run `pmo-agent install` to run as a background service")
	}
	fmt.Println()

	// Section 1: config
	fmt.Println("─── config ───")
	cfg, err := config.Load()
	if errors.Is(err, os.ErrNotExist) {
		fmt.Println("  (no config — run `pmo-agent login` first)")
	} else if err != nil {
		fmt.Println("  error:", err)
	} else {
		p, _ := config.Path()
		fmt.Println("  file:      ", p)
		fmt.Println("  server_url:", cfg.ServerURL)
		if cfg.Token != "" {
			fmt.Printf("  token:      pmo_…%s\n", lastN(cfg.Token, 4))
		} else {
			fmt.Println("  token:      (empty)")
		}
	}
	fmt.Println()

	// Section 2: recent uploads
	fmt.Println("─── recent uploads ───")
	dbPath, dbErr := store.DefaultPath()
	if dbErr != nil {
		fmt.Println("  error locating state.db:", dbErr)
	} else {
		fmt.Println("  state.db:  ", dbPath)
		st, err := store.Open()
		if err != nil {
			fmt.Println("  error opening state.db:", err)
		} else {
			defer st.Close()
			ups, err := st.RecentUploads(*limit)
			if err != nil {
				fmt.Println("  error querying state.db:", err)
			} else if len(ups) == 0 {
				fmt.Println("  (none — daemon hasn't uploaded anything yet)")
			} else {
				for _, u := range ups {
					id := "<dedup>"
					if u.ServerTurnID != nil {
						id = fmt.Sprintf("#%d", *u.ServerTurnID)
					}
					fmt.Printf("  %s  %s  %s/%s/turn=%d\n",
						u.UploadedAt.Local().Format("2006-01-02 15:04:05"),
						id, u.Agent, shortSession(u.SessionID), u.TurnIndex)
				}
			}
		}
	}
	fmt.Println()

	// Section 3: discovered transcripts
	fmt.Println("─── discovered transcripts ───")
	root, _ := watcher.ClaudeCodeRoot()
	fmt.Println("  root:", root)
	matches, _ := filepath.Glob(filepath.Join(root, "*", "*.jsonl"))
	if len(matches) == 0 {
		fmt.Println("  (no jsonl files yet)")
	} else {
		fmt.Printf("  %d file(s) — most recent first\n", len(matches))
		// Sort by mtime DESC.
		type fileInfo struct {
			path string
			mod  time.Time
			size int64
		}
		fis := make([]fileInfo, 0, len(matches))
		for _, p := range matches {
			info, err := os.Stat(p)
			if err != nil {
				continue
			}
			fis = append(fis, fileInfo{p, info.ModTime(), info.Size()})
		}
		// simple selection sort, n is tens at most
		for i := 0; i < len(fis); i++ {
			for j := i + 1; j < len(fis); j++ {
				if fis[j].mod.After(fis[i].mod) {
					fis[i], fis[j] = fis[j], fis[i]
				}
			}
		}
		// Show up to 10
		max := 10
		if len(fis) < max {
			max = len(fis)
		}
		for _, f := range fis[:max] {
			rel, _ := filepath.Rel(root, f.path)
			fmt.Printf("  %s  %6s  %s\n",
				f.mod.Local().Format("2006-01-02 15:04:05"),
				humanSize(f.size), rel)
		}
		if len(fis) > max {
			fmt.Printf("  ... and %d more\n", len(fis)-max)
		}
	}
	return nil
}

func shortSession(s string) string {
	if i := strings.IndexByte(s, '-'); i >= 0 && i <= 8 {
		return s[:i]
	}
	if len(s) > 8 {
		return s[:8]
	}
	return s
}

func humanSize(n int64) string {
	switch {
	case n < 1024:
		return fmt.Sprintf("%dB", n)
	case n < 1024*1024:
		return fmt.Sprintf("%.0fKB", float64(n)/1024)
	case n < 1024*1024*1024:
		return fmt.Sprintf("%.1fMB", float64(n)/(1024*1024))
	default:
		return fmt.Sprintf("%.1fGB", float64(n)/(1024*1024*1024))
	}
}
