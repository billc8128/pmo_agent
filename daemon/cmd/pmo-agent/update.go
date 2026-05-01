package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	releaseRepo      = "billc8128/pmo_agent"
	installScriptURL = "https://pmo-agent-sigma.vercel.app/install.sh"
)

var updateHTTPClient = &http.Client{Timeout: 60 * time.Second}

func runUpdate(args []string) error {
	fs := flag.NewFlagSet("update", flag.ContinueOnError)
	checkOnly := fs.Bool("check", false, "check for an available update without installing it")
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), `Usage:
  pmo-agent update [--check]

Checks the latest GitHub release and installs the matching pmo-agent binary.
`)
	}
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}

	fmt.Printf("Current version: %s\n", version)
	latest, err := fetchLatestVersion(updateHTTPClient, releaseRepo)
	if err != nil {
		return err
	}
	fmt.Printf("Latest version:  %s\n", latest)

	newer, err := isNewerVersion(latest, version)
	if err != nil {
		return err
	}
	if !newer {
		fmt.Printf("pmo-agent %s is already up to date.\n", version)
		return nil
	}
	if *checkOnly {
		fmt.Printf("Update available: %s -> %s\n", version, latest)
		return nil
	}

	asset, err := releaseAssetName(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return err
	}
	binaryPath, err := absoluteBinaryPath()
	if err != nil {
		return err
	}

	tmpPath, err := downloadUpdate(updateHTTPClient, releaseRepo, latest, asset, filepath.Dir(binaryPath))
	if err != nil {
		return formatUpdateWriteError(binaryPath, err)
	}
	removeTmp := true
	defer func() {
		if removeTmp {
			_ = os.Remove(tmpPath)
		}
	}()

	if err := installDownloadedBinary(tmpPath, binaryPath); err != nil {
		return formatUpdateWriteError(binaryPath, err)
	}
	removeTmp = false

	fmt.Printf("Installed pmo-agent %s at %s\n", latest, binaryPath)
	if runtime.GOOS == "darwin" && IsServiceLoaded() {
		fmt.Println("Restarting background service...")
		if err := runInstall(nil); err != nil {
			return fmt.Errorf("updated binary, but could not restart background service: %w", err)
		}
	}
	return nil
}

func releaseAssetName(goos, goarch string) (string, error) {
	switch goos {
	case "darwin", "linux":
	default:
		return "", fmt.Errorf("unsupported OS %q; install from source at https://github.com/%s", goos, releaseRepo)
	}
	switch goarch {
	case "amd64", "arm64":
	default:
		return "", fmt.Errorf("unsupported architecture %q; install from source at https://github.com/%s", goarch, releaseRepo)
	}
	return fmt.Sprintf("pmo-agent-%s-%s", goos, goarch), nil
}

func fetchLatestVersion(client *http.Client, repo string) (string, error) {
	req, err := http.NewRequest(http.MethodGet, fmt.Sprintf("https://github.com/%s/releases/latest", repo), nil)
	if err != nil {
		return "", err
	}
	setUpdateRequestHeaders(req)

	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("look up latest release: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 400 {
		return "", fmt.Errorf("look up latest release: GitHub returned %s", resp.Status)
	}
	version, err := latestVersionFromURL(resp.Request.URL.String())
	if err != nil {
		return "", err
	}
	return version, nil
}

func latestVersionFromURL(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("parse latest release URL: %w", err)
	}
	cleanPath := strings.TrimSuffix(path.Clean(u.Path), "/")
	if !strings.Contains(cleanPath, "/releases/tag/") {
		return "", fmt.Errorf("could not determine latest version from %s", raw)
	}
	tag := path.Base(cleanPath)
	if tag == "." || tag == "/" || tag == "tag" || strings.TrimSpace(tag) == "" {
		return "", fmt.Errorf("could not determine latest version from %s", raw)
	}
	return tag, nil
}

func isNewerVersion(latest, current string) (bool, error) {
	latestParts, err := parseReleaseVersion(latest)
	if err != nil {
		return false, fmt.Errorf("latest version %q is not a release version: %w", latest, err)
	}
	currentParts, err := parseReleaseVersion(current)
	if err != nil {
		return true, nil
	}
	for i := range latestParts {
		if latestParts[i] > currentParts[i] {
			return true, nil
		}
		if latestParts[i] < currentParts[i] {
			return false, nil
		}
	}
	return false, nil
}

func parseReleaseVersion(v string) ([3]int, error) {
	var out [3]int
	v = strings.TrimSpace(v)
	v = strings.TrimPrefix(v, "pmo-agent")
	v = strings.TrimSpace(v)
	v = strings.TrimPrefix(v, "v")
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	parts := strings.Split(v, ".")
	if len(parts) != 3 {
		return out, errors.New("expected vMAJOR.MINOR.PATCH")
	}
	for i, part := range parts {
		n, err := strconv.Atoi(part)
		if err != nil {
			return out, err
		}
		out[i] = n
	}
	return out, nil
}

func downloadUpdate(client *http.Client, repo, version, asset, dir string) (string, error) {
	tmp, err := os.CreateTemp(dir, ".pmo-agent-update-*")
	if err != nil {
		return "", fmt.Errorf("create temporary update file: %w", err)
	}
	tmpPath := tmp.Name()
	closeAndRemove := true
	defer func() {
		_ = tmp.Close()
		if closeAndRemove {
			_ = os.Remove(tmpPath)
		}
	}()

	downloadURL := fmt.Sprintf(
		"https://github.com/%s/releases/download/%s/%s",
		repo,
		url.PathEscape(version),
		url.PathEscape(asset),
	)
	fmt.Printf("Downloading %s...\n", asset)
	if err := downloadToWriter(client, downloadURL, tmp); err != nil {
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", fmt.Errorf("write temporary update file: %w", err)
	}
	closeAndRemove = false
	return tmpPath, nil
}

func downloadToWriter(client *http.Client, downloadURL string, w io.Writer) error {
	req, err := http.NewRequest(http.MethodGet, downloadURL, nil)
	if err != nil {
		return err
	}
	setUpdateRequestHeaders(req)

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("download update: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download update: GitHub returned %s for %s", resp.Status, downloadURL)
	}
	if _, err := io.Copy(w, resp.Body); err != nil {
		return fmt.Errorf("download update: %w", err)
	}
	return nil
}

func installDownloadedBinary(src, target string) error {
	mode := os.FileMode(0o755)
	info, err := os.Stat(target)
	if err == nil {
		mode = info.Mode().Perm() | 0o111
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("stat %s: %w", target, err)
	}
	if err := os.Chmod(src, mode); err != nil {
		return fmt.Errorf("mark update executable: %w", err)
	}
	if err := os.Rename(src, target); err != nil {
		return fmt.Errorf("replace %s: %w", target, err)
	}
	return nil
}

func setUpdateRequestHeaders(req *http.Request) {
	req.Header.Set("User-Agent", "pmo-agent/"+version)
	req.Header.Set("Accept", "application/octet-stream")
}

func formatUpdateWriteError(target string, err error) error {
	if !os.IsPermission(err) {
		return err
	}
	return fmt.Errorf(`cannot update %s: %w

This usually means the install path is not writable by your user.
Re-run the installer instead:

  curl -fsSL %s | bash`, target, err, installScriptURL)
}
