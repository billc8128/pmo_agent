package main

import (
	"context"
	"crypto/rand"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"golang.org/x/term"

	"github.com/superlion8/pmo_agent/daemon/internal/config"
)

// runLogin saves a PAT and server URL to ~/.pmo-agent/config.toml.
//
// Default flow (no flags): browser-based CLI auth.
//   1. Generate a 4-character nonce.
//   2. Start a localhost HTTP server on an OS-picked port.
//   3. Open a browser to <server>/cli-auth?session=NONCE&redirect=
//      http://127.0.0.1:PORT/callback&label=HOSTNAME.
//   4. Wait for the callback to deliver the plaintext token.
//   5. Verify the session nonce matches what we generated.
//   6. Write config.toml.
//
// Fallback paths (kept for CI / no-browser environments):
//   --token <pat>            explicit
//   PMO_AGENT_PAT=...        env var
//   piped stdin              `echo $PAT | pmo-agent login --no-browser`
//   --no-browser             force the prompt path
func runLogin(args []string) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	tokenFlag := fs.String("token", "", "PAT (skips browser flow)")
	serverFlag := fs.String("server", "", "server URL (default: existing config or "+config.DefaultServerURL+")")
	noBrowser := fs.Bool("no-browser", false, "skip the browser auth flow; use --token, env, stdin, or prompt instead")
	labelFlag := fs.String("label", "", "label for the new daemon token (default: hostname)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	server := resolveServer(*serverFlag)

	// Token sources, in order of priority.
	if *tokenFlag != "" {
		return saveAndReport(server, *tokenFlag)
	}
	if v := os.Getenv("PMO_AGENT_PAT"); v != "" {
		return saveAndReport(server, v)
	}
	if !isTerminal(os.Stdin.Fd()) {
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return fmt.Errorf("read stdin: %w", err)
		}
		return saveAndReport(server, string(b))
	}
	if *noBrowser {
		fmt.Fprint(os.Stderr, "PAT: ")
		b, err := term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Fprintln(os.Stderr)
		if err != nil {
			return fmt.Errorf("read PAT from terminal: %w", err)
		}
		return saveAndReport(server, string(b))
	}

	// Default: browser flow.
	tok, err := browserLogin(server, *labelFlag)
	if err != nil {
		return err
	}
	return saveAndReport(server, tok)
}

func saveAndReport(server, rawToken string) error {
	tok := strings.TrimSpace(rawToken)
	if tok == "" {
		return errors.New("token is empty")
	}
	if !strings.HasPrefix(tok, "pmo_") {
		fmt.Fprintln(os.Stderr, "warning: token does not start with \"pmo_\"; "+
			"continuing, but the server will reject it if the format is wrong")
	}
	c := &config.Config{ServerURL: server, Token: tok}
	if err := config.Save(c); err != nil {
		return err
	}
	p, _ := config.Path()
	fmt.Printf("Saved %s (server_url=%s, token=pmo_…%s)\n",
		p, server, lastN(tok, 4))
	return nil
}

// browserLogin runs the full CLI auth dance and returns the plaintext
// token on success.
func browserLogin(serverURL, labelOverride string) (string, error) {
	nonce, err := generateNonce()
	if err != nil {
		return "", fmt.Errorf("generate nonce: %w", err)
	}

	// Pick a localhost port.
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", fmt.Errorf("listen on loopback: %w", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	redirectURL := fmt.Sprintf("http://127.0.0.1:%d/callback", port)

	label := labelOverride
	if label == "" {
		label = defaultLabel()
	}

	// Build the auth URL.
	authURL := fmt.Sprintf(
		"%s/cli-auth?session=%s&redirect=%s&label=%s",
		strings.TrimRight(serverURL, "/"),
		url.QueryEscape(nonce),
		url.QueryEscape(redirectURL),
		url.QueryEscape(label),
	)

	// Channel that receives the result of the callback.
	type cbResult struct {
		token string
		err   error
	}
	resultCh := make(chan cbResult, 1)

	mux := http.NewServeMux()
	mux.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		// Verify session matches before accepting the token. This
		// closes the loop opened in the terminal: a stale tab from a
		// previous login attempt can't smuggle in an old token.
		gotSession := r.URL.Query().Get("session")
		gotToken := r.URL.Query().Get("token")
		if gotSession != nonce {
			http.Error(w, "session mismatch", http.StatusBadRequest)
			resultCh <- cbResult{err: fmt.Errorf("session mismatch (got %q, want %q)", gotSession, nonce)}
			return
		}
		if !strings.HasPrefix(gotToken, "pmo_") {
			http.Error(w, "missing or malformed token", http.StatusBadRequest)
			resultCh <- cbResult{err: errors.New("missing token in callback")}
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, successHTML)
		resultCh <- cbResult{token: gotToken}
	})

	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(listener) }()
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	fmt.Println("Opening browser to authorize this daemon…")
	fmt.Printf("If it doesn't open automatically, visit:\n  %s\n\n", authURL)
	fmt.Printf("Verify this code in the browser matches:  %s\n\n", nonce)
	if err := openBrowser(authURL); err != nil {
		fmt.Fprintln(os.Stderr, "warning: couldn't open browser automatically:", err)
	}

	select {
	case r := <-resultCh:
		if r.err != nil {
			return "", r.err
		}
		fmt.Println("✓ Authorized.")
		return r.token, nil
	case <-time.After(5 * time.Minute):
		return "", errors.New("timed out waiting for browser authorization (5 minutes)")
	}
}

// generateNonce returns 4 characters from a Crockford-base32 alphabet
// (excluding 0, 1, O, I to make it terminal/visual readable).
func generateNonce() (string, error) {
	const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" // 32 chars, no I/L/O/0/1
	var buf [4]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return "", err
	}
	out := make([]byte, 4)
	for i, b := range buf {
		out[i] = alphabet[int(b)%len(alphabet)]
	}
	return string(out), nil
}

// defaultLabel picks a sensible per-machine label. Hostname is the
// best portable cue; we strip the trailing ".local" macOS adds.
func defaultLabel() string {
	if h, err := os.Hostname(); err == nil && h != "" {
		h = strings.TrimSuffix(h, ".local")
		// Reject pathological labels.
		if h != "" && len(h) <= 64 {
			return h
		}
	}
	return "daemon"
}

// openBrowser is the cross-platform "open this URL in the user's
// default browser" helper.
func openBrowser(url string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	return cmd.Start()
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

// successHTML is shown in the browser after the daemon has accepted
// the token. We keep it self-contained (no external CSS / JS) so the
// loopback server is genuinely zero-dependency.
const successHTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pmo_agent — authorized</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif;
         max-width: 28rem; margin: 4rem auto; padding: 0 1rem;
         color: #18181b; }
  h1 { font-size: 1.5rem; }
  p  { color: #52525b; line-height: 1.5; }
  code { background: #f4f4f5; padding: 0.1rem 0.3rem; border-radius: 0.25rem;
         font-family: ui-monospace, monospace; font-size: 0.95em; }
</style>
</head>
<body>
  <h1>✓ Authorized</h1>
  <p>The daemon on your machine now has a token. You can close this
     tab and return to your terminal.</p>
</body>
</html>`
