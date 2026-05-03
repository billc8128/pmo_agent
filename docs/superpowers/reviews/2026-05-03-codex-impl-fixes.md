# Fix list for Codex's first implementation pass

> **Context**: Codex landed the v21 implementation. Tests pass (17 / 17),
> SDK class names all resolve, the 5-MCP-server architecture is wired
> correctly. But several spec invariants were dropped on the floor — the
> tests didn't catch them because the tests don't cover those paths.
>
> **Spec source of truth**: `docs/specs/2026-05-02-pmo-bot-write-tools-design.md`
> **Don't ship until the 🔴 items are fixed.**

---

## 🔴 Blocker 1 — `start_action` violates spec §5.1 state machine

**File**: `bot/agent/tools_impl/common.py:25-39`

**Current code**:
```python
existing_for_message = queries.get_bot_action(message_id, action_type)
if existing_for_message:
    status = existing_for_message.get("status")
    if status == "success":
        return None, ok(existing_for_message.get("result") or {})
    if status == "reconciled_unknown":
        return None, ok(existing_for_message.get("result") or {})           # ← bug
    if status == "failed":
        row = queries.update_for_retry(existing_for_message["id"])           # ← bug
        if row:
            return row, None
        return None, err(...)
    if status == "undone":
        return None, err(...)
    return None, err("这个请求已经在处理，等它完成后我会回复")
```

**Problems**:
1. `reconciled_unknown` is treated identically to `success` — but per spec
   §5.1 row 76 + §10 row 6, `reconciled_unknown` means **partial state on
   Feishu**; the LLM must be told so it can offer undo. Silently
   replaying it as success hides the partial-failure signal.
2. `failed` auto-retries via `update_for_retry`. Per spec §5.1 row 80,
   `failed` rows must NOT auto-retry on the same `message_id` — the user
   must re-issue (which gets a new `message_id`). Auto-retrying on the
   same message hides transient errors and loops silently.

**Fix**:
```python
existing_for_message = queries.get_bot_action(message_id, action_type)
if existing_for_message:
    status = existing_for_message.get("status")
    if status == "success":
        return None, ok(existing_for_message.get("result") or {})
    if status == "reconciled_unknown":
        # Surface the partial-success signal so the LLM can offer undo.
        result = existing_for_message.get("result") or {}
        return None, ok({
            **result,
            "reconciliation_kind": result.get("reconciliation_kind") or "partial_success",
            "suggest_undo": True,
            "agent_directive": (
                "上一次相同的请求处于 reconciled_unknown 状态——飞书上可能已经"
                "部分生效。把这个不确定性告诉用户，并主动提议 undo_last_action。"
            ),
        })
    if status == "failed":
        # Spec §5.1 row 80: do NOT auto-retry on the same message_id.
        return None, err(
            "这个请求上次失败了。如果还想做，请重新发一条消息（飞书会给它新的 message_id）",
            previous_error=existing_for_message.get("error"),
        )
    if status == "undone":
        return None, err("这个请求已经被撤销；如果要重新执行，请重新发一条消息")
    return None, err("这个请求已经在处理，等它完成后我会回复")
```

Same applies to the `LogicalKeyConflict` branch lower in the same
function — `reconciled_unknown` there should also surface the
partial-success signal:

```python
except queries.LogicalKeyConflict as exc:
    row = exc.existing_row
    if row and row.get("status") == "success":
        return None, ok({**(row.get("result") or {}), "deduplicated_from_logical_key": True})
    if row and row.get("status") == "reconciled_unknown":
        result = row.get("result") or {}
        return None, ok({
            **result,
            "deduplicated_from_logical_key": True,
            "reconciliation_kind": result.get("reconciliation_kind") or "partial_success",
            "suggest_undo": True,
        })
    return None, err("同一个动作还在进行中，先等它完成")
```

---

## 🔴 Blocker 2 — `schedule_meeting` writes `success` for a freebusy conflict

**File**: `bot/agent/tools_impl/calendar_impl.py:33-43`

**Current code**:
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

**Problem**: spec §3.3 Phase 2.1 + §10 row 12 — a freebusy conflict means
**no Feishu side effect happened**. Marking the row `success` keeps
`logical_key_locked=true`, so a re-issued identical request within 60s
will hit the partial UNIQUE and return
`deduplicated_from_logical_key=true` — falsely claiming the meeting
exists when nothing was created.

**Fix**: add a `mark_bot_action_no_op` helper that releases the lock,
or transition straight to `failed` with a special error tag that's
recognized by the LLM as "user fixable":

```python
# In bot/db/queries.py — add this helper
def mark_bot_action_no_op(action_id: str, result: dict[str, Any]) -> None:
    """No-op terminal state: the request was processed but produced no
    Feishu side effect (e.g. freebusy conflict). Releases the lock so
    a follow-up request with different args isn't blocked.
    """
    sb_admin().table("bot_actions").update({
        "status": "success",
        "result": {**result, "outcome_no_op": True},
        "logical_key_locked": False,                # ← release the lock
        "updated_at": _utc_now_iso(),
    }).eq("id", action_id).eq("status", "pending").execute()
```

Then in `calendar_impl.py`:
```python
if conflicts:
    result = {
        "outcome": "conflict",
        "conflicts": conflicts,
        ...
    }
    queries.mark_bot_action_no_op(row["id"], result)
    return ok(result)
```

---

## 🔴 Blocker 3 — `_undo_row` for `cancel_meeting` retires the wrong row's lifecycle

**File**: `bot/agent/tools_meta.py:244-254` and `_restore_cancelled_meeting`
at lines 290-347.

**Current behavior** (when undoing a cancel):
1. `_restore_cancelled_meeting` runs → creates a new event, writes a
   `restore_schedule_meeting` row, AND calls
   `queries.retire_source_action(result["source_meeting_action_id"])`
   → original schedule row goes `success → undone`.
2. Back in `_undo_row`, the trailing
   `queries.retire_source_action(row["id"])` retires the cancel row →
   cancel row goes `success → undone`.

**Problem**: now both the original `schedule_meeting` row and the
`cancel_meeting` row are `undone`. But the user's mental model after
"schedule → cancel → undo cancel" is **"the meeting is back"** — they
expect a clean lineage where the original schedule conceptually returned
to `success`. With the current state, a follow-up "cancel that meeting
again" can't find a `success` schedule row and falls through.

**Fix**: don't retire the source schedule row inside
`_restore_cancelled_meeting`. The restore creates a NEW schedule audit
row (`action_type='restore_schedule_meeting'`) which is what subsequent
operations should reference. Remove this:

```python
async def _restore_cancelled_meeting(source_row, snapshot, calendar_id):
    ...
    if result.get("source_meeting_action_id"):
        queries.retire_source_action(result["source_meeting_action_id"])  # ← REMOVE
    ...
```

The original schedule row stays `undone` (it was correctly transitioned
to `undone` at the moment cancel succeeded — that's still true; that
event WAS deleted). The `restore_schedule_meeting` row is the new
`success` row representing the live meeting. `bot_known_events_for_attendee`
already includes both `schedule_meeting` and `restore_schedule_meeting`
in its filter so list_my_meetings will still find it.

Also: in `_undo_row` the `target_id = restored.get("event_id")`
assignment on line 254 is dead code (variable never read). Delete it.

---

## 🔴 Blocker 4 — `_restore_cancelled_meeting` invite-failure leaves state inconsistent

**File**: `bot/agent/tools_meta.py:336-345`

**Current code**:
```python
try:
    await calendar.invite_attendees(calendar_id, created["event_id"], attendees)
except Exception as e:
    queries.mark_bot_action_reconciled_unknown(
        restore_row["id"],
        reconciliation_kind="partial_success",
        error=f"restore_attendee_invite_failed: ...",
        keep_lock=True,
    )
    raise
```

**Problem**: the `raise` propagates back to `_undo_row`'s outer except,
so:
- `record_undo_audit(row)` is **never called** — no audit row written.
- `retire_source_action(row["id"])` is also **never called** — the
  cancel row stays `success`, but the `restore_row` is `reconciled_unknown`
  and the calendar event exists. Three rows in mutually inconsistent
  states.

**Fix**: don't raise. Return a structured partial-success indicator and
let `_undo_row` finish the audit/retire dance, just with a different
result payload:

```python
try:
    await calendar.invite_attendees(calendar_id, created["event_id"], attendees)
    restored = {**created, "attendees": attendees, "predecessor_action_id": source_row["id"]}
    queries.mark_bot_action_success(restore_row["id"], restored)
    return restored
except Exception as e:
    queries.mark_bot_action_reconciled_unknown(
        restore_row["id"],
        reconciliation_kind="partial_success",
        error=f"restore_attendee_invite_failed: {type(e).__name__}: {e}",
        keep_lock=True,
    )
    # Return the partial result; let _undo_row handle audit + retire
    # so the caller-facing message can include the partial-success warning.
    return {
        **created,
        "attendees": attendees,
        "predecessor_action_id": source_row["id"],
        "restore_partial": True,
        "restore_partial_reason": f"attendee_invite_failed: {type(e).__name__}: {e}",
    }
```

Then in `_undo_row`'s cancel branch, propagate the partial signal in the
returned status:

```python
elif action_type == "cancel_meeting":
    snapshot = result.get("pre_cancel_event_snapshot") or {}
    calendar_id = result.get("calendar_id") or snapshot.get("calendar_id")
    try:
        await calendar.get_event(calendar_id, target_id)
        restored = None
    except Exception as e:
        if "not" not in str(e).lower() and "404" not in str(e):
            raise
        restored = await _restore_cancelled_meeting(row, snapshot, calendar_id)
    # ... fall through to retire + audit (the unconditional tail) ...
```

And at the bottom of `_undo_row`, surface partial restore in the return:
```python
queries.retire_source_action(row["id"])
queries.record_undo_audit(row)
out = {"status": "undone", "source_action_id": row.get("id"), "source_action_type": action_type}
if action_type == "cancel_meeting" and isinstance(restored, dict) and restored.get("restore_partial"):
    out["restore_partial"] = True
    out["restore_partial_reason"] = restored["restore_partial_reason"]
return out
```

(`restored` only exists in the cancel branch — wrap the bottom logic
accordingly, or carry partial-info via a closure variable; whichever
reads cleaner.)

---

## 🔴 Blocker 5 — `today_iso` hard-codes `user_timezone="Asia/Shanghai"`

**File**: `bot/agent/tools_meta.py:101-120`

**Current code**:
```python
@tool("today_iso", "Return current UTC date and useful anchors. Also includes the asker open_id.", {})
async def today_iso(args: dict) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ok({
        ...
        "asker_open_id": ctx.sender_open_id,
        "user_timezone": "Asia/Shanghai",   # ← hard-coded
    })
```

**Problem**: spec §3.2 requires fetching the asker's timezone via
`feishu.contact.get_user(open_id=ctx.sender_open_id)`. Hard-coding
breaks every non-Shanghai user immediately — e.g. someone in PST asks
"我下午有啥会"; the bot computes "下午" against UTC+8 and returns the
wrong window.

**Fix**:
```python
async def today_iso(args: dict) -> dict[str, Any]:
    from feishu import contact

    user_tz = "UTC"
    user_today_local = None
    if ctx.sender_open_id:
        try:
            user = await contact.get_user(ctx.sender_open_id)
            user_tz = user.get("time_zone") or "UTC"
        except Exception:
            pass  # fall back to UTC silently — still better than wrong assumption

    now_utc = datetime.now(timezone.utc)
    today_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Compute today_start in the user's local timezone too, for "今天 / 昨天" framing.
    try:
        from zoneinfo import ZoneInfo
        local_now = now_utc.astimezone(ZoneInfo(user_tz))
        user_today_local = local_now.date().isoformat()
    except Exception:
        user_today_local = today_start_utc.date().isoformat()

    return ok({
        "now": now_utc.isoformat(),
        "today_start": today_start_utc.isoformat(),
        "yesterday_start": (today_start_utc - timedelta(days=1)).isoformat(),
        "yesterday_end": today_start_utc.isoformat(),
        "seven_days_ago": (now_utc - timedelta(days=7)).isoformat(),
        "thirty_days_ago": (now_utc - timedelta(days=30)).isoformat(),
        "asker_open_id": ctx.sender_open_id,
        "user_timezone": user_tz,
        "user_today_local": user_today_local,
    })
```

---

## 🔴 Blocker 6 — `append_to_doc` uses `parent_block_id="root"` literal which Feishu rejects

**Files**: `bot/agent/tools_impl/doc_impl.py:132,138-139`,
`bot/agent/tools_meta.py:275`

**Current code in doc_impl.py**:
```python
parent = "root"
blocks = _markdown_to_blocks(...)
await docx.list_child_blocks(token, parent)         # ← will 404
block_ids = await docx.append_blocks(token, parent, blocks, client_token=row["id"])
```

**Problem**: Feishu's docx API uses `document_id` itself as the root
parent block id, NOT the literal string `"root"`. Calling
`document_block_children.create(block_id="root")` returns
`InvalidBlockId` and the call fails 100% of the time.

The undo path inherits the same bug:
```python
# tools_meta.py:275
await docx.delete_blocks(target_id, result.get("parent_block_id") or "root", ...)
```

**Fix**: use `document_id` (== `token`) as the default parent. Spec §3.12
explicitly says "append at the document end" — that maps to appending
under the root block, which is `document_id`.

```python
async def append_to_doc(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("markdown_body"):
        return err("markdown_body is required")
    try:
        token = await _normalize_doc_token(args.get("doc_link_or_token") or "")
    except Exception as e:
        return err(str(e))
    if not queries.is_doc_authored_by_bot(token):
        return err("我只能改我自己创建的文档。这个文档不是我建的，请让我新建一个相关文档。")
    row, replay = start_action(ctx, "append_to_doc", args)
    if replay:
        return replay
    try:
        parent = token  # ← Feishu docx convention: document_id == root block_id
        blocks = _markdown_to_blocks(
            f"<!-- bot_action_id={row['id']} -->\n"
            + ((f"## {args['heading']}\n\n") if args.get("heading") else "")
            + args.get("markdown_body", "")
        )
        block_ids = await docx.append_blocks(token, parent, blocks, client_token=row["id"])
        result = {
            "appended_block_ids": block_ids,
            "parent_block_id": parent,
            "append_marker_block_id": block_ids[0] if block_ids else None,
        }
        queries.record_bot_action_target_pending(
            row["id"], target_id=token, target_kind="docx_block_append", result_patch=result
        )
        queries.mark_bot_action_success(row["id"], result)
        return ok({"doc_token": token, **result})
    except Exception as e:
        return fail_action(row, e)
```

(Also remove the now-pointless `await docx.list_child_blocks(token, parent)`
on line 138 — it was dead I/O, never used; the index-mapping list happens
inside `delete_blocks` at undo time.)

In `tools_meta.py:275`, change the fallback:
```python
elif action_type == "append_to_doc" and target_kind == "docx_block_append":
    parent = result.get("parent_block_id") or target_id   # ← fall back to doc itself, not "root"
    await docx.delete_blocks(target_id, parent, result.get("appended_block_ids") or [])
```

---

## 🟡 Medium 7 — `query_my_table` / `describe_my_table` use `table_exists` which lists every table

**File**: `bot/agent/tools_impl/bitable_impl.py:153-178`

**Current code**:
```python
async def query_my_table(ctx, args):
    ws, ws_err = workspace_or_error()
    if ws_err: return ws_err
    if not await bitable.table_exists(ws["base_app_token"], args["table_id"]):  # ← O(N) call
        return err("这张表不在我的工作台里，不能读取")
    ...
```

`bitable.table_exists` (line 149) calls `list_tables` which paginates
through every table in the base. For each query/describe call this is
O(N) Feishu API calls just to verify membership. Hits Feishu rate limits
quickly.

**Fix**: trust the workspace gate (we already verified `app_token ==
ws.base_app_token`) and let Feishu return NotFound naturally if the
table_id is bogus. Replace `table_exists` calls with a try/except around
the actual data call:

```python
async def query_my_table(ctx, args):
    ws, ws_err = workspace_or_error()
    if ws_err: return ws_err
    try:
        return ok(await bitable.search_records(
            app_token=ws["base_app_token"],
            table_id=args["table_id"],
            filter=args.get("filter") or None,
            page_size=min(int(args.get("page_size") or 50), 200),
            page_token=args.get("page_token") or None,
        ))
    except RuntimeError as e:
        if "NotFound" in str(e) or "not exist" in str(e).lower() or "1254" in str(e):
            return err("这张表不在我的工作台里，或者已被删除")
        return err(str(e))


async def describe_my_table(ctx, args):
    ws, ws_err = workspace_or_error()
    if ws_err: return ws_err
    try:
        fields = await bitable.list_fields(ws["base_app_token"], args["table_id"])
    except RuntimeError as e:
        if "NotFound" in str(e) or "not exist" in str(e).lower():
            return err("这张表不在我的工作台里，或者已被删除")
        raise
    return ok({"table_id": args["table_id"], "fields": fields})
```

Same fix for `append_to_my_table`'s `table_exists` precondition (line 133).

---

## 🟡 Medium 8 — `_get_client` walrus inside dict literal is too clever

**File**: `bot/agent/runner.py:215-221`

**Current code**:
```python
mcp_servers={
    "pmo_meta": build_meta_mcp(ctx := RequestContext()),
    "pmo_calendar": build_calendar_mcp(ctx),
    ...
},
...
slot = _PooledClient(client=client, ctx=ctx)
```

Walrus binding leaking out of a dict literal works but trips reviewers.
Make it explicit:

**Fix**:
```python
ctx = RequestContext()
options = ClaudeAgentOptions(
    system_prompt=SYSTEM_PROMPT,
    allowed_tools=[...],
    mcp_servers={
        "pmo_meta": build_meta_mcp(ctx),
        "pmo_calendar": build_calendar_mcp(ctx),
        "pmo_bitable": build_bitable_mcp(ctx),
        "pmo_doc": build_doc_mcp(ctx),
        "pmo_external": build_external_mcp(ctx),
    },
    disallowed_tools=[...],
    max_turns=settings.agent_max_duration_seconds,
)
client = ClaudeSDKClient(options=options)
await client.connect()
slot = _PooledClient(client=client, ctx=ctx)
```

---

## 🟡 Medium 9 — `_default_project_for_asker` runs expensive DB queries before Phase 0/1

**File**: `bot/agent/tools_impl/bitable_impl.py:13-20,194-213`

**Current**:
```python
async def append_action_items(ctx, args):
    items = args.get("items") or []
    if not items:
        return err("items is required")
    default_project = args.get("project") or _default_project_for_asker(ctx)  # ← DB calls
    missing_project = [item for item in items if not item.get("project") and not default_project]
    if missing_project:
        return ok({"needs_input": "project", ...})
    ws, ws_err = workspace_or_error()
    ...
    row, replay = start_action(ctx, "append_action_items", args)
```

`_default_project_for_asker` does `lookup_by_feishu_open_id` +
`recent_turns(limit=1000)` for every call, even on
duplicate-message replays (where `start_action` would have short-circuited).

**Fix**: gate the auto-project lookup behind `start_action` for the
replay-fast-path, OR compute it lazily only when actually needed:

```python
async def append_action_items(ctx, args):
    items = args.get("items") or []
    if not items:
        return err("items is required")

    # Cheap pre-check: if every item has its own project OR args has one,
    # we don't need the expensive auto-lookup at all.
    explicit_project = args.get("project")
    items_need_default = [item for item in items if not item.get("project")]

    default_project = explicit_project
    if items_need_default and not explicit_project:
        default_project = _default_project_for_asker(ctx)  # only when actually needed

    missing_project = [item for item in items_need_default if not default_project]
    if missing_project:
        return ok({
            "needs_input": "project",
            "items_pending": missing_project,
            "auto_suggestion": None,
            "auto_suggestion_confidence": "low",
            "agent_directive": "Ask the user which project these action items belong to before calling append_action_items again.",
        })

    ws, ws_err = workspace_or_error()
    if ws_err: return ws_err
    row, replay = start_action(ctx, "append_action_items", args)
    if replay: return replay
    ...
```

---

## 🟡 Medium 10 — Test coverage gaps

**Files**: `bot/tests/`

The 17 passing tests miss the bug surfaces above. Add at minimum:

**`bot/tests/test_start_action_state_machine.py`** (NEW):
- `test_message_replay_success_returns_cached_result`
- `test_message_replay_reconciled_unknown_surfaces_partial_signal`  ← would catch Blocker 1
- `test_message_replay_failed_does_not_auto_retry`                   ← would catch Blocker 1
- `test_logical_key_lock_dedup_for_success`
- `test_logical_key_lock_in_flight_returns_in_flight_error`
- `test_logical_key_lock_reconciled_unknown_surfaces_partial`        ← would catch Blocker 1

**`bot/tests/test_tools_calendar_schedule_meeting_more.py`** (extend):
- `test_freebusy_conflict_releases_lock_and_marks_no_op`             ← would catch Blocker 2
- `test_freebusy_conflict_then_reissue_creates_new_meeting_after_lock_release`

**`bot/tests/test_tools_calendar_cancel_meeting.py`** (NEW — currently no
cancel tests at all):
- `test_cancel_with_explicit_event_id`
- `test_cancel_with_last_true_uses_last_meeting_action`
- `test_cancel_persists_snapshot_before_delete`
- `test_cancel_then_undo_restores_event_with_partial_invite_warning`  ← would catch Blockers 3+4
- `test_cancel_then_undo_does_not_double_retire_schedule_row`         ← would catch Blocker 3

**`bot/tests/test_tools_doc_append.py`** (NEW):
- `test_append_to_doc_uses_document_id_as_parent_block`               ← would catch Blocker 6
- `test_append_to_doc_authorship_gate_refuses_external_doc`
- `test_undo_append_to_doc_deletes_only_appended_blocks`

**`bot/tests/test_tools_meta_today_iso.py`** (NEW):
- `test_today_iso_uses_user_timezone_from_contact_get_user`           ← would catch Blocker 5
- `test_today_iso_falls_back_to_utc_when_contact_get_user_fails`

---

## Summary

| # | Severity | File | Issue |
|---|---|---|---|
| 1 | 🔴 | `tools_impl/common.py:25-69` | `start_action` mishandles `reconciled_unknown` and `failed` |
| 2 | 🔴 | `tools_impl/calendar_impl.py:33-43` | Freebusy conflict written as `success` with lock held |
| 3 | 🔴 | `tools_meta.py:290-347` | `_restore_cancelled_meeting` over-retires the source schedule row |
| 4 | 🔴 | `tools_meta.py:336-345` | Restore invite-failure raises and skips audit/retire |
| 5 | 🔴 | `tools_meta.py:118` | `user_timezone` hard-coded to Asia/Shanghai |
| 6 | 🔴 | `tools_impl/doc_impl.py:132`, `tools_meta.py:275` | `parent_block_id="root"` is invalid; use `document_id` |
| 7 | 🟡 | `tools_impl/bitable_impl.py:133,157,175` | `table_exists` is O(N) Feishu calls per request |
| 8 | 🟡 | `agent/runner.py:215-221` | walrus inside dict literal — readability |
| 9 | 🟡 | `tools_impl/bitable_impl.py:17` | Auto-project DB lookup runs on every replay |
| 10 | 🟡 | `bot/tests/` | Missing tests for the 6 blockers above |

After 1-6 are fixed and 10 (the new tests covering them) passes, plus a
manual smoke-test against a real Feishu tenant, the implementation is
ready to ship. 7-9 are good-citizen cleanups.
