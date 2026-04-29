// Package redact removes secrets from text before it leaves the user's
// machine. Per spec §4.5, this is the project's most important
// non-functional requirement.
//
// Milestone 1: passthrough — Redact returns input unchanged with no
// hits. Real rules will be wired in here without changing the signature,
// so callers don't need to be touched when the rules ship.

package redact

// Hit describes a single redaction. We keep these (and surface them via
// status / audit log) so users can verify redaction is working without
// us ever storing the matched plaintext.
type Hit struct {
	Rule           string // identifier of the rule that matched
	OriginalLength int    // length of the redacted span, in bytes
}

// Redact returns text with secrets replaced by the literal string
// "[REDACTED]" and a record of every match. It must be deterministic and
// not depend on any global state, so it's safe to call from many
// goroutines and easy to unit-test.
//
// Milestone 1: stub. Returns input unchanged.
func Redact(text string) (string, []Hit) {
	return text, nil
}
