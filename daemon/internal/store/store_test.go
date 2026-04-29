package store

import (
	"path/filepath"
	"testing"
)

func TestStore_MarkAndCheck(t *testing.T) {
	dir := t.TempDir()
	s, err := OpenAt(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	got, err := s.IsUploaded("claude_code", "sess-1", 0)
	if err != nil || got {
		t.Fatalf("fresh DB: IsUploaded=%v err=%v want false,nil", got, err)
	}

	id := int64(42)
	if err := s.MarkUploaded("claude_code", "sess-1", 0, &id); err != nil {
		t.Fatal(err)
	}

	got, err = s.IsUploaded("claude_code", "sess-1", 0)
	if err != nil || !got {
		t.Fatalf("after Mark: IsUploaded=%v err=%v want true,nil", got, err)
	}

	// Different turn_index → not found.
	got, _ = s.IsUploaded("claude_code", "sess-1", 1)
	if got {
		t.Errorf("turn_index=1 should not be marked")
	}
}

func TestStore_MarkUploadedIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	s, err := OpenAt(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	id1, id2 := int64(1), int64(2)
	if err := s.MarkUploaded("a", "s", 0, &id1); err != nil {
		t.Fatal(err)
	}
	if err := s.MarkUploaded("a", "s", 0, &id2); err != nil {
		t.Fatal(err) // ON CONFLICT must not error
	}
	ups, err := s.RecentUploads(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(ups) != 1 {
		t.Fatalf("want 1 row, got %d", len(ups))
	}
	if got := *ups[0].ServerTurnID; got != id2 {
		t.Errorf("server_turn_id should reflect latest: got %d, want %d", got, id2)
	}
}

func TestStore_RecentUploadsOrder(t *testing.T) {
	dir := t.TempDir()
	s, err := OpenAt(filepath.Join(dir, "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	// Insert in non-monotonic order; we don't control time precisely,
	// so just verify ordering by uploaded_at DESC matches insertion
	// order *within a single test* (uploaded_at is set to now() each call).
	for i := 0; i < 3; i++ {
		if err := s.MarkUploaded("a", "s", i, nil); err != nil {
			t.Fatal(err)
		}
	}
	ups, _ := s.RecentUploads(10)
	if len(ups) != 3 {
		t.Fatalf("want 3 rows, got %d", len(ups))
	}
	// Most-recent first means turn_index=2 should be first.
	if ups[0].TurnIndex != 2 {
		t.Errorf("RecentUploads[0].TurnIndex = %d, want 2", ups[0].TurnIndex)
	}
}
