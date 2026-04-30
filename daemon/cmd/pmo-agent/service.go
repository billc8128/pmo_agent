package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// macOS launchd integration. Linux (systemd --user) is left as a
// follow-up — for MVP we ship the macOS path because that's where
// our users live.
//
// We write a LaunchAgent at:
//
//   ~/Library/LaunchAgents/com.pmo-agent.daemon.plist
//
// pointing at the absolute path of the running binary, so users
// who put pmo-agent in /usr/local/bin or anywhere else still get
// the right invocation. RunAtLoad=true starts it immediately,
// KeepAlive=true restarts on crash, and the agent is loaded into
// the user's per-user launchd domain so it follows them across
// reboots.

const launchAgentLabel = "com.pmo-agent.daemon"

func runInstall(args []string) error {
	if runtime.GOOS != "darwin" {
		return errors.New("install is currently macOS-only; on Linux/Windows, run `pmo-agent start` under systemd / NSSM yourself")
	}

	// Sanity-check that the user has a config (otherwise launchd will
	// happily crash-loop on "load config: no such file").
	if err := assertConfigExists(); err != nil {
		return err
	}

	binaryPath, err := absoluteBinaryPath()
	if err != nil {
		return err
	}

	plistPath, err := launchAgentPlistPath()
	if err != nil {
		return err
	}
	logDir, err := logDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(logDir, 0o700); err != nil {
		return fmt.Errorf("ensure log dir: %w", err)
	}
	stdoutPath := filepath.Join(logDir, "daemon.log")
	stderrPath := filepath.Join(logDir, "daemon.err.log")

	plist := buildPlist(binaryPath, stdoutPath, stderrPath)
	if err := os.WriteFile(plistPath, []byte(plist), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", plistPath, err)
	}

	uid := os.Getuid()
	domain := fmt.Sprintf("gui/%d", uid)

	// If the agent is already loaded (re-install), boot it out first.
	// `bootout` returns nonzero when the service isn't loaded, which is
	// fine for our purposes — we ignore the error.
	_ = exec.Command("launchctl", "bootout", domain, plistPath).Run()

	bootstrap := exec.Command("launchctl", "bootstrap", domain, plistPath)
	if out, err := bootstrap.CombinedOutput(); err != nil {
		return fmt.Errorf("launchctl bootstrap failed: %w (output: %s)", err, strings.TrimSpace(string(out)))
	}

	// Kick the service so it actually starts now (RunAtLoad covers the
	// next boot; this covers right now).
	_ = exec.Command("launchctl", "kickstart", "-k", domain+"/"+launchAgentLabel).Run()

	fmt.Printf("✓ Installed %s\n", plistPath)
	fmt.Printf("  binary:   %s\n", binaryPath)
	fmt.Printf("  logs:     %s\n", stdoutPath)
	fmt.Printf("  errors:   %s\n", stderrPath)
	fmt.Println()
	fmt.Println("  pmo-agent will now run in the background and restart on boot.")
	fmt.Println("  Check status with `pmo-agent status` or `tail -f " + stdoutPath + "`.")
	return nil
}

func runUninstall(_ []string) error {
	if runtime.GOOS != "darwin" {
		return errors.New("uninstall is currently macOS-only")
	}
	plistPath, err := launchAgentPlistPath()
	if err != nil {
		return err
	}
	uid := os.Getuid()
	domain := fmt.Sprintf("gui/%d", uid)

	// Unload (ignore not-loaded errors).
	_ = exec.Command("launchctl", "bootout", domain, plistPath).Run()

	// Remove the plist.
	if err := os.Remove(plistPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("remove %s: %w", plistPath, err)
	}
	fmt.Println("✓ Uninstalled. The daemon is stopped and won't restart on boot.")
	fmt.Println("  Your config (~/.pmo-agent/config.toml) and state (~/.pmo-agent/state.db) are kept.")
	fmt.Println("  Run `pmo-agent install` to bring it back, or `rm -rf ~/.pmo-agent` to wipe everything.")
	return nil
}

// IsServiceLoaded returns true if launchd reports our agent as
// currently loaded in the user's GUI domain. Used by the status
// command. Best-effort — any error returns false.
func IsServiceLoaded() bool {
	if runtime.GOOS != "darwin" {
		return false
	}
	uid := os.Getuid()
	target := fmt.Sprintf("gui/%d/%s", uid, launchAgentLabel)
	out, err := exec.Command("launchctl", "print", target).CombinedOutput()
	if err != nil {
		return false
	}
	// `launchctl print` prints "state = running" or "state = waiting" when
	// the service exists. If it doesn't exist we get an error above.
	return strings.Contains(string(out), "state = running") ||
		strings.Contains(string(out), "state = waiting")
}

func assertConfigExists() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	cfg := filepath.Join(home, ".pmo-agent", "config.toml")
	info, err := os.Stat(cfg)
	if err != nil {
		if os.IsNotExist(err) {
			return errors.New("no config found — run `pmo-agent login` first")
		}
		return err
	}
	if info.Size() == 0 {
		return errors.New("config.toml is empty — run `pmo-agent login` first")
	}
	return nil
}

func absoluteBinaryPath() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("locate self: %w", err)
	}
	abs, err := filepath.Abs(exe)
	if err != nil {
		return "", err
	}
	// Resolve any symlinks (Homebrew puts binaries behind a symlink farm
	// in /opt/homebrew/bin; launchd is fine with that, but resolving it
	// makes the plist self-documenting).
	resolved, err := filepath.EvalSymlinks(abs)
	if err != nil {
		// Symlink resolution failure is non-fatal.
		return abs, nil
	}
	return resolved, nil
}

func launchAgentPlistPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(home, "Library", "LaunchAgents")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("ensure %s: %w", dir, err)
	}
	return filepath.Join(dir, launchAgentLabel+".plist"), nil
}

func logDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".pmo-agent"), nil
}

// buildPlist returns the LaunchAgent XML. We don't escape the inputs
// because they come from os.Executable() (a real path) and our own
// log paths — none of which can contain & < > etc. on a sane macOS
// install. If you somehow have a path with these chars, the plist
// will be invalid and `launchctl` will tell you on bootstrap.
func buildPlist(binaryPath, stdoutPath, stderrPath string) string {
	const tmpl = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>` + launchAgentLabel + `</string>
    <key>ProgramArguments</key>
    <array>
        <string>%s</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
`
	return fmt.Sprintf(tmpl, binaryPath, stdoutPath, stderrPath)
}

// uidString is unused at present but kept for the eventual systemd port.
//
//nolint:unused
func uidString() string { return strconv.Itoa(os.Getuid()) }
