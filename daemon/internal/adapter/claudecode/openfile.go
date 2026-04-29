package claudecode

import (
	"io"
	"os"
)

// openFileImpl is split out so tests can avoid touching the real
// filesystem (we'd swap this in a test build, but for MVP it's just
// os.Open).
func openFileImpl(path string) (io.ReadCloser, error) {
	return os.Open(path)
}
