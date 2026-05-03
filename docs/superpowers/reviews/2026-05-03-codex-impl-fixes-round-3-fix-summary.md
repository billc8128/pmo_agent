# Round-3 implementation fix summary

Date: 2026-05-03

Source review: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes-round-3.md`

## Fixed

- R3-1 fixed `_phone_variants` so a bare Chinese mobile such as
  `13800138000` also matches `8613800138000` and `+8613800138000`.
  Existing `+86...` inputs now also generate bare and `+<bare>` forms,
  and empty input returns an empty list.
- R3-2 fixed `_success_replay` so meeting-specific conflict wording is
  emitted only for `schedule_meeting` and `restore_schedule_meeting`
  rows with `result.outcome == "conflict"`.
- R3-3 fixed negative `read_doc.max_chars` handling. Values `<= 0` now
  fall back to the 20000-character default instead of triggering Python
  negative slicing. Positive values below 20000 remain honored because
  the spec does not define a lower bound.

## Tests Added

- `bot/tests/test_db_queries.py`
  - Chinese bare mobile adds `86` and `+86` variants.
  - `+86` mobile adds bare and `+<bare>` variants.
  - dashed/spaced phone formats normalize correctly.
  - empty input returns an empty list.
- `bot/tests/test_write_tools_impl.py`
  - non-meeting cached `outcome=conflict` results do not receive
    meeting-specific replay directives.
- `bot/tests/test_tools_external.py`
  - negative `max_chars` no longer truncates from the end.

## Verification

- Red run before implementation:
  `pytest bot/tests/test_db_queries.py bot/tests/test_write_tools_impl.py::test_start_action_conflict_logical_replay_says_no_meeting_was_created bot/tests/test_write_tools_impl.py::test_start_action_non_meeting_conflict_replay_does_not_emit_meeting_directive bot/tests/test_tools_external.py::test_read_doc_clamps_negative_max_chars_without_truncating_from_end -q`
  -> 6 failed, 1 passed.
- Focused green run after implementation:
  same command -> 7 passed, 9 warnings.
- `python -m compileall -q bot`
- `pytest bot/tests -q` -> 42 passed, 9 warnings.
- `git diff --check`
- `pnpm --dir web lint` -> 0 errors, 4 existing warnings.
