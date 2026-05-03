# Round-2 fix list for Codex's implementation

> **Context**: Round 1 review fixed 7 of 9 issues + the cancel-after-snapshot
> partial-success path (Codex went beyond the ask there — good). Tests went
> 17 → 25 passing.
>
> **But**: Blocker 2 from round 1 is still unfixed AND now defended by a
> test that codifies the buggy behavior. Three new blockers + four new
> medium issues surfaced on second-pass review.
>
> **Spec source of truth**: `docs/specs/2026-05-02-pmo-bot-write-tools-design.md`
> **Round-1 review**: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes.md`
>
> **Don't ship until the 🔴 items are fixed.**

---

## 🔴 Blocker R2-1 — Freebusy conflict still written as `success` (round 1's Blocker 2 unfixed; test now defends the bug)

**Files**:
- `bot/agent/tools_impl/calendar_impl.py:33-43`
- `bot/tests/test_write_tools_impl.py:79-99`

**Current code (calendar_impl.py)**:
```python
conflicts = await calendar.batch_freebusy(attendees, start.isoformat(), end.isoformat())
if conflicts:
    result = {
        "outcome": "conflict",
        "conflicts": conflicts,
        "attendees": attendees,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }
    queries.mark_bot_action_success(row["id"], result)   # ← bug
    return ok(result)
```

**Why this is still wrong**: `mark_bot_action_success` does NOT release
`logical_key_locked` (verified at `db/queries.py:582-596`; only
`mark_bot_action_failed` and `mark_bot_action_undone` flip the flag to
`False`). So:

1. T=0: User asks for meeting M1 at 3pm with [ou_a, ou_b]. Freebusy
   returns conflict. Row locked + status=success + outcome=conflict.
2. T=10s: User retries the same request (LLM may auto-retry, or user
   re-sends). `start_action` computes the same `logical_key`. Partial
   UNIQUE on `(logical_key) WHERE logical_key_locked=true` finds the
   row → `LogicalKeyConflict` → `_success_replay(row, logical_key_replay=True)`
   returns `{outcome: "conflict", deduplicated_from_logical_key: true, ...}`.
3. The LLM sees `deduplicated_from_logical_key=true` and tells the user
   the meeting was created (it wasn't).

**The defending test** (`test_schedule_meeting_freebusy_conflict_is_success_without_event_create`)
asserts:
```python
assert successes[0][1] == {}     # ← asserts mark_bot_action_success WAS called
```
This locks in the bug. The assertion must invert.

**Fix**:

Step 1 — add a no-op terminal helper to `bot/db/queries.py`:

```python
def mark_bot_action_no_op(action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """Terminal status: request processed, but no Feishu side effect.
    Releases logical_key_locked so an immediate re-issue with different
    args isn't blocked by the partial UNIQUE.

    Used for paths like "freebusy returned a conflict so we never called
    create_event" — the row is a real audit record (we did the work),
    but there's nothing to dedup against on a re-issue.
    """
    res = (
        sb_admin()
        .table("bot_actions")
        .update({
            "status": "success",
            "result": {**(result or {}), "no_side_effect": True},
            "logical_key_locked": False,             # ← release the lock
            "updated_at": _utc_now_iso(),
        })
        .eq("id", action_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None
```

Step 2 — `calendar_impl.py:42`:
```python
if conflicts:
    result = {
        "outcome": "conflict",
        "conflicts": conflicts,
        ...
    }
    queries.mark_bot_action_no_op(row["id"], result)   # ← release lock
    return ok(result)
```

Step 3 — invert the test in `tests/test_write_tools_impl.py:87-99`:
```python
no_ops = []
monkeypatch.setattr("db.queries.mark_bot_action_no_op", lambda *args, **kwargs: no_ops.append((args, kwargs)))
monkeypatch.setattr(
    "db.queries.mark_bot_action_success",
    lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("freebusy conflict must not call mark_bot_action_success")),
)

result = asyncio.run(calendar_impl.schedule_meeting(_ctx(), {...}))

payload = content_payload(result)
assert payload["outcome"] == "conflict"
assert len(no_ops) == 1
# Assert the lock is released, not held.
```

Step 4 — add a regression test that proves re-issue after conflict
isn't dedup-replayed:
```python
def test_schedule_meeting_after_conflict_can_reissue_without_dedup_replay(monkeypatch):
    """A freebusy conflict must release the logical_key lock so a fresh
    request with new attendees / new time isn't silently dedup-replayed."""
    # ... mock the conflict path, then mock get_locked_by_logical_key to
    # return None (lock was released), and verify a second call with
    # different args proceeds to create_event normally.
```

---

## 🔴 Blocker R2-2 — `logical_key` canonicalization is too shallow; the 60s dedup window is broken in practice

**File**: `bot/agent/tool_utils.py:25-31`

**Current code**:
```python
def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def logical_key(*, chat_id, sender_open_id, action_type, args):
    digest = hashlib.sha256(stable_json(args).encode("utf-8")).hexdigest()
    return f"{chat_id}:{sender_open_id}:{action_type}:{digest}"
```

**Problem**: `sort_keys=True` only sorts dict keys at serialization time
— it does NOT canonicalize values. Real-world equivalents that produce
DIFFERENT logical_keys today:

| Equivalent intent | Args A | Args B |
|---|---|---|
| Same attendees | `["ou_a", "ou_b"]` | `["ou_b", "ou_a"]` |
| Same instant | `"2026-05-08T15:00:00+08:00"` | `"2026-05-08T07:00:00Z"` |
| Same default | `{title:"X"}` | `{title:"X", duration_minutes:30, include_asker:true}` |
| Same body | `markdown_body: "# Notes\n..."` | `markdown_body: "# Notes\n...\n"` (trailing newline) |

Spec §5.2 explicitly required per-action canonicalization: sort+dedup
attendees, normalize timestamps to UTC ISO, fill defaults, hash large
text bodies via sha256.

**Impact**: the 60s dedup window — the load-bearing safety net for
"webhook fires twice with different message_ids" or "user accidentally
sends twice via two devices" — fails to catch most realistic cases. The
first re-issue creates a duplicate side effect.

**Fix** — add per-action canonicalizers in
`bot/agent/canonical_args.py` (new file, per spec §5.2):

```python
"""Per-action canonicalization of tool args for logical_key hashing.

Spec §5.2: two requests with the same intent must produce the same
logical_key, so the partial UNIQUE on bot_actions can deduplicate them
within the 60-second window. This requires:
  - sorted+deduped attendee lists
  - timestamps normalized to UTC ISO
  - default values filled in
  - large bodies replaced with sha256 digests
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any


def _to_utc_iso(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(tz=__import__("datetime").timezone.utc).isoformat()
    except Exception:
        return value


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon_action_item(item: dict) -> dict:
    return {
        "title": (item.get("title") or "").strip(),
        "owner_open_id": item.get("owner_open_id") or "",
        "due_date": _to_utc_iso(item.get("due_date")),
        "project": (item.get("project") or "").strip(),
        "status": item.get("status") or "todo",
    }


def _canon_field(field: dict) -> dict:
    return {
        "name": (field.get("name") or field.get("field_name") or "").strip(),
        "type": str(field.get("type")),
        "options": sorted(((field.get("options") or {}).get("choices") or field.get("choices") or [])),
    }


def canonicalize_args(action_type: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical dict whose JSON encoding is identical for
    semantically-equivalent requests. Used as the input to sha256 in
    logical_key. See spec §5.2.
    """
    if action_type in {"schedule_meeting", "restore_schedule_meeting"}:
        return {
            "title": (args.get("title") or "").strip(),
            "start_time_utc": _to_utc_iso(args.get("start_time")),
            "duration_minutes": int(args.get("duration_minutes") or 30),
            "attendee_open_ids": sorted(set(args.get("attendee_open_ids") or [])),
            "description_sha256": _sha256(args.get("description") or ""),
            "include_asker": bool(args.get("include_asker", True)),
        }
    if action_type == "cancel_meeting":
        return {
            "event_id": args.get("event_id") or "",
            "last": bool(args.get("last")),
        }
    if action_type == "append_action_items":
        items = sorted(
            (_canon_action_item(it) for it in (args.get("items") or [])),
            key=lambda i: (i["title"], i["owner_open_id"]),
        )
        return {
            "items": items,
            "project": (args.get("project") or "").strip(),
            "meeting_event_id": args.get("meeting_event_id") or "",
        }
    if action_type in {"create_doc", "create_meeting_doc"}:
        return {
            "title": (args.get("title") or "").strip(),
            "markdown_sha256": _sha256(args.get("markdown_body") or ""),
            "meeting_event_id": args.get("meeting_event_id") or "",
        }
    if action_type == "append_to_doc":
        return {
            "doc_link_or_token": (args.get("doc_link_or_token") or "").strip(),
            "heading": (args.get("heading") or "").strip(),
            "markdown_sha256": _sha256(args.get("markdown_body") or ""),
        }
    if action_type == "create_bitable_table":
        return {
            "name": (args.get("name") or "").strip(),
            "fields": [_canon_field(f) for f in (args.get("fields") or [])],
        }
    if action_type == "append_to_my_table":
        return {
            "table_id": args.get("table_id") or "",
            "records_sha256": _sha256(_stable(args.get("records") or [])),
        }
    # Default: stable_json with sorted keys (still better than nothing).
    return args


def _stable(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

Then `bot/agent/tool_utils.py:logical_key`:

```python
def logical_key(*, chat_id: str, sender_open_id: str, action_type: str, args: dict[str, Any]) -> str:
    from agent.canonical_args import canonicalize_args
    canon = canonicalize_args(action_type, args)
    digest = hashlib.sha256(stable_json(canon).encode("utf-8")).hexdigest()
    return f"{chat_id}:{sender_open_id}:{action_type}:{digest}"
```

**Add tests** (`bot/tests/test_canonical_args.py`):
- `test_attendee_order_does_not_affect_logical_key`
- `test_timestamp_offsets_normalize_to_same_logical_key` (UTC vs +08:00)
- `test_default_duration_minutes_normalize_to_same_logical_key`
- `test_trailing_newline_in_markdown_does_not_affect_logical_key`
- `test_action_item_order_does_not_affect_logical_key`

---

## 🔴 Blocker R2-3 — `cancel_meeting` source-lookup runs before `start_action`; webhook retry returns "no source" instead of cached success

**File**: `bot/agent/tools_impl/calendar_impl.py:79-99`

**Current code**:
```python
async def cancel_meeting(ctx, args):
    event_id = args.get("event_id")
    if not event_id and args.get("last"):
        source = queries.last_meeting_action_for_sender_in_chat(ctx.chat_id, ctx.sender_open_id)
        event_id = source.get("target_id") if source else None
    elif event_id:
        source = queries.get_bot_action_by_target(...)
    else:
        return err("event_id is required, or pass last=true")
    if not source:
        return err("只能取消我在这个会话里为你创建的会议")
    row, replay = start_action(ctx, "cancel_meeting", args)   # ← too late
    if replay:
        return replay
```

**Trace of the bug**:

1. T=0: User sends `"取消刚才那个"` (`message_id=M`, args=`{last: true}`).
2. T=1: `last_meeting_action_for_sender_in_chat` returns the schedule
   row (status=success). Cancel proceeds: snapshot → delete →
   `retire_source_action(schedule_row.id)` (status: success → undone)
   → `mark_bot_action_success(cancel_row.id)`.
3. T=4: Feishu retries the webhook (same `message_id=M`) because our
   ack was delayed past Feishu's 3s threshold.
4. T=4 webhook B: `last_meeting_action_for_sender_in_chat` filters
   `status IN ('success','reconciled_unknown')` (`db/queries.py:688`).
   The schedule row is now `undone` → filtered out. Returns None →
   `event_id` is None → returns `err("只能取消...")`.

But `(message_id=M, action_type='cancel_meeting')` already exists at
status=success! `start_action` would have replayed the cached success.
We never reach `start_action` because the source lookup fails first.

**Fix**: move `start_action` ahead of the source lookup. The shape
becomes:

```python
async def cancel_meeting(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    event_id = args.get("event_id")
    if not event_id and not args.get("last"):
        return err("event_id is required, or pass last=true")

    # Phase 0/1 first — webhook retries of a previously-succeeded cancel
    # must replay the cached result, even if the source schedule row has
    # since been retired and is no longer findable.
    row, replay = start_action(ctx, "cancel_meeting", args)
    if replay:
        return replay

    # Now resolve the source row (only on fresh runs).
    if event_id:
        source = queries.get_bot_action_by_target(
            chat_id=ctx.chat_id,
            sender_open_id=ctx.sender_open_id,
            target_id=event_id,
            target_kind="calendar_event",
            action_type_in=["schedule_meeting", "restore_schedule_meeting"],
            status_in=["success", "reconciled_unknown"],
        )
    else:
        source = queries.last_meeting_action_for_sender_in_chat(ctx.chat_id, ctx.sender_open_id)
        event_id = source.get("target_id") if source else None

    if not source:
        # Roll the row back so a future re-issue with valid event_id can proceed.
        queries.mark_bot_action_failed(row["id"], "no_source_meeting_for_cancel")
        return err("只能取消我在这个会话里为你创建的会议")

    snapshot_persisted = False
    try:
        calendar_id = (source.get("result") or {}).get("calendar_id")
        snapshot = await calendar.get_event(calendar_id, event_id)
        ...
```

**Add test** (in `tests/test_write_tools_impl.py` or a new file):

```python
def test_cancel_meeting_webhook_retry_after_success_replays_cached_result(monkeypatch):
    """Feishu webhook retry of a successful cancel must replay the cached
    result, not fail with 'no source' because the source was retired."""
    cached_result = {"cancelled": True, "event_id": "evt-1", "calendar_id": "cal-1"}
    monkeypatch.setattr("db.queries.get_bot_action", lambda message_id, action_type: {
        "id": "cancel-1",
        "status": "success",
        "result": cached_result,
    })
    # last_meeting_action would now return None (schedule row is undone),
    # but we never reach it.
    monkeypatch.setattr(
        "db.queries.last_meeting_action_for_sender_in_chat",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not run after cache hit")),
    )

    result = asyncio.run(calendar_impl.cancel_meeting(_ctx(), {"last": True}))
    payload = content_payload(result)
    assert payload["cancelled"] is True
    assert payload["event_id"] == "evt-1"
    assert payload["cached_result"] is True
```

---

## 🟡 Medium R2-4 — `contact.search_users` uses POST; the endpoint requires GET

**File**: `bot/feishu/contact.py:51-63`

**Current code**:
```python
async with httpx.AsyncClient(timeout=10.0) as ac:
    resp = await ac.post(
        "https://open.feishu.cn/open-apis/search/v1/user",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "page_size": 20},
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        await asyncio.sleep(0.5)
        resp = await ac.post(
            "https://open.feishu.cn/open-apis/search/v1/user",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "page_size": 20},
        )
```

**Problem**: Feishu's `/open-apis/search/v1/user` is a **GET** endpoint
that takes `query` and `page_size` as URL query params. POST with JSON
body returns either 400 or empty `users` list. `resolve_people`'s
"name-search" tier (the third resolution path in spec §3.1) silently
fails in production.

**Fix**:
```python
async with httpx.AsyncClient(timeout=10.0) as ac:
    params = {"query": query, "page_size": 20}
    resp = await ac.get(
        "https://open.feishu.cn/open-apis/search/v1/user",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        await asyncio.sleep(0.5)
        resp = await ac.get(
            "https://open.feishu.cn/open-apis/search/v1/user",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    resp.raise_for_status()
    data = resp.json()
```

Add a respx-based test that asserts the request was GET with the right
query params. This is the kind of bug that won't show up until a real
user types `@包工头 帮我和张伟订下午 3 点的会` and resolve_people silently
returns empty.

---

## 🟡 Medium R2-5 — `read_external_table` rate limit counts failed calls

**File**: `bot/agent/tools_external.py:81-101`

**Current code**:
```python
async def read_external_table(args: dict) -> dict[str, Any]:
    key = ctx.conversation_key or "anon"
    now = monotonic()
    calls = _external_table_calls.setdefault(key, deque())
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= 5:
        return err("read_external_table 每小时每会话最多 5 次。请改用文字描述或缩小范围。")
    calls.append(now)                                 # ← always increments
    try:
        app_token, table_id = await _normalize_table(args["link_or_app_table_token"])
        ...
    except Exception as e:
        return err(str(e))
```

**Problem**: 5 bad URLs in a row exhaust the rate limit even though no
data was returned.

**Fix** — only count successful reads:
```python
async def read_external_table(args: dict) -> dict[str, Any]:
    key = ctx.conversation_key or "anon"
    now = monotonic()
    calls = _external_table_calls.setdefault(key, deque())
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= 5:
        return err("read_external_table 每小时每会话最多 5 次。请改用文字描述或缩小范围。")
    try:
        app_token, table_id = await _normalize_table(args["link_or_app_table_token"])
        page_size = min(int(args.get("page_size") or 50), 200)
        result = await bitable.search_records(
            app_token=app_token, table_id=table_id,
            filter=args.get("filter") or None,
            page_size=page_size,
            page_token=args.get("page_token") or None,
        )
    except Exception as e:
        return err(str(e))
    calls.append(now)                                  # only after success
    return ok(result)
```

---

## 🟡 Medium R2-6 — Dead `list_child_blocks` call in `append_to_doc`

**File**: `bot/agent/tools_impl/doc_impl.py:138`

**Current code**:
```python
parent = token
blocks = _markdown_to_blocks(...)
await docx.list_child_blocks(token, parent)            # ← result discarded
block_ids = await docx.append_blocks(token, parent, blocks, client_token=row["id"])
```

`docx.list_child_blocks` paginates through every direct child of the
doc's root block (could be hundreds of API calls for a long doc), then
the result is thrown away.

**Fix**: delete that line. Undo's `delete_blocks` already calls
`list_child_blocks` internally to map stored block_ids → current
indexes; we don't need to pre-fetch at append time.

```python
parent = token
blocks = _markdown_to_blocks(
    f"<!-- bot_action_id={row['id']} -->\n"
    + ((f"## {args['heading']}\n\n") if args.get("heading") else "")
    + args.get("markdown_body", "")
)
block_ids = await docx.append_blocks(token, parent, blocks, client_token=row["id"])
```

---

## 🟡 Medium R2-7 — No `lookup_feishu_link_by_phone`; phone input always hits Feishu API

**Files**: `bot/agent/tools_meta.py:171-179`, `bot/db/queries.py`

**Current code (tools_meta.py)**:
```python
if email or phone:
    linked = queries.lookup_feishu_link_by_email(email) if email else None  # ← phone path: linked stays None
    if linked and linked.get("open_id"):
        resolved.append(...)
        continue
    found = await contact.batch_get_id_by_email_or_phone(...)
```

A user passes `{"phone": "13800138000"}` — even if their phone↔open_id
binding is in `feishu_links` locally, we always go to the Feishu API.

**Fix**:

Add to `bot/db/queries.py`:
```python
def lookup_feishu_link_by_phone(phone: str) -> Optional[dict[str, Any]]:
    if not phone:
        return None
    # Normalize: strip leading +, country-code prefix variants
    normalized = phone.lstrip("+").replace("-", "").replace(" ", "")
    res = (
        sb_admin()
        .table("feishu_links")
        .select("user_id, feishu_open_id, feishu_name, feishu_email, feishu_mobile, profiles!inner(handle, display_name)")
        .or_(f"feishu_mobile.eq.{phone},feishu_mobile.eq.{normalized}")
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    return _feishu_link_row_to_person(res.data)
```

(Verify `feishu_links.feishu_mobile` column exists; if not, that's a
separate pre-req — either add the column, or skip this fix until
feishu_links has phone tracking.)

In `tools_meta.py:172`:
```python
if email or phone:
    linked = None
    if email:
        linked = queries.lookup_feishu_link_by_email(email)
    if not linked and phone:
        linked = queries.lookup_feishu_link_by_phone(phone)
    if linked and linked.get("open_id"):
        resolved.append({"input": s, **linked, "source": "profiles"})
        continue
    found = await contact.batch_get_id_by_email_or_phone(...)
```

---

## 🟡 Medium R2-8 — `_external_table_calls` dict never prunes empty deques

**File**: `bot/agent/tools_external.py:14`

**Problem**: a bot running for months accumulates one entry per
conversation_key it has ever seen. Each entry's deque does get aged out
(values older than 3600s drop), but the deque itself stays in the dict.

**Fix** — opportunistic prune at the top of each call:
```python
async def read_external_table(args: dict) -> dict[str, Any]:
    key = ctx.conversation_key or "anon"
    now = monotonic()
    # Opportunistic prune: drop conversations that haven't called in >1h
    for k in list(_external_table_calls.keys()):
        deq = _external_table_calls[k]
        while deq and now - deq[0] > 3600:
            deq.popleft()
        if not deq:
            del _external_table_calls[k]
    calls = _external_table_calls.setdefault(key, deque())
    ...
```

---

## 🟢 Things round 1 raised that Codex correctly fixed — keep an eye on regressions

| # | Where it was | What was done | Test that locks it in |
|---|---|---|---|
| R1-1 | `tools_impl/common.py:25-69` | reconciled_unknown surfaces partial signal; failed refuses auto-retry | `test_start_action_surfaces_reconciled_unknown_*`, `test_start_action_failed_message_requires_new_message` |
| R1-4 | `tools_meta.py:360-386` | `_restore_cancelled_meeting` invite failure returns partial_result instead of raising | `test_undo_cancel_restore_invite_failure_records_partial_audit_without_retiring_cancel` |
| R1-5 | `tools_meta.py:106-133` | `today_iso` fetches user timezone from `contact.get_user`; falls back to Asia/Shanghai with `user_timezone_source="fallback"` | `test_today_iso_uses_feishu_contact_timezone` |
| R1-6 | `tools_impl/doc_impl.py:132`, `tools_meta.py:300` | parent_block_id = document_id (not "root" literal) | `test_append_to_doc_uses_document_token_as_root_parent` |
| R1-7 | `tools_impl/bitable_impl.py:135,159,177` | `_is_bot_owned_custom_table` (DB authorship) replaces O(N) `table_exists` | `test_append_to_my_table_uses_local_authorship_gate_not_table_exists` |
| R1-8 | `agent/runner.py:187` | walrus removed | (no test, low risk) |
| R1-9 | `tools_impl/bitable_impl.py:17-19` | auto-project lookup gated behind "any item missing project" | (no test, low risk) |
| (bonus) | `tools_impl/calendar_impl.py:100-139` | cancel: snapshot persisted but delete crashes → `mark_bot_action_reconciled_unknown(keep_lock=True)` instead of `mark_bot_action_failed` (which would have released the lock and let the partial state silently re-execute) | `test_cancel_meeting_delete_failure_after_snapshot_becomes_partial_success` |

---

## 🔵 Round-1 Blocker 3 — downgrade

Round-1 review claimed `_restore_cancelled_meeting` over-retiring the
source schedule row was a hard blocker. On second-pass trace, all
downstream queries correctly find the new `restore_schedule_meeting`
row (which has `target_kind='calendar_event'` and shows up in
`bot_known_events_for_attendee` and `last_meeting_action_for_sender_in_chat`).

The audit trail is conceptually weird (the original schedule row reads
as `undone` even though the meeting effectively came back), but no
concrete user flow breaks.

**Downgraded to documentation note.** No code change required this round.

---

## Summary

| # | Severity | File | Issue |
|---|---|---|---|
| **R2-1** | 🔴 | `tools_impl/calendar_impl.py:42`, `tests/test_write_tools_impl.py:79-99` | Freebusy conflict still writes `success` with lock held; test defends the bug — must invert |
| **R2-2** | 🔴 | `agent/tool_utils.py:25-31` (+ new `agent/canonical_args.py`) | `logical_key` lacks per-action canonicalization; 60s dedup window broken |
| **R2-3** | 🔴 | `tools_impl/calendar_impl.py:79-99` | `cancel_meeting` source-lookup before `start_action`; webhook retry hits `"no source"` instead of cached success |
| R2-4 | 🟡 | `feishu/contact.py:52-63` | `search_users` uses POST; should be GET — name-search silently broken |
| R2-5 | 🟡 | `tools_external.py:89` | Rate-limit counter increments on failed calls |
| R2-6 | 🟡 | `tools_impl/doc_impl.py:138` | Dead `list_child_blocks` call |
| R2-7 | 🟡 | `tools_meta.py:171`, `db/queries.py` | No `lookup_feishu_link_by_phone`; phone input always hits Feishu API |
| R2-8 | 🟡 | `tools_external.py:14` | `_external_table_calls` dict accumulates empty deques forever |

After R2-1, R2-2, R2-3 are fixed (with new tests for each), plus R2-4
(production-impact: name-search fails day one), the implementation is
ready for a real Feishu smoke test. R2-5/6/7/8 are good-citizen
cleanups.
