// Package notify shows macOS user notifications via osascript. On
// non-Darwin platforms it's a silent no-op — Linux's notify-send is
// not always installed and we'd rather skip than print errors.
//
// Two flavors of message:
//
//   StartedListening()  — fired once when the daemon's run loop is
//                         ready. Confirms install / boot worked.
//
//   UploadProgress(n)   — fired after each batch of successful uploads,
//                         throttled to one notification per ThrottleWindow.
//                         Counts accumulate within the window so the
//                         user sees "5 new turns in the last 5 min"
//                         instead of 5 separate banners.

package notify

import (
	"fmt"
	"os/exec"
	"runtime"
	"sync"
	"time"
)

// ThrottleWindow caps the upload notification rate. Production-tuned
// to feel "alive but not spammy".
const ThrottleWindow = 5 * time.Minute

const appName = "pmo-agent"

// StartedListening shows a one-shot confirmation that the daemon is
// running. Safe to call multiple times — each call shows another
// banner.
func StartedListening() {
	send("✓ pmo-agent started", "Watching for new turns.")
}

// uploadThrottle accumulates upload counts and flushes them at most
// once per ThrottleWindow.
type uploadThrottle struct {
	mu          sync.Mutex
	count       int
	windowStart time.Time
	timer       *time.Timer
}

var ut = &uploadThrottle{}

// UploadProgress records that one or more turns were successfully
// uploaded. The first call within a window shows a banner immediately.
// Subsequent calls accumulate and flush at the end of the window with
// a count.
//
// Behavior:
//
//   t=0:00  UploadProgress(1)  → banner: "1 new turn"
//   t=0:30  UploadProgress(1)  → counted, no banner yet
//   t=2:00  UploadProgress(2)  → counted (now 3 since 0:00)
//   t=5:00  (window expires)   → banner: "3 new turns since 0:00"
//   t=5:01  UploadProgress(1)  → banner: "1 new turn" (new window starts)
func UploadProgress(n int) {
	if n <= 0 {
		return
	}
	ut.mu.Lock()
	defer ut.mu.Unlock()

	if ut.timer == nil {
		// Fresh window. Show now, queue a delayed flush of any
		// follow-up counts.
		ut.windowStart = time.Now()
		ut.count = 0 // shown immediately, doesn't count toward window
		send(banner(n), "")
		ut.timer = time.AfterFunc(ThrottleWindow, flushWindow)
		return
	}
	// Mid-window: just accumulate.
	ut.count += n
}

func flushWindow() {
	ut.mu.Lock()
	n := ut.count
	ut.count = 0
	ut.timer = nil
	since := time.Since(ut.windowStart)
	ut.mu.Unlock()

	if n <= 0 {
		return
	}
	mins := int(since.Round(time.Minute).Minutes())
	subtitle := fmt.Sprintf("%d new turn%s in the last %d min", n, plural(n), mins)
	if n == 1 {
		subtitle = "1 new turn since the last update"
	}
	send(banner(n), subtitle)
}

func banner(n int) string {
	if n == 1 {
		return "✓ 1 new turn uploaded"
	}
	return fmt.Sprintf("✓ %d new turns uploaded", n)
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// send is the platform-specific dispatch. macOS uses osascript;
// other platforms are silent.
//
// We deliberately fire-and-forget: a missing osascript or denied
// notification permission shouldn't prevent the daemon from running.
func send(title, body string) {
	if runtime.GOOS != "darwin" {
		return
	}
	// osascript's `display notification` doesn't honor newlines
	// inside the body cleanly, but escaping double quotes is
	// enough for our text.
	script := fmt.Sprintf(
		`display notification %q with title %q subtitle %q`,
		body, appName, title,
	)
	_ = exec.Command("osascript", "-e", script).Start()
}
