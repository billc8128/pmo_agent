package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReleaseAssetName(t *testing.T) {
	tests := []struct {
		name   string
		goos   string
		goarch string
		want   string
	}{
		{name: "mac arm64", goos: "darwin", goarch: "arm64", want: "pmo-agent-darwin-arm64"},
		{name: "mac amd64", goos: "darwin", goarch: "amd64", want: "pmo-agent-darwin-amd64"},
		{name: "linux arm64", goos: "linux", goarch: "arm64", want: "pmo-agent-linux-arm64"},
		{name: "linux amd64", goos: "linux", goarch: "amd64", want: "pmo-agent-linux-amd64"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := releaseAssetName(tt.goos, tt.goarch)
			if err != nil {
				t.Fatalf("releaseAssetName returned error: %v", err)
			}
			if got != tt.want {
				t.Fatalf("releaseAssetName() = %q, want %q", got, tt.want)
			}
		})
	}

	if _, err := releaseAssetName("freebsd", "amd64"); err == nil {
		t.Fatal("releaseAssetName unsupported platform error = nil, want error")
	}
}

func TestLatestVersionFromURL(t *testing.T) {
	got, err := latestVersionFromURL("https://github.com/billc8128/pmo_agent/releases/tag/v0.1.2")
	if err != nil {
		t.Fatalf("latestVersionFromURL returned error: %v", err)
	}
	if got != "v0.1.2" {
		t.Fatalf("latestVersionFromURL() = %q, want %q", got, "v0.1.2")
	}

	if _, err := latestVersionFromURL("https://github.com/billc8128/pmo_agent/releases/latest"); err == nil {
		t.Fatal("latestVersionFromURL without tag error = nil, want error")
	}
}

func TestIsNewerVersion(t *testing.T) {
	tests := []struct {
		name    string
		latest  string
		current string
		want    bool
	}{
		{name: "new patch", latest: "v0.1.2", current: "v0.1.1", want: true},
		{name: "same version", latest: "v0.1.2", current: "v0.1.2", want: false},
		{name: "current newer", latest: "v0.1.2", current: "v0.1.3", want: false},
		{name: "numeric compare", latest: "v0.10.0", current: "v0.2.0", want: true},
		{name: "dev installs release", latest: "v1.0.0", current: "dev", want: true},
		{name: "empty installs release", latest: "v1.0.0", current: "", want: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := isNewerVersion(tt.latest, tt.current)
			if err != nil {
				t.Fatalf("isNewerVersion returned error: %v", err)
			}
			if got != tt.want {
				t.Fatalf("isNewerVersion() = %v, want %v", got, tt.want)
			}
		})
	}

	if _, err := isNewerVersion("not-a-version", "v0.1.1"); err == nil {
		t.Fatal("isNewerVersion invalid latest error = nil, want error")
	}
}

func TestInstallDownloadedBinary(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "pmo-agent")
	src := filepath.Join(dir, "download")

	if err := os.WriteFile(target, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(src, []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := installDownloadedBinary(src, target); err != nil {
		t.Fatalf("installDownloadedBinary returned error: %v", err)
	}

	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new" {
		t.Fatalf("target content = %q, want %q", string(got), "new")
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o111 == 0 {
		t.Fatalf("target mode = %v, want executable bit", info.Mode().Perm())
	}
	if _, err := os.Stat(src); !os.IsNotExist(err) {
		t.Fatalf("source still exists or stat failed with unexpected error: %v", err)
	}
}
