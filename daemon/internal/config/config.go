// Package config reads and writes ~/.pmo-agent/config.toml.
//
// File layout (mode 0600 enforced):
//
//	server_url = "https://xecnsibhijdlwqulkxor.supabase.co"
//	token      = "pmo_..."
//
// We intentionally keep this small: just enough for the daemon to know
// where to upload and how to authenticate. PAT lifecycle (issue, revoke)
// happens server-side.

package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	toml "github.com/pelletier/go-toml/v2"
)

// DefaultServerURL is the production Supabase project. Override with
// PMO_AGENT_SERVER_URL or by editing config.toml.
const DefaultServerURL = "https://xecnsibhijdlwqulkxor.supabase.co"

// Config is the on-disk shape of ~/.pmo-agent/config.toml.
type Config struct {
	ServerURL string `toml:"server_url"`
	Token     string `toml:"token"`
}

// Path returns the absolute path to the config file, creating the
// containing directory at mode 0700 if needed.
func Path() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("locate home dir: %w", err)
	}
	dir := filepath.Join(home, ".pmo-agent")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create %s: %w", dir, err)
	}
	return filepath.Join(dir, "config.toml"), nil
}

// Load reads the config from disk. Returns os.ErrNotExist (wrapped) when
// the file is missing — callers should treat this as "user hasn't run
// `pmo-agent login` yet" and print a helpful message.
func Load() (*Config, error) {
	p, err := Path()
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err // includes os.ErrNotExist on first run
	}
	var c Config
	if err := toml.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("parse %s: %w", p, err)
	}
	if c.ServerURL == "" {
		c.ServerURL = DefaultServerURL
	}
	return &c, nil
}

// Save writes the config to disk at mode 0600. The file is written
// atomically: tmp file + rename, so a crash mid-write can't leave a
// truncated config.
func Save(c *Config) error {
	if err := c.Validate(); err != nil {
		return err
	}
	p, err := Path()
	if err != nil {
		return err
	}
	b, err := toml.Marshal(c)
	if err != nil {
		return fmt.Errorf("encode toml: %w", err)
	}

	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return fmt.Errorf("write %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, p); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("rename to %s: %w", p, err)
	}
	// Belt-and-suspenders: ensure final mode is 0600 even if the tmp
	// file was created with a different umask before WriteFile applied.
	if err := os.Chmod(p, 0o600); err != nil {
		return fmt.Errorf("chmod %s: %w", p, err)
	}
	return nil
}

// Validate enforces minimum invariants. Loose: we don't try to verify
// the PAT is still live — that's the server's job at upload time.
func (c *Config) Validate() error {
	if c == nil {
		return errors.New("config is nil")
	}
	if c.ServerURL == "" {
		return errors.New("server_url is required")
	}
	if c.Token == "" {
		return errors.New("token is required (run `pmo-agent login`)")
	}
	return nil
}
