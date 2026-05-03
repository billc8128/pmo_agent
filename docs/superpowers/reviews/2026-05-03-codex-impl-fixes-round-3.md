# Round-3 fix list for Codex's implementation

> **Context**: Round 2 fixed all 8 issues from the previous review (one
> with a different-but-defensible approach for R2-1). 36 tests pass (was
> 25). Implementation is now in good shape.
>
> This round surfaces 3 minor polish items found while verifying the
> round-2 fixes. **None are blockers.** After these and a manual Feishu
> smoke test, the implementation is ready to ship.
>
> **Spec source of truth**: `docs/specs/2026-05-02-pmo-bot-write-tools-design.md`
> **Round-1 review**: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes.md`
> **Round-2 review**: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes-round-2.md`
> **Round-2 fix summary by Codex**: `docs/superpowers/reviews/2026-05-03-codex-impl-fixes-round-2-fix-summary.md`

---

## 🟡 Medium R3-1 — `_phone_variants` doesn't generate `+86<11-digits>` for bare Chinese mobile numbers

**File**: `bot/db/queries.py:133-139`

**Current code**:
```python
def _phone_variants(phone: str) -> list[str]:
    raw = (phone or "").strip()
    normalized = raw.lstrip("+").replace("-", "").replace(" ", "")
    variants = [v for v in {raw, normalized, f"+{normalized}"} if v]
    if normalized.startswith("86") and len(normalized) > 2:
        variants.append(normalized[2:])
    return sorted(set(variants))
```

**Problem**: covers `+86…` → strips → matches; covers `+86 138-…` →
normalizes → matches; but **does not** cover bare `13800138000` →
generate the `+86` variant. Feishu OAuth's userinfo typically returns
`+8613800138000`, so `feishu_links.feishu_mobile` is stored with the
country code. A user typing `"13800138000"` into a chat will not hit
the local fast path.

**Trace**:
- input: `"13800138000"`
- `raw = "13800138000"`, `normalized = "13800138000"`
- variants = `{"13800138000", "+13800138000"}`
- `normalized.startswith("86")` → False, no extra variant added
- DB row: `feishu_mobile = "+8613800138000"` → no match → falls through
  to remote `contact.batch_get_id_by_email_or_phone`

**Impact**: minor — the remote lookup still resolves, the user gets the
right answer. But the optimization Codex meant to provide doesn't fire
for the most common Chinese phone format. Wastes one Feishu API call
per name resolution from a Chinese phone.

**Fix**:

```python
def _phone_variants(phone: str) -> list[str]:
    raw = (phone or "").strip()
    if not raw:
        return []
    normalized = raw.lstrip("+").replace("-", "").replace(" ", "")
    variants = {raw, normalized, f"+{normalized}"}
    # Strip 86 country-code prefix
    if normalized.startswith("86") and len(normalized) > 2:
        bare = normalized[2:]
        variants.add(bare)
        variants.add(f"+{bare}")
    # Add 86 country-code prefix for bare 11-digit Chinese mobiles
    elif len(normalized) == 11 and normalized.startswith("1"):
        variants.add(f"86{normalized}")
        variants.add(f"+86{normalized}")
    return sorted(v for v in variants if v)
```

**Add tests** in `bot/tests/test_db_queries.py` (new file) or wherever
`_phone_variants` is reachable:

```python
from db.queries import _phone_variants


def test_phone_variants_generates_china_country_code_for_bare_11_digits():
    out = _phone_variants("13800138000")
    assert "+8613800138000" in out
    assert "8613800138000" in out
    assert "13800138000" in out


def test_phone_variants_strips_china_country_code_when_present():
    out = _phone_variants("+8613800138000")
    assert "13800138000" in out
    assert "+13800138000" in out
    assert "+8613800138000" in out
    assert "8613800138000" in out


def test_phone_variants_handles_dashes_and_spaces():
    out = _phone_variants("+86 138-0013-8000")
    # All four canonical forms should be generated
    assert "+8613800138000" in out
    assert "8613800138000" in out
    assert "+13800138000" in out
    assert "13800138000" in out


def test_phone_variants_empty_returns_empty_list():
    assert _phone_variants("") == []
    assert _phone_variants(None) == []
```

---

## 🟡 Medium R3-2 — `_success_replay` conflict-language gates on result content, not action_type

**File**: `bot/agent/tools_impl/common.py:11-22`

**Current code**:
```python
def _success_replay(row: dict[str, Any], *, logical_key_replay: bool = False) -> dict[str, Any]:
    payload = dict(row.get("result") or {})
    payload["cached_result"] = True
    if payload.get("outcome") == "conflict":
        payload["meeting_created"] = False
        payload["agent_directive"] = (
            "This cached result is a freebusy conflict, not a created meeting. "
            "Tell the user no meeting was created and ask for a different time or attendees."
        )
    if logical_key_replay:
        payload["deduplicated_from_logical_key"] = True
    return ok(payload)
```

**Problem**: `_success_replay` runs for ALL action types (schedule,
cancel, append_action_items, create_doc, etc.). Today, only
`schedule_meeting` writes `outcome=conflict` into its result. But the
gate keys on the field, not the action type — if a future tool happens
to use `outcome=conflict` in its result for an unrelated reason, it'll
inherit the meeting-specific message ("not a created meeting").

**Impact**: zero today; latent footgun for whoever adds the next tool.

**Fix**:
```python
def _success_replay(row: dict[str, Any], *, logical_key_replay: bool = False) -> dict[str, Any]:
    payload = dict(row.get("result") or {})
    payload["cached_result"] = True
    action_type = row.get("action_type")
    is_meeting_conflict = (
        action_type in {"schedule_meeting", "restore_schedule_meeting"}
        and payload.get("outcome") == "conflict"
    )
    if is_meeting_conflict:
        payload["meeting_created"] = False
        payload["agent_directive"] = (
            "This cached result is a freebusy conflict, not a created meeting. "
            "Tell the user no meeting was created and ask for a different time or attendees."
        )
    if logical_key_replay:
        payload["deduplicated_from_logical_key"] = True
    return ok(payload)
```

The corresponding test
(`test_start_action_conflict_logical_replay_says_no_meeting_was_created`
in `bot/tests/test_write_tools_impl.py:125-140`) needs the mocked row
to include `"action_type": "schedule_meeting"`:

```python
monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda *args: {
    "id": "act-1",
    "action_type": "schedule_meeting",      # ← add this
    "status": "success",
    "result": {"outcome": "conflict", "conflicts": [{"open_id": "ou_a"}]},
})
```

---

## 🟡 Medium R3-3 — `read_doc` doesn't clamp negative `max_chars`; negative slice silently truncates from the end

**File**: `bot/agent/tools_external.py:46-53`

**Current code**:
```python
requested_max_chars = int(args.get("max_chars") or 20000)
max_chars = min(requested_max_chars, 20000)
blocks = await docx.list_blocks(token)
markdown = "\n".join(filter(None, (_render_block(b) for b in blocks)))
char_count = len(markdown)
truncated = char_count > max_chars
if truncated:
    markdown = markdown[:max_chars] + f"\n\n[... 文档已截断，剩余 {char_count - max_chars} 字符]"
```

**Problem**: a misbehaving LLM passes `max_chars=-5`. Then:
- `int(-5) = -5` is truthy, so `requested_max_chars = -5`
- `max_chars = min(-5, 20000) = -5`
- `truncated = (char_count > -5)` — almost always True
- `markdown[:-5]` — Python negative slice — strips the LAST 5 characters
  rather than returning the first N

The user gets a doc body with the last few characters cut off, plus a
truncation banner saying "remaining N chars" where N is huge and
nonsensical.

**Impact**: rare edge case. But if an LLM ever produces `max_chars=-1`
or similar, the user sees confusing truncation. Cheap to defend.

**Fix**:
```python
_MIN_MAX_CHARS = 500
_MAX_MAX_CHARS = 20000

raw = args.get("max_chars")
requested_max_chars = int(raw) if raw is not None else _MAX_MAX_CHARS
max_chars = min(max(requested_max_chars, _MIN_MAX_CHARS), _MAX_MAX_CHARS)
```

Or, equivalently:
```python
requested_max_chars = int(args.get("max_chars") or 20000)
# Floor at 500 (still useful), ceiling at 20000 (token-budget safety)
max_chars = min(max(requested_max_chars, 500), 20000)
```

Add a test:
```python
def test_read_doc_clamps_negative_max_chars(monkeypatch):
    from feishu import docx
    fake_blocks = [
        MagicMock(block_type=2, text=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="hello world"))
        ])),
    ]
    monkeypatch.setattr(docx, "list_blocks", AsyncMock(return_value=fake_blocks))

    out = asyncio.run(
        _tool(RequestContext(), "read_doc")({"doc_link_or_token": "doc_xxx", "max_chars": -5})
    )
    payload = json.loads(out["content"][0]["text"])
    # The full body fits under 500, so no truncation should happen.
    assert payload["truncated"] is False
    assert payload["markdown"] == "hello world"
    assert payload["max_chars"] >= 500
```

---

## Summary

| # | Severity | File | Issue |
|---|---|---|---|
| **R3-1** | 🟡 | `db/queries.py:133-139` | `_phone_variants` misses bare 11-digit Chinese mobiles → wastes one Feishu API call when local DB has the binding |
| **R3-2** | 🟡 | `tools_impl/common.py:11-22` | `_success_replay` conflict-language gates on result content, not action_type — fragile coupling for future tools |
| **R3-3** | 🟡 | `tools_external.py:46-47` | `read_doc` doesn't clamp negative `max_chars`; Python negative slice truncates from the end |

None of these are blockers. R3-1 and R3-3 are 1-2 line fixes plus
4 new tests total. R3-2 is a 3-line clarification plus updating one
existing test fixture.

After these and a manual Feishu smoke test against a real tenant, the
implementation is ready to ship.

## Smoke test checklist (suggested next step)

The unit-test surface area is now solid (36 passing tests). What's
**not** covered by tests is the actual Feishu API contract — class
names resolve, but only a real tenant call confirms field shapes,
permission scopes, and rate-limit thresholds. Suggested smoke flow,
in order, against a dev tenant:

1. Run all migrations including `0012_feishu_links_mobile.sql`.
2. Run `python -m scripts.bootstrap_bot_workspace` from `bot/`.
3. Confirm `bot_workspace` row inserted; calendar/base/folder created
   in Feishu UI.
4. From a test chat: `@bot 今天几号` → `today_iso` returns user
   timezone from `contact.get_user`.
5. `@bot 帮我和 zhangwei 订下午 3 点会议` → `resolve_people` (name path
   via search_users GET), `schedule_meeting` Phase 0/1/2/3, event
   visible in Feishu calendar.
6. Same request again within 60s → freebusy conflict (probably) OR
   logical_key dedup → replay returns cached result, no second event.
7. `@bot 取消刚才那个会` → `cancel_meeting` last:true, snapshot
   persisted, event deleted.
8. `@bot 撤销` → `undo_last_action`, `_restore_cancelled_meeting`,
   event re-created.
9. `@bot 记一下 [#repo] 完成 X` → `append_action_items` writes a row
   to bot's bitable.
10. `@bot 起草一份 X 的纪要` → `create_doc` Path A 3-step.
11. Paste a Feishu doc URL the bot didn't create, ask `@bot 在末尾加
    一段` → `append_to_doc` authorship gate refuses.
12. `@bot 撤销刚才的纪要` → `undo_last_action` deletes docx + .md.
13. Force a logical_key collision: send 2 webhook bodies with same args
    but different `message_id` concurrently → exactly 1 Feishu side
    effect, 1 row in `bot_actions`.

If 1-12 pass, ship. If any fail, the failing one likely surfaces an
SDK contract mismatch we couldn't see without real tenant.
