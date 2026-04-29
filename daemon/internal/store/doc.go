// Package store wraps the daemon's local SQLite database
// (~/.pmo-agent/state.db). It records per-file byte offsets and per-turn
// upload status so a restart doesn't re-upload work.
package store
