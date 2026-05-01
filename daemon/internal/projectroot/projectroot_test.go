package projectroot

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestResolve_UsesNearestGitRoot(t *testing.T) {
	requireGit(t)

	base := t.TempDir()
	parent := filepath.Join(base, "project", "Personal")
	game := filepath.Join(parent, "game")
	if err := os.MkdirAll(game, 0o700); err != nil {
		t.Fatal(err)
	}
	gitInit(t, parent)
	gitInit(t, game)

	nested := filepath.Join(game, "frontend")
	if err := os.MkdirAll(nested, 0o700); err != nil {
		t.Fatal(err)
	}

	if got := Resolve(nested); samePath(t, got, game) == false {
		t.Fatalf("Resolve(%q) = %q, want nearest git root %q", nested, got, game)
	}
}

func TestResolve_ClaudeWorktreeReturnsOriginalRepo(t *testing.T) {
	requireGit(t)

	base := t.TempDir()
	repo := filepath.Join(base, "vibeliveai")
	wt := filepath.Join(repo, ".claude", "worktrees", "nervous-hofstadter-6eb6fb")
	if err := os.MkdirAll(wt, 0o700); err != nil {
		t.Fatal(err)
	}
	gitInit(t, repo)

	if got := Resolve(wt); samePath(t, got, repo) == false {
		t.Fatalf("Resolve(%q) = %q, want original repo %q", wt, got, repo)
	}
}

func TestResolve_FallsBackToCleanPathOutsideGit(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "not-a-repo")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}

	if got := Resolve(dir + string(os.PathSeparator)); got != dir {
		t.Fatalf("Resolve should fall back to cleaned cwd; got %q, want %q", got, dir)
	}
}

func requireGit(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
}

func gitInit(t *testing.T, dir string) {
	t.Helper()
	cmd := exec.Command("git", "init", "-q")
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git init %s: %v\n%s", dir, err, out)
	}
}

func samePath(t *testing.T, a, b string) bool {
	t.Helper()
	realA, err := filepath.EvalSymlinks(a)
	if err != nil {
		t.Fatal(err)
	}
	realB, err := filepath.EvalSymlinks(b)
	if err != nil {
		t.Fatal(err)
	}
	return realA == realB
}
