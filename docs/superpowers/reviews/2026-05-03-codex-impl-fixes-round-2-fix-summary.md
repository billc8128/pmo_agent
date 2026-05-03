# Round-2 implementation fix summary

Date: 2026-05-03

Source review: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes-round-2.md`

## Fixed

- R2-1 was handled as a replay-language fix, not a DB state change.
  Current spec still defines freebusy conflict as `success + outcome=conflict`.
  `_success_replay` now adds `meeting_created=false` and an explicit
  `agent_directive` so cached conflict results are not presented as created
  meetings.
- R2-2 fixed by adding `bot/agent/canonical_args.py` and routing
  `logical_key()` through per-action canonicalization. Covered cases include
  attendee order, timestamp offsets, schedule defaults, markdown trailing
  newline, and action item order.
- R2-3 fixed by moving `cancel_meeting` `start_action` before source lookup.
  Same-message webhook retries now replay cached cancel success even after the
  source schedule row has been retired.
- R2-4 fixed by changing `contact.search_users` from POST JSON to GET query
  params for `/open-apis/search/v1/user`.
- R2-5 and R2-8 fixed by pruning empty external-table rate-limit deques and
  counting only successful `read_external_table` calls.
- R2-6 fixed by removing the discarded `docx.list_child_blocks` prefetch from
  `append_to_doc`.
- R2-7 fixed with migration `0012_feishu_links_mobile.sql`, OAuth mobile
  persistence, `lookup_feishu_link_by_phone`, and a `resolve_people` local
  phone-link fast path before remote Feishu lookup.

## Tests Added

- `bot/tests/test_canonical_args.py`
- `bot/tests/test_feishu_contact.py`
- New regression cases in `bot/tests/test_write_tools_impl.py`
- New external-table rate-limit cases in `bot/tests/test_tools_external.py`

## Verification

- `python -m compileall -q bot`
- `pytest bot/tests -q` -> `36 passed, 9 warnings`
- `git diff --check`
- `pnpm --dir web lint` -> 0 errors, 4 existing warnings
