package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"golang.org/x/term"

	"github.com/superlion8/pmo_agent/daemon/internal/config"
)

// runLogin saves a PAT and server URL to ~/.pmo-agent/config.toml.
//
// Sources for the PAT, in priority order:
//  1. --token flag
//  2. PMO_AGENT_PAT environment variable
//  3. piped stdin (so `echo $PAT | pmo-agent login` works in scripts)
//  4. interactive TTY prompt with terminal echo disabled
//
// Source for the server URL, in priority order:
//  1. --server flag
//  2. PMO_AGENT_SERVER_URL environment variable
//  3. existing config.toml's server_url (preserve on re-login)
//  4. config.DefaultServerURL
func runLogin(args []string) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	tokenFlag := fs.String("token", "", "PAT (otherwise read from PMO_AGENT_PAT, stdin, or prompt)")
	serverFlag := fs.String("server", "", "server URL (default: existing config or "+config.DefaultServerURL+")")
	if err := fs.Parse(args); err != nil {
		return err
	}

	tok, err := resolveToken(*tokenFlag)
	if err != nil {
		return err
	}
	tok = strings.TrimSpace(tok)
	if tok == "" {
		return errors.New("token is empty")
	}
	if !strings.HasPrefix(tok, "pmo_") {
		// Not a hard error — server decides what's valid — but warn loudly.
		fmt.Fprintln(os.Stderr, "warning: token does not start with \"pmo_\"; "+
			"continuing, but the server will reject it if the format is wrong")
	}

	server := resolveServer(*serverFlag)

	c := &config.Config{ServerURL: server, Token: tok}
	if err := config.Save(c); err != nil {
		return err
	}

	p, _ := config.Path()
	fmt.Printf("Saved %s (server_url=%s, token=pmo_…%s)\n",
		p, server, lastN(tok, 4))
	return nil
}

func resolveToken(flagVal string) (string, error) {
	if flagVal != "" {
		return flagVal, nil
	}
	if v := os.Getenv("PMO_AGENT_PAT"); v != "" {
		return v, nil
	}
	// Stdin: only consume if it's a pipe, not a terminal — otherwise we'd
	// silently swallow user keystrokes meant for the prompt.
	if !isTerminal(os.Stdin.Fd()) {
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return "", fmt.Errorf("read stdin: %w", err)
		}
		return string(b), nil
	}
	// Interactive prompt.
	fmt.Fprint(os.Stderr, "PAT: ")
	b, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stderr) // ReadPassword swallows the newline
	if err != nil {
		return "", fmt.Errorf("read PAT from terminal: %w", err)
	}
	return string(b), nil
}

func resolveServer(flagVal string) string {
	if flagVal != "" {
		return flagVal
	}
	if v := os.Getenv("PMO_AGENT_SERVER_URL"); v != "" {
		return v
	}
	if existing, err := config.Load(); err == nil && existing.ServerURL != "" {
		return existing.ServerURL
	}
	return config.DefaultServerURL
}

func isTerminal(fd uintptr) bool { return term.IsTerminal(int(fd)) }

func lastN(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}
