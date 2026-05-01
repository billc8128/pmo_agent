// Package projectroot resolves a raw agent cwd into the canonical
// project key used by the backend and UI.
package projectroot

import (
	"os/exec"
	"path/filepath"
	"strings"
)

const claudeWorktreeMarker = string(filepath.Separator) + ".claude" + string(filepath.Separator) + "worktrees" + string(filepath.Separator)

// Resolve returns the best project root for cwd. Git roots are the
// project boundary; outside git, the cleaned cwd is the fallback.
func Resolve(cwd string) string {
	if cwd == "" {
		return ""
	}
	clean := filepath.Clean(cwd)
	if repo, ok := originalRepoFromClaudeWorktree(clean); ok {
		if gitRoot := gitRoot(repo); gitRoot != "" {
			return gitRoot
		}
		return repo
	}
	if gitRoot := gitRoot(clean); gitRoot != "" {
		return gitRoot
	}
	return clean
}

func originalRepoFromClaudeWorktree(path string) (string, bool) {
	i := strings.Index(path, claudeWorktreeMarker)
	if i < 0 {
		return "", false
	}
	return path[:i], true
}

func gitRoot(dir string) string {
	cmd := exec.Command("git", "-C", dir, "rev-parse", "--show-toplevel")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	root := strings.TrimSpace(string(out))
	if root == "" {
		return ""
	}
	return filepath.Clean(root)
}
