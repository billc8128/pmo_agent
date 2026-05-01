// Package store wraps the daemon's local SQLite database
// (~/.pmo-agent/state.db). It records uploaded turn keys and raw transcript
// file state so a restart doesn't re-upload unchanged work.
package store
