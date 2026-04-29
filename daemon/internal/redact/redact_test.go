package redact

import "testing"

// Tiny smoke test that locks in the passthrough contract. When real
// rules ship, this test will fail and force us to update it
// deliberately, which is the kind of breakage we want.
func TestRedactPassthrough(t *testing.T) {
	in := "hello world\nsk_live_FAKE_LOOKING_SECRET\nuser@example.com"
	out, hits := Redact(in)

	if out != in {
		t.Errorf("milestone-1 stub must not modify input\n got: %q\nwant: %q", out, in)
	}
	if len(hits) != 0 {
		t.Errorf("milestone-1 stub must report 0 hits, got %d", len(hits))
	}
}
