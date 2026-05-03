# PMO Bot Write Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the read-only "包工头" Feishu bot into a real PMO assistant that can schedule/cancel meetings, append action items to Bitable, create meeting-notes Docx, resolve names from the Feishu directory — all with audit + undo.

**Architecture:** Bot is treated as a Feishu employee with its own calendar / Bitable base / Docx folder (no OAuth, all `tenant_access_token`). Write tools follow a Phase -1 / Phase 0 / Phase 1 / Phase 2 / Phase 3 pattern with intermediate persistence after each Feishu sub-step. Cross-process idempotency comes from `bot_actions` UNIQUE constraints + a `logical_key_locked` partial UNIQUE for short-window dedup. Undo is the safety net (no confirmation gates).

**Tech Stack:** Python 3.13, FastAPI, claude-agent-sdk, lark-oapi (Feishu Python SDK), supabase-py (PostgREST), httpx. Tests use pytest + respx (httpx mock) + a small in-memory PostgREST stub for `bot_actions` flows.

**Spec source of truth:** `docs/specs/2026-05-02-pmo-bot-write-tools-design.md` (3240 lines). When this plan is silent on a detail, fall back to the spec — especially §6.2 SQL helpers, §3.9 dispatch table, §5.1 skeleton, and §10 omissions table for "why this way".

**Working directory:** Run all commands from repo root (`/Users/a/Desktop/pmo_agent`).

---

## File Structure

### New files (created by this plan)

> **v5 update for spec v21**: now ships 18 tools across 5 MCP server
> modules (was 9 in single tools.py). Old `bot/agent/tools.py` is
> renamed to `bot/agent/tools_meta.py`; four new MCP module files
> are created. Feishu wrappers also expand to cover docx/wiki/links.

#### Schema + bootstrap

| Path | Purpose |
|---|---|
| `backend/supabase/migrations/0010_bot_workspace.sql` | Single-row config table for bot's calendar/base/folder ids |
| `backend/supabase/migrations/0011_bot_actions.sql` | Idempotency + audit + lock table |
| `bot/scripts/__init__.py` | (empty) |
| `bot/scripts/bootstrap_bot_workspace.py` | One-shot script to create bot's calendar / Bitable / Docs folder |

#### Agent infrastructure

| Path | Purpose |
|---|---|
| `bot/agent/request_context.py` | `RequestContext` dataclass — per-pooled-client mutable scope |
| `bot/agent/canonical_args.py` | `compute_logical_key` + canonicalization helpers (sorted-keys JSON, UTC time, asker auto-include, attendee dedup) |

#### Feishu SDK wrappers (split per Feishu API surface)

| Path | Purpose |
|---|---|
| `bot/feishu/auth.py` | Shared `tenant_access_token` issuer extracted from `feishu/client.py:67` |
| `bot/feishu/calendar.py` | Calendar v4 wrappers (calendar.create, primarys, freebusy.batch, calendar_event.create/get/delete/list, calendar_event_attendee.create) |
| `bot/feishu/bitable.py` | Bitable v1 wrappers (app.create/get, app_table.create/get/batch_delete, app_table_field.list, app_table_record.batch_create/batch_delete/search) plus a `bootstrap_base()` convenience |
| `bot/feishu/drive.py` | Drive v1 wrappers (file.upload_all/delete/create_folder, import_task.create/get) |
| `bot/feishu/docx.py` | Docx v1 wrappers (document_block.list, document_block_children.create/batch_delete) |
| `bot/feishu/contact.py` | Contact v3 wrappers (user.get, user.batch_get_id) + raw httpx wrapper for `/open-apis/search/v1/user` |
| `bot/feishu/wiki.py` | Wiki v2 wrapper (`wiki.v2.space.get_node` for `/wiki/<token>` redirect resolution) |
| `bot/feishu/links.py` | Pure-function URL parser used by `resolve_feishu_link`; calls `wiki.py` only for the wiki redirect path |

#### Agent tool modules (one MCP server per file)

| Path | Purpose |
|---|---|
| `bot/agent/tools_meta.py` | **Renamed from `tools.py`**. Hosts the 7 existing read tools (list_users, lookup_user, get_recent_turns, get_project_overview, get_activity_stats, today_iso extension, generate_image) + 3 new meta tools (resolve_people, undo_last_action, expanded today_iso). Exports `build_meta_mcp(ctx)` and `build_meta_tools(ctx)` for tests. |
| `bot/agent/tools_calendar.py` | schedule_meeting + cancel_meeting + list_my_meetings. Exports `build_calendar_mcp(ctx)` and `build_calendar_tools(ctx)`. |
| `bot/agent/tools_bitable.py` | append_action_items + query_action_items + create_bitable_table + append_to_my_table + query_my_table + describe_my_table. Exports `build_bitable_mcp(ctx)` and `build_bitable_tools(ctx)`. |
| `bot/agent/tools_doc.py` | create_meeting_doc + create_doc + append_to_doc + private `_drive_import_markdown(ctx, action_id, title, markdown)` helper. Exports `build_doc_mcp(ctx)` and `build_doc_tools(ctx)`. |
| `bot/agent/tools_external.py` | read_doc + read_external_table + resolve_feishu_link. Exports `build_external_mcp(ctx)` and `build_external_tools(ctx)`. |

#### Tests (one file per tool module + helpers)

| Path | Purpose |
|---|---|
| `bot/tests/__init__.py` | (empty) |
| `bot/tests/conftest.py` | pytest fixtures: in-memory bot_actions stub, fake `RequestContext`, time-freeze |
| `bot/tests/test_canonical_args.py` | logical_key hashing tests |
| `bot/tests/test_queries_bot_actions.py` | DB helper tests |
| `bot/tests/test_request_context.py` | RequestContext closure-capture sanity test |
| `bot/tests/test_feishu_auth.py` | tenant_access_token issuer (respx mock) |
| `bot/tests/test_feishu_calendar.py` | calendar.py wrapper tests |
| `bot/tests/test_feishu_bitable.py` | bitable.py wrapper tests |
| `bot/tests/test_feishu_drive.py` | drive.py wrapper tests |
| `bot/tests/test_feishu_docx.py` | docx.py wrapper tests |
| `bot/tests/test_feishu_contact.py` | contact.py wrapper tests (incl. raw httpx search_users) |
| `bot/tests/test_feishu_wiki.py` | wiki.py wrapper test |
| `bot/tests/test_feishu_links.py` | URL parser unit tests (docx / wiki / base / sheet patterns) |
| `bot/tests/test_tools_meta_resolve_people.py` | resolve_people 3-tier resolution |
| `bot/tests/test_tools_meta_today_iso.py` | today_iso extension (timezone via contact.user.get) |
| `bot/tests/test_tools_meta_undo_last_action.py` | undo dispatch (one test file per action_type arm) |
| `bot/tests/test_tools_calendar_schedule_meeting.py` | schedule_meeting Phase -1 / 0 / 1a / 1b / 2.X.5 / 3 |
| `bot/tests/test_tools_calendar_cancel_meeting.py` | cancel_meeting (last:true + explicit event_id + Phase 2a.5 snapshot) |
| `bot/tests/test_tools_calendar_list_my_meetings.py` | dual result sets, primarys lookup, rate-limit not applicable |
| `bot/tests/test_tools_bitable_append_action_items.py` | ambiguous flow, client_token, source_action_id marker |
| `bot/tests/test_tools_bitable_query_action_items.py` | basic query |
| `bot/tests/test_tools_bitable_create_table.py` | **NEW v21**: create_bitable_table (workspace gate, schema validation) |
| `bot/tests/test_tools_bitable_my_table.py` | **NEW v21**: append_to_my_table + query_my_table + describe_my_table (workspace gate, action_items_table_id refusal) |
| `bot/tests/test_tools_doc_create_meeting_doc.py` | Path A 3-step, partial paths |
| `bot/tests/test_tools_doc_create_doc.py` | **NEW v21**: create_doc (same Path A, no meeting linkage) |
| `bot/tests/test_tools_doc_append_to_doc.py` | **NEW v21**: append_to_doc (authorship gate, block_id capture, undo block-level delete) |
| `bot/tests/test_tools_external_read_doc.py` | **NEW v21**: read_doc (block-list → markdown render, truncation, 403 messaging) |
| `bot/tests/test_tools_external_read_external_table.py` | **NEW v21**: read_external_table (rate limit 5/h, page_size cap, 403) |
| `bot/tests/test_tools_external_resolve_feishu_link.py` | **NEW v21**: URL parsing for docx/wiki/base, wiki redirect via wiki.py |

### Modified files

| Path | Change |
|---|---|
| `bot/requirements.txt` | Add `pytest`, `pytest-asyncio`, `respx`, `freezegun` |
| `bot/feishu/client.py` | Replace inline `tenant_access_token` POST in `fetch_self_info` with `feishu/auth.py:get_tenant_access_token()` (no functional change) |
| `bot/db/queries.py` | Add `bot_workspace` + `bot_actions` helpers (~16 new functions, includes append_to_doc/read_doc support) |
| `bot/agent/tools.py` | **Renamed to `bot/agent/tools_meta.py`** — see Task 6.1. Module body becomes a factory `build_meta_mcp(ctx)` and loses `_current_conversation_key_var` + `set_current_conversation`. The 8 calendar/bitable/doc/external tools live in their own files. |
| `bot/agent/runner.py` | Add `RequestContext` per `_PooledClient`, `answer*` accept `message_id`/`chat_id`/`sender_open_id` kwargs. Replace single `mcp_servers={"pmo": ...}` with five servers (`pmo_meta`, `pmo_calendar`, `pmo_bitable`, `pmo_doc`, `pmo_external`). Expand `allowed_tools` with the new `mcp__pmo_<domain>__*` prefixes. Replace `SYSTEM_PROMPT` tool inventory. |
| `bot/agent/imaging.py` | (no change to signature; caller in tools_meta.py changes how it's called — uses `ctx.conversation_key`) |
| `bot/app.py` | (a) `_handle_message` calls `answer_streaming(...)` with new kwargs. (b) Line 255 (`tool_name.removeprefix("mcp__pmo__")`) updated to strip any of the 5 new prefixes (`mcp__pmo_meta__`, `mcp__pmo_calendar__`, `mcp__pmo_bitable__`, `mcp__pmo_doc__`, `mcp__pmo_external__`) before display. |
| `bot/README.md` | Document the 12 Feishu scopes that need to be enabled (was 10 — adds `wiki:wiki:readonly` and `docs:document.media:download` for v21 read-any) |

### Removed (pure deletions)

| Path / lines | Why |
|---|---|
| `bot/agent/tools.py:26-31` (`_current_conversation_key_var` + `set_current_conversation`) | Replaced by `RequestContext` closure (the file itself is renamed to `tools_meta.py`; deletion happens during the rename). |
| `bot/agent/runner.py` (`agent_tools.set_current_conversation(conversation_key)` call) | Same. |

---

## Conventions Used in This Plan

- **TDD**: Every behavior task is `write failing test → run → see fail → implement → run → see pass → commit`. Pure refactors with no behavioral change can skip the failing-test step but still must run existing tests.
- **Test mocks**:
  - `respx` for `httpx` (used by Feishu auth and contact search).
  - For lark-oapi calls, mock at the **resource method** level (e.g., `monkeypatch.setattr(client.calendar.v4.calendar_event, "create", fake)`) — do NOT mock `httpx` because lark-oapi uses `requests` underneath.
  - For Supabase client, mock at the `bot/db/client.py:sb_admin()` level — return a fake client whose `.table().insert().execute()` chain returns scripted responses.
- **Commits**: Each task ends with one atomic commit. Use `feat:` / `fix:` / `refactor:` / `test:` / `chore:` prefixes.
- **Branch**: Implement on `main` (project is small, single-developer; no PR flow).
- **Verification**: After each migration / SDK wrapper / tool, run the relevant test file. Smoke tests in Task Group 11 run against a real Feishu dev tenant.

---

# Task Group 0: Test Infrastructure (1 task)

### Task 0.1: Set up pytest + respx + freezegun

**Files:**
- Modify: `bot/requirements.txt`
- Create: `bot/tests/__init__.py`
- Create: `bot/tests/conftest.py`
- Create: `bot/pytest.ini`

- [ ] **Step 1: Add test dependencies**

Append to `bot/requirements.txt`:
```
pytest>=8.0.0
pytest-asyncio>=0.24.0
respx>=0.21.0
freezegun>=1.5.0
```

- [ ] **Step 2: Install**

Run from `bot/`:
```bash
pip install -r requirements.txt
```

- [ ] **Step 3: Create `bot/pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
python_files = test_*.py
python_functions = test_*
filterwarnings =
    ignore::DeprecationWarning
```

- [ ] **Step 4: Create `bot/tests/__init__.py`** (empty file)

- [ ] **Step 5: Create `bot/tests/conftest.py` with two fixtures**

```python
"""Shared pytest fixtures for the bot test suite."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeRequestContext:
    """In-test stand-in for RequestContext. Mirrors the real one's shape."""
    message_id: str = "msg_test_123"
    chat_id: str = "oc_test_chat"
    sender_open_id: str = "ou_test_sender"
    conversation_key: str = "oc_test_chat:ou_test_sender"


@pytest.fixture
def fake_ctx() -> FakeRequestContext:
    return FakeRequestContext()


@pytest.fixture
def fake_bot_actions() -> dict[str, dict[str, Any]]:
    """In-memory dict keyed by `bot_actions.id`. Tests can inspect / mutate."""
    return {}
```

- [ ] **Step 6: Verify pytest works**

```bash
cd bot && pytest --collect-only
```

Expected: collects 0 items, exits 0.

- [ ] **Step 7: Commit**

```bash
git add bot/requirements.txt bot/pytest.ini bot/tests/
git commit -m "chore: bootstrap pytest + respx + freezegun for write-tools tests"
```

---

# Task Group 1: Feishu Permission Scope Verification (1 task)

> **Spec ref:** §11 step 0, §8.

### Task 1.1: Verify Feishu scope names against installed SDK + admin console

This is a manual / human task; the plan documents it for completeness. The implementer must:

- [ ] **Step 1: Install lark-cli locally for verification only**

```bash
npm install -g @larksuite/cli
```

- [ ] **Step 2: Inspect calendar skill manifest for the canonical scope names**

Open `https://raw.githubusercontent.com/larksuite/cli/main/skills/lark-calendar/SKILL.md`
in a browser; record the exact strings for:
- calendar (own)
- calendar.event:* / calendar.event.attendee:*
- freebusy:read (note: spec lists this as TBD — could be `freebusy` or `free_busy`)

- [ ] **Step 3: In the Feishu open-platform admin console, enable**

Per spec §8 (verify each before pasting):
- `im:*` (existing)
- `calendar:calendar`
- `calendar:calendar.event:*`
- `calendar:calendar.event.attendee:*`
- TBD freebusy scope (verified in Step 2)
- `bitable:app`
- `docx:document`
- `drive:drive`
- `contact:user.base:readonly`
- `contact:contact:readonly`

- [ ] **Step 4: Publish a new app version**

Click "发布版本" in the Feishu admin console.

- [ ] **Step 5: Document in bot/README.md**

Append a section "Feishu permission scopes" listing the 10 scopes (with verified spelling) and a note that re-running this checklist is required when the spec adds new SDK calls.

- [ ] **Step 6: Commit**

```bash
git add bot/README.md
git commit -m "docs: list required Feishu scopes for write tools"
```

---

# Task Group 2: Schema Migrations (4 tasks)

> **Spec ref:** §6.1, §6.2.

### Task 2.1: Migration 0010 — bot_workspace

**Files:**
- Create: `backend/supabase/migrations/0010_bot_workspace.sql`

- [ ] **Step 1: Write the migration**

```sql
-- backend/supabase/migrations/0010_bot_workspace.sql
-- Single-row config: bot's own Feishu workspace identifiers.
-- The bot creates a primary calendar, a Bitable base "包工头的工作台",
-- and a Drive folder "包工头的文档柜" once at deploy time. The IDs are
-- stored here so all write tools resolve them at runtime.

CREATE TABLE bot_workspace (
    id                       smallint PRIMARY KEY CHECK (id = 1),
    calendar_id              text NOT NULL,
    base_app_token           text NOT NULL,
    action_items_table_id    text NOT NULL,
    meetings_table_id        text NOT NULL,
    docs_folder_token        text NOT NULL,
    bootstrapped_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE bot_workspace ENABLE ROW LEVEL SECURITY;
-- Service role only; no end-user policy.
```

- [ ] **Step 2: Apply locally (Supabase CLI or psql)**

If using Supabase CLI:
```bash
cd backend && supabase db push
```

Otherwise psql:
```bash
psql "$SUPABASE_DB_URL" -f backend/supabase/migrations/0010_bot_workspace.sql
```

Expected: no errors.

- [ ] **Step 3: Verify**

```bash
psql "$SUPABASE_DB_URL" -c "SELECT * FROM bot_workspace;"
```

Expected: empty table, 0 rows.

- [ ] **Step 4: Commit**

```bash
git add backend/supabase/migrations/0010_bot_workspace.sql
git commit -m "feat(db): add bot_workspace table (migration 0010)"
```

### Task 2.2: Migration 0011 — bot_actions table

**Files:**
- Create: `backend/supabase/migrations/0011_bot_actions.sql`

- [ ] **Step 1: Write the migration (full schema from spec §6.2)**

```sql
-- backend/supabase/migrations/0011_bot_actions.sql
-- Idempotency + audit + lock log for write tools.
-- See docs/specs/2026-05-02-pmo-bot-write-tools-design.md §6.2 for the
-- full design rationale (status enum, lock-behavior table, partial UNIQUE).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE bot_actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      text NOT NULL,
    chat_id         text NOT NULL,
    sender_open_id  text NOT NULL,
    logical_key     text NOT NULL,
    attempt_count   int  NOT NULL DEFAULT 1,
    action_type     text NOT NULL,
    status          text NOT NULL CHECK (
                      status IN ('pending','success','failed','undone','reconciled_unknown')
                    ),
    logical_key_locked boolean NOT NULL DEFAULT true,
    args            jsonb NOT NULL,
    target_id       text,
    target_kind     text,
    result          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bot_actions_message_action_uniq
      UNIQUE (message_id, action_type)
);

CREATE INDEX bot_actions_target_idx ON bot_actions (target_kind, target_id);

-- §5.3 stuck-pending GC predicate uses updated_at (update_for_retry
-- bumps it; the GC clock should restart from that retry).
CREATE INDEX bot_actions_pending_idx ON bot_actions (status, updated_at)
  WHERE status = 'pending';

CREATE INDEX bot_actions_chat_sender_recent_idx
  ON bot_actions (chat_id, sender_open_id, created_at DESC);

-- §5.2 logical-key cross-process exclusion. At most ONE row per
-- logical_key may currently hold the dedup lock AND be in an
-- active-or-orphan state. The status IN clause includes
-- reconciled_unknown so partial_success orphans block duplicates
-- (see §6.2 Lock-behavior on status transitions).
CREATE UNIQUE INDEX bot_actions_logical_locked_uniq
  ON bot_actions (logical_key)
  WHERE logical_key_locked = true
    AND status IN ('pending', 'success', 'reconciled_unknown');

ALTER TABLE bot_actions ENABLE ROW LEVEL SECURITY;
-- Service role only; no end-user policy.
```

- [ ] **Step 2: Apply locally**

```bash
psql "$SUPABASE_DB_URL" -f backend/supabase/migrations/0011_bot_actions.sql
```

Expected: no errors.

- [ ] **Step 3: Verify constraint name appears in PostgREST 409 messages**

```bash
psql "$SUPABASE_DB_URL" <<EOF
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args)
VALUES ('m1', 'c1', 's1', 'lk1', 'schedule_meeting', 'pending', '{}'::jsonb);
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args)
VALUES ('m1', 'c1', 's1', 'lk2', 'schedule_meeting', 'pending', '{}'::jsonb);
EOF
```

Expected: second INSERT errors with message containing `bot_actions_message_action_uniq`.

- [ ] **Step 4: Clean up the test row**

```bash
psql "$SUPABASE_DB_URL" -c "DELETE FROM bot_actions WHERE message_id='m1';"
```

- [ ] **Step 5: Commit**

```bash
git add backend/supabase/migrations/0011_bot_actions.sql
git commit -m "feat(db): add bot_actions table with idempotency + lock invariants (migration 0011)"
```

### Task 2.3: Verify partial UNIQUE on logical_key

- [ ] **Step 1: Manual verification**

```bash
psql "$SUPABASE_DB_URL" <<EOF
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m1', 'c1', 's1', 'sharedkey', 'schedule_meeting', 'success', '{}'::jsonb, true);
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m2', 'c1', 's1', 'sharedkey', 'schedule_meeting', 'pending', '{}'::jsonb, true);
EOF
```

Expected: second INSERT errors with `bot_actions_logical_locked_uniq` in message.

- [ ] **Step 2: Verify it allows insertion when lock is released**

```bash
psql "$SUPABASE_DB_URL" <<EOF
DELETE FROM bot_actions WHERE message_id IN ('m1','m2');
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m1', 'c1', 's1', 'sharedkey', 'schedule_meeting', 'success', '{}'::jsonb, false);
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m2', 'c1', 's1', 'sharedkey', 'schedule_meeting', 'pending', '{}'::jsonb, true);
DELETE FROM bot_actions WHERE message_id IN ('m1','m2');
EOF
```

Expected: both INSERTs succeed (first has lock=false so falls outside partial UNIQUE).

- [ ] **Step 3: No commit (verification only)**

### Task 2.4: Verify failed/undone rows fall outside partial UNIQUE

- [ ] **Step 1: Verify failed and reconciled_unknown stay isolated**

```bash
psql "$SUPABASE_DB_URL" <<EOF
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m1', 'c1', 's1', 'sharedkey2', 'schedule_meeting', 'failed', '{}'::jsonb, true);
-- This should succeed: 'failed' is excluded from partial UNIQUE
INSERT INTO bot_actions (message_id, chat_id, sender_open_id, logical_key, action_type, status, args, logical_key_locked)
VALUES ('m2', 'c1', 's1', 'sharedkey2', 'schedule_meeting', 'pending', '{}'::jsonb, true);
DELETE FROM bot_actions WHERE message_id IN ('m1','m2');
EOF
```

Expected: both INSERTs succeed (the partial UNIQUE includes only `pending|success|reconciled_unknown`).

- [ ] **Step 2: No commit**

---

# Task Group 3: db/queries.py — bot_actions helpers (11 tasks)

> **Spec ref:** §6.2 SQL helpers, §11 step 2.

### Task 3.1: compute_logical_key + canonical_args

**Files:**
- Create: `bot/agent/canonical_args.py`
- Test: `bot/tests/test_canonical_args.py`

- [ ] **Step 1: Write the failing test**

`bot/tests/test_canonical_args.py`:
```python
from datetime import timezone

from agent.canonical_args import canonicalize_args, compute_logical_key


def test_canonical_orders_dict_keys():
    a = canonicalize_args({"b": 2, "a": 1, "c": [3, 1, 2]})
    b = canonicalize_args({"a": 1, "c": [3, 1, 2], "b": 2})
    assert a == b


def test_canonical_normalizes_start_time_to_utc():
    a = canonicalize_args({"start_time": "2026-05-08T15:00:00+08:00"}, action_type="schedule_meeting")
    b = canonicalize_args({"start_time": "2026-05-08T07:00:00+00:00"}, action_type="schedule_meeting")
    assert a == b


def test_logical_key_stable_across_arg_order():
    k1 = compute_logical_key(
        chat_id="c", sender_open_id="s", action_type="schedule_meeting",
        canonical_args={"title": "X", "start_time": "2026-05-08T15:00:00+08:00"},
    )
    k2 = compute_logical_key(
        chat_id="c", sender_open_id="s", action_type="schedule_meeting",
        canonical_args={"start_time": "2026-05-08T15:00:00+08:00", "title": "X"},
    )
    assert k1 == k2
    assert isinstance(k1, str)
    assert len(k1) == 64  # sha256 hex


def test_canonical_fills_default_duration_for_schedule():
    """duration_minutes omitted vs explicitly passed 30 must collide."""
    a = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"]},
        action_type="schedule_meeting",
    )
    b = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"], "duration_minutes": 30},
        action_type="schedule_meeting",
    )
    assert a == b


def test_canonical_fills_default_include_asker():
    a = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"]},
        action_type="schedule_meeting",
    )
    b = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"], "include_asker": True},
        action_type="schedule_meeting",
    )
    assert a == b


def test_canonical_sorts_attendee_open_ids():
    """User saying "和 albert 和 bcc" vs "和 bcc 和 albert" must collide."""
    a = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_albert", "ou_bcc"]},
        action_type="schedule_meeting",
    )
    b = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_bcc", "ou_albert"]},
        action_type="schedule_meeting",
    )
    assert a == b


def test_canonical_dedupes_attendee_open_ids():
    """Doubling an attendee accidentally must collide."""
    a = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a", "ou_a", "ou_b"]},
        action_type="schedule_meeting",
    )
    b = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a", "ou_b"]},
        action_type="schedule_meeting",
    )
    assert a == b


def test_canonical_distinct_when_truly_different():
    """Different titles still produce different keys (sanity)."""
    a = canonicalize_args(
        {"title": "X", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"]},
        action_type="schedule_meeting",
    )
    b = canonicalize_args(
        {"title": "Y", "start_time": "2026-05-08T15:00:00+08:00",
         "attendee_open_ids": ["ou_a"]},
        action_type="schedule_meeting",
    )
    assert a != b
```

- [ ] **Step 2: Run, expect ImportError**

```bash
cd bot && pytest tests/test_canonical_args.py -v
```

Expected: ModuleNotFoundError on `agent.canonical_args`.

- [ ] **Step 3: Implement**

`bot/agent/canonical_args.py`:
```python
"""Pure-function helpers for the §5.2 logical_key dedup window.

`compute_logical_key` produces a stable hash that identifies "the same
logical request from a human's POV" across two messages. See spec §5.2
for the dedup contract. `canonicalize_args` makes the hash insensitive
to JSON key ordering and timezone representation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonicalize_args(args: dict[str, Any], *, action_type: str | None = None) -> dict[str, Any]:
    """Return args with stable key order, normalized values, AND
    per-action-type default-filling so the same logical request
    produces the same logical_key regardless of whether the user
    typed it the long way or the short way.

    Per Codex plan-review iter-3 #3:
      - duration_minutes=30 omitted vs explicitly passed → same key
      - include_asker omitted vs True → same key
      - reminder_minutes=15 omitted vs explicit → same key
      - attendee_open_ids ["ou_a","ou_b"] vs ["ou_b","ou_a"] → same key
        (sorted-and-deduplicated)

    Without this, a user retyping "和 albert 订下周三 3 点 30 分钟" vs
    "和 albert 订下周三 3 点" would slip past the 60s dedup window.
    """
    out = dict(args)

    if action_type == "schedule_meeting":
        # 1. Default-fill the optional fields.
        out.setdefault("duration_minutes", 30)
        out.setdefault("reminder_minutes", 15)
        out.setdefault("include_asker", True)
        out.setdefault("description", "")

        # 2. Normalize start_time to UTC so +08:00 / +00:00 collide.
        if "start_time" in out:
            try:
                dt = datetime.fromisoformat(out["start_time"])
                if dt.tzinfo is not None:
                    out["start_time"] = dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass  # downstream validation catches it

        # 3. Sort + dedupe attendee_open_ids (set semantics — order
        #    doesn't matter to a human asking "和 albert + bcc 订会").
        if "attendee_open_ids" in out and isinstance(out["attendee_open_ids"], list):
            out["attendee_open_ids"] = sorted(set(out["attendee_open_ids"]))

    elif action_type == "append_action_items":
        # Per-item canonicalization: sort items by title, normalize
        # owner_open_id casing.
        if "items" in out and isinstance(out["items"], list):
            out["items"] = sorted(
                [{**item, "title": (item.get("title") or "").strip()}
                 for item in out["items"]],
                key=lambda i: (i.get("project") or "", i.get("title") or ""),
            )

    elif action_type == "create_meeting_doc":
        out.setdefault("meeting_event_id", None)

    # cancel_meeting / undo_last_action / list_my_meetings / query_action_items
    # / resolve_people / today_iso don't need additional canonicalization;
    # their logical identity matches their literal args.

    # Final stable serialization
    return json.loads(json.dumps(out, sort_keys=True))


def compute_logical_key(
    *, chat_id: str, sender_open_id: str, action_type: str,
    canonical_args: dict[str, Any],
) -> str:
    """SHA-256 hex of the canonical tuple. See spec §5.2."""
    payload = json.dumps(
        [chat_id, sender_open_id, action_type, canonical_args],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run, expect pass**

```bash
pytest tests/test_canonical_args.py -v
```

- [ ] **Step 5: Commit**

```bash
git add bot/agent/canonical_args.py bot/tests/test_canonical_args.py
git commit -m "feat(agent): canonical_args + compute_logical_key for §5.2 dedup"
```

### Task 3.2: bot_workspace queries

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py` (start of file)

- [ ] **Step 1: Write the failing test**

Append to `bot/tests/test_queries_bot_actions.py`:
```python
"""Tests for db/queries.py bot_workspace and bot_actions helpers.

Mocks bot/db/client.py:sb_admin() so tests don't hit a live Supabase.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from db import queries


@pytest.fixture
def fake_admin(monkeypatch):
    """Replace sb_admin() with a MagicMock chain."""
    fake = MagicMock()
    monkeypatch.setattr("db.queries.sb_admin", lambda: fake)
    return fake


def test_get_bot_workspace_returns_row(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": 1, "calendar_id": "cal_x", "base_app_token": "app_x",
        "action_items_table_id": "tbl_a", "meetings_table_id": "tbl_m",
        "docs_folder_token": "fld_x",
    }
    ws = queries.get_bot_workspace()
    assert ws["calendar_id"] == "cal_x"
    fake_admin.table.assert_called_with("bot_workspace")


def test_get_bot_workspace_returns_none_when_unbootstrapped(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = None
    assert queries.get_bot_workspace() is None
```

- [ ] **Step 2: Run, expect AttributeError**

```bash
pytest tests/test_queries_bot_actions.py::test_get_bot_workspace_returns_row -v
```

- [ ] **Step 3: Implement**

Append to `bot/db/queries.py`:
```python
# ── bot_workspace (§6.1) ─────────────────────────────────────────────


def get_bot_workspace() -> Optional[dict[str, Any]]:
    """Return the single bot_workspace row, or None if not bootstrapped."""
    res = (
        sb_admin()
        .table("bot_workspace")
        .select("*")
        .eq("id", 1)
        .maybe_single()
        .execute()
    )
    return res.data if res and res.data else None


def upsert_bot_workspace(
    *, calendar_id: str, base_app_token: str,
    action_items_table_id: str, meetings_table_id: str,
    docs_folder_token: str,
) -> None:
    """One-shot insert from the bootstrap script. id is always 1."""
    sb_admin().table("bot_workspace").upsert({
        "id": 1,
        "calendar_id": calendar_id,
        "base_app_token": base_app_token,
        "action_items_table_id": action_items_table_id,
        "meetings_table_id": meetings_table_id,
        "docs_folder_token": docs_folder_token,
    }).execute()
```

Add `from .client import sb, sb_admin` if not already present.

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git add bot/db/queries.py bot/tests/test_queries_bot_actions.py
git commit -m "feat(db): get_bot_workspace + upsert_bot_workspace helpers"
```

### Task 3.3: insert_bot_action_pending + constraint dispatch

> **Depends on:** Task 2.2 migration (the named UNIQUE constraint `bot_actions_message_action_uniq` and the partial UNIQUE `bot_actions_logical_locked_uniq` must exist for the regex dispatch to find them in PostgREST 409 messages).

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing tests for the happy path and both conflict types**

Append to `bot/tests/test_queries_bot_actions.py`:
```python
def test_insert_pending_returns_row(fake_admin):
    fake_admin.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "uuid-1", "status": "pending"}
    ]
    row = queries.insert_bot_action_pending(
        message_id="m1", chat_id="c1", sender_open_id="s1",
        action_type="schedule_meeting",
        args={"title": "X"}, logical_key="lk1",
    )
    assert row["id"] == "uuid-1"


def test_insert_pending_message_conflict_raises_message_conflict(fake_admin, monkeypatch):
    """409 with bot_actions_message_action_uniq → MessageActionConflict."""
    from postgrest.exceptions import APIError
    err = APIError({
        "code": "23505",
        "message": 'duplicate key value violates unique constraint "bot_actions_message_action_uniq"',
    })
    fake_admin.table.return_value.insert.return_value.execute.side_effect = err
    # The helper does a follow-up SELECT to fetch the existing row.
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "uuid-existing", "status": "success", "result": {"x": 1},
    }
    with pytest.raises(queries.MessageActionConflict) as exc:
        queries.insert_bot_action_pending(
            message_id="m1", chat_id="c1", sender_open_id="s1",
            action_type="schedule_meeting",
            args={"title": "X"}, logical_key="lk1",
        )
    assert exc.value.existing_row["id"] == "uuid-existing"


def test_insert_pending_logical_conflict_raises_logical_conflict(fake_admin):
    from postgrest.exceptions import APIError
    err = APIError({
        "code": "23505",
        "message": 'duplicate key value violates unique constraint "bot_actions_logical_locked_uniq"',
    })
    fake_admin.table.return_value.insert.return_value.execute.side_effect = err
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "uuid-winner", "status": "success", "result": {"y": 2},
    }
    with pytest.raises(queries.LogicalKeyConflict) as exc:
        queries.insert_bot_action_pending(
            message_id="m2", chat_id="c1", sender_open_id="s1",
            action_type="schedule_meeting",
            args={"title": "X"}, logical_key="lk1",
        )
    assert exc.value.existing_row["status"] == "success"
```

- [ ] **Step 2: Run, expect failures (helper not defined)**

- [ ] **Step 3: Implement**

Append to `bot/db/queries.py`:
```python
# ── bot_actions (§6.2) ───────────────────────────────────────────────

import re
from typing import NoReturn


class BotActionInsertConflict(Exception):
    """Base. Caller should treat as a hard error if a subclass isn't raised."""
    def __init__(self, existing_row: dict[str, Any] | None = None,
                 raw_error: Any = None):
        self.existing_row = existing_row
        self.raw_error = raw_error


class MessageActionConflict(BotActionInsertConflict):
    """409 on bot_actions_message_action_uniq — same (message_id, action_type)."""


class LogicalKeyConflict(BotActionInsertConflict):
    """409 on bot_actions_logical_locked_uniq — different message, same logical_key."""


_CONSTRAINT_RE = re.compile(r'unique constraint "([^"]+)"')


def _extract_constraint_name(error_message: str) -> str | None:
    m = _CONSTRAINT_RE.search(error_message)
    return m.group(1) if m else None


def insert_bot_action_pending(
    *, message_id: str, chat_id: str, sender_open_id: str,
    action_type: str, args: dict[str, Any], logical_key: str,
    target_id: str | None = None, target_kind: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """INSERT a pending row. Raises MessageActionConflict / LogicalKeyConflict
    on UNIQUE violations, with the existing row already fetched.

    See spec §6.2 "Constraint names" for the contract.
    """
    from postgrest.exceptions import APIError

    payload = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_open_id": sender_open_id,
        "action_type": action_type,
        "logical_key": logical_key,
        "status": "pending",
        "logical_key_locked": True,
        "args": args,
        "target_id": target_id,
        "target_kind": target_kind,
        "result": result,
    }
    try:
        res = sb_admin().table("bot_actions").insert(payload).execute()
        return res.data[0]
    except APIError as e:
        msg = str(e.message if hasattr(e, "message") else e)
        constraint = _extract_constraint_name(msg)
        if constraint == "bot_actions_message_action_uniq":
            existing = (
                sb_admin().table("bot_actions").select("*")
                .eq("message_id", message_id).eq("action_type", action_type)
                .maybe_single().execute().data
            )
            raise MessageActionConflict(existing_row=existing, raw_error=e)
        elif constraint == "bot_actions_logical_locked_uniq":
            existing = (
                sb_admin().table("bot_actions").select("*")
                .eq("logical_key", logical_key).eq("logical_key_locked", True)
                .maybe_single().execute().data
            )
            raise LogicalKeyConflict(existing_row=existing, raw_error=e)
        raise BotActionInsertConflict(raw_error=e)
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git add bot/db/queries.py bot/tests/test_queries_bot_actions.py
git commit -m "feat(db): insert_bot_action_pending with PostgREST constraint dispatch"
```

### Task 3.4: get_bot_action + content-aware lazy GC

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing tests for the GC content-aware classification**

Append:
```python
from freezegun import freeze_time


def test_get_bot_action_returns_row_unchanged_when_fresh(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "pending", "created_at": "2026-05-03T10:00:00Z",
        "target_id": None, "result": {},
    }
    with freeze_time("2026-05-03T10:01:00Z"):
        row = queries.get_bot_action("m1", "schedule_meeting")
    assert row["status"] == "pending"


def test_get_bot_action_promotes_stuck_pending_to_partial_with_target(fake_admin):
    """5+ min pending row WITH a target_id → reconciled_unknown(partial_success), lock kept."""
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "pending", "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": "evt_xyz", "result": {},
    }
    fake_admin.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "reconciled_unknown",
        "result": {"reconciliation_kind": "partial_success"},
        "logical_key_locked": True, "target_id": "evt_xyz",
    }]
    with freeze_time("2026-05-03T10:06:00Z"):
        row = queries.get_bot_action("m1", "schedule_meeting")
    assert row["status"] == "reconciled_unknown"
    assert row["result"]["reconciliation_kind"] == "partial_success"
    assert row["logical_key_locked"] is True


def test_get_bot_action_promotes_stuck_pending_to_stuck_when_no_handle(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "pending", "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": None, "result": {},
    }
    fake_admin.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "reconciled_unknown",
        "result": {"reconciliation_kind": "stuck_pending"},
        "logical_key_locked": False, "target_id": None,
    }]
    with freeze_time("2026-05-03T10:06:00Z"):
        row = queries.get_bot_action("m1", "schedule_meeting")
    assert row["result"]["reconciliation_kind"] == "stuck_pending"
    assert row["logical_key_locked"] is False
```

- [ ] **Step 2: Run, expect failures**

- [ ] **Step 3: Implement**

The PostgREST builder doesn't expose CASE expressions cleanly, so we run a raw SQL via the Supabase RPC pattern OR use two separate UPDATEs based on a Python-side decision. Given supabase-py limitations, do the decision in Python and pick which UPDATE to issue:

```python
from datetime import datetime, timezone, timedelta


_STUCK_PENDING_THRESHOLD = timedelta(minutes=5)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_artifact_handle(row: dict[str, Any]) -> bool:
    if row.get("target_id"):
        return True
    result = row.get("result") or {}
    return "import_ticket" in result or "source_file_token" in result


def _lazy_gc_stuck_pending(row: dict[str, Any]) -> dict[str, Any]:
    """If `row` is pending-and-aged, promote to reconciled_unknown.

    Content-aware: rows with any artifact handle become partial_success
    (lock kept); rows with no handle become stuck_pending (lock cleared).
    See spec §5.3 case (a).

    Returns the updated row, or the original if no GC was needed.
    """
    if row.get("status") != "pending":
        return row
    age_source = row.get("updated_at") or row.get("created_at")
    if not age_source:
        return row
    age = datetime.now(timezone.utc) - datetime.fromisoformat(
        age_source.replace("Z", "+00:00")
    )
    if age < _STUCK_PENDING_THRESHOLD:
        return row

    has_handle = _has_artifact_handle(row)
    kind = "partial_success" if has_handle else "stuck_pending"
    new_result = {**(row.get("result") or {}), "reconciliation_kind": kind}
    update = (
        sb_admin().table("bot_actions").update({
            "status": "reconciled_unknown",
            "error": "reconciled: pending too long",
            "result": new_result,
            "logical_key_locked": has_handle,
            "updated_at": _utc_now_iso(),
        }).eq("id", row["id"]).eq("status", "pending").execute()
    )
    if update.data:
        return update.data[0]
    # Lost the race — re-read the row.
    return (
        sb_admin().table("bot_actions").select("*")
        .eq("id", row["id"]).maybe_single().execute().data or row
    )


def get_bot_action(message_id: str, action_type: str) -> dict[str, Any] | None:
    """Look up by (message_id, action_type), with lazy stuck-pending GC.

    Both `_handle_message` (Phase 1a) and `get_locked_by_logical_key`
    must call this so a stuck pending row is reconciled regardless of
    which path discovers it (spec §5.3 + iter-14 #1).
    """
    row = (
        sb_admin().table("bot_actions").select("*")
        .eq("message_id", message_id).eq("action_type", action_type)
        .maybe_single().execute().data
    )
    if not row:
        return None
    return _lazy_gc_stuck_pending(row)
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): get_bot_action + content-aware stuck-pending GC"
```

### Task 3.5: get_locked_by_logical_key (Phase 0 lookup)

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing test**

```python
def test_get_locked_unlocks_aged_success(fake_admin):
    """Success row > 60s old with logical_key_locked=true → unlock + return None."""
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "success", "logical_key_locked": True,
        "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": "evt_x", "result": {},
    }
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "logical_key_locked": False,
    }]
    with freeze_time("2026-05-03T10:02:00Z"):
        row = queries.get_locked_by_logical_key("lk1")
    assert row is None  # unlocked → caller can INSERT


def test_get_locked_returns_active_pending(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "pending", "logical_key_locked": True,
        "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": None, "result": {},
    }
    with freeze_time("2026-05-03T10:00:30Z"):
        row = queries.get_locked_by_logical_key("lk1")
    assert row["status"] == "pending"
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
_SUCCESS_LOCK_TTL = timedelta(seconds=60)


def get_locked_by_logical_key(logical_key: str) -> dict[str, Any] | None:
    """§5.2 Phase 0 lookup. Runs both stuck-pending GC AND aged-success
    unlock before returning. Returns the active locking row, or None
    if the slot is free.
    """
    row = (
        sb_admin().table("bot_actions").select("*")
        .eq("logical_key", logical_key)
        .eq("logical_key_locked", True)
        .maybe_single().execute().data
    )
    if not row:
        return None

    # Case (a): stuck pending → promote (may release lock).
    row = _lazy_gc_stuck_pending(row)
    if not row.get("logical_key_locked"):
        return None

    # Case (b): aged success → unlock.
    if row.get("status") == "success":
        created = datetime.fromisoformat(
            row["created_at"].replace("Z", "+00:00")
        )
        if datetime.now(timezone.utc) - created > _SUCCESS_LOCK_TTL:
            update = (
                sb_admin().table("bot_actions").update({"logical_key_locked": False})
                .eq("id", row["id"]).eq("logical_key_locked", True).execute()
            )
            if update.data:
                return None
            # 0 rows — another worker won the race and already unlocked.
            # Re-read by id to see the post-race state. If it's now
            # unlocked, the slot is genuinely free; return None so the
            # caller proceeds with their INSERT. Otherwise fall through.
            # (Codex plan-review iter-3 #4: returning the stale `row` here
            # would falsely report dedup hit on a >60s legitimate retry.)
            current = (
                sb_admin().table("bot_actions").select("*")
                .eq("id", row["id"]).maybe_single().execute().data
            )
            if not current or not current.get("logical_key_locked"):
                return None
            row = current  # use the re-read state for the final return

    return row
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): get_locked_by_logical_key Phase 0 lookup with lazy GC"
```

### Task 3.6: mark_bot_action_success / failed / undone helpers

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing tests for the three transition helpers**

```python
def test_mark_success_writes_target_and_result(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "success", "target_id": "evt_x",
    }]
    row = queries.mark_bot_action_success(
        "u1", target_id="evt_x", target_kind="calendar_event",
        result_patch={"link": "https://...", "attendees": ["ou_a"]},
    )
    assert row["status"] == "success"


def test_mark_failed_clears_lock(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "failed", "logical_key_locked": False,
    }]
    row = queries.mark_bot_action_failed("u1", error="api error")
    assert row["logical_key_locked"] is False


def test_mark_undone_clears_lock(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "undone", "logical_key_locked": False,
    }]
    row = queries.mark_bot_action_undone("u1")
    assert row["logical_key_locked"] is False


def test_mark_success_returns_none_when_terminal(fake_admin):
    """0-row UPDATE result → None; caller must re-read."""
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    row = queries.mark_bot_action_success("u1", target_id="evt_x", target_kind="calendar_event")
    assert row is None


def test_status_transition_updates_timestamp(fake_admin):
    captured = {}
    def fake_update(payload):
        captured.update(payload)
        return fake_admin.table.return_value.update.return_value
    fake_admin.table.return_value.update.side_effect = fake_update
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "failed",
    }]
    queries.mark_bot_action_failed("u1", error="api error")
    assert "updated_at" in captured
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
def mark_bot_action_success(
    action_id: str, *,
    target_id: str | None = None,
    target_kind: str | None = None,
    result_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Phase 3 success terminal. Guarded with status='pending' so a
    delayed retry can't overwrite a terminal row. Returns the updated
    row, or None if the row is already terminal (caller must re-read).
    """
    payload: dict[str, Any] = {
        "status": "success",
        "updated_at": _utc_now_iso(),
    }
    if target_id is not None:
        payload["target_id"] = target_id
    if target_kind is not None:
        payload["target_kind"] = target_kind
    if result_patch:
        # PostgREST `||` top-level merge — emulate via Python read-then-write
        # since supabase-py doesn't expose raw SQL fragments easily.
        existing = (
            sb_admin().table("bot_actions").select("result")
            .eq("id", action_id).maybe_single().execute().data
        )
        merged = {**(existing.get("result") or {} if existing else {}), **result_patch}
        payload["result"] = merged
    res = (
        sb_admin().table("bot_actions").update(payload)
        .eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None


def mark_bot_action_failed(action_id: str, error: str) -> dict[str, Any] | None:
    res = (
        sb_admin().table("bot_actions").update({
            "status": "failed", "error": error, "logical_key_locked": False,
            "updated_at": _utc_now_iso(),
        }).eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None


def mark_bot_action_undone(action_id: str) -> dict[str, Any] | None:
    """Phase-1 transition guard variant: pending → undone only.
    Used by tools that want to retire their OWN pending row (e.g., a
    cancel_meeting Phase 3 marking the cancel row's twin schedule_meeting
    row when both are still pending).

    For the §3.9 undo dispatch path that must transition a TERMINAL
    success/reconciled_unknown source row to undone, use
    `retire_source_action` below — it intentionally allows
    success/reconciled_unknown → undone without the pending guard.
    """
    res = (
        sb_admin().table("bot_actions").update({
            "status": "undone", "logical_key_locked": False,
            "updated_at": _utc_now_iso(),
        }).eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None


def retire_source_action(action_id: str) -> dict[str, Any] | None:
    """§3.9 undo dispatch helper: transition success/reconciled_unknown
    source row to undone AND clear logical_key_locked.

    Distinct from `mark_bot_action_undone` because:
    - mark_bot_action_undone has WHERE status='pending' (transition
      guard against overwriting terminals); the undo path needs the
      OPPOSITE — explicitly transition a terminal to undone.
    - The predicate `status IN ('success','reconciled_unknown')` makes
      this idempotent: a second undo attempt on an already-undone row
      affects 0 rows; caller treats that as success (the desired end
      state already holds).

    Used by every §3.9 dispatch arm that retires its source row:
    schedule_meeting / restore_schedule_meeting (after delete_event),
    cancel_meeting (after probe-then-restore-or-just-mark),
    append_action_items (after batch_delete), create_meeting_doc (after
    file.delete).

    The §3.9 cancel-restore R0 pre-step uses a more specific UPDATE
    with `action_type IN (...)` AND `target_kind='calendar_event'`
    guards (spec row 110); that lives directly in the tool body, not
    here, because it has additional invariants.
    """
    res = (
        sb_admin().table("bot_actions").update({
            "status": "undone", "logical_key_locked": False,
            "updated_at": _utc_now_iso(),
        }).eq("id", action_id).in_("status", ["success", "reconciled_unknown"]).execute()
    )
    if res.data:
        return res.data[0]
    # 0 rows: either already undone (idempotent success) or row doesn't
    # exist. Re-read to give caller current state.
    current = (
        sb_admin().table("bot_actions").select("*")
        .eq("id", action_id).maybe_single().execute().data
    )
    return current
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Add a test for retire_source_action**

```python
def test_retire_source_action_transitions_success_to_undone(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.in_.return_value.execute.return_value.data = [{
        "id": "u1", "status": "undone", "logical_key_locked": False,
    }]
    row = queries.retire_source_action("u1")
    assert row["status"] == "undone"
    assert row["logical_key_locked"] is False


def test_retire_source_action_idempotent_on_already_undone(fake_admin):
    """0 rows updated → re-read; should still return current state."""
    fake_admin.table.return_value.update.return_value.eq.return_value.in_.return_value.execute.return_value.data = []
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "status": "undone", "logical_key_locked": False,
    }
    row = queries.retire_source_action("u1")
    assert row["status"] == "undone"
```

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(db): mark_bot_action_success/failed/undone with transition guards + retire_source_action for undo dispatch"
```

### Task 3.6a: record_bot_action_target_pending

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

This helper is required by every write tool sub-step that creates or
discovers a Feishu artifact before Phase 3. Do not inline ad-hoc UPDATEs
inside tool bodies; otherwise crash recovery and undo metadata will drift
between schedule/cancel/doc/action-item flows.

- [ ] **Step 1: Write failing tests**

```python
def test_record_bot_action_target_pending_persists_artifact_without_terminalizing(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "result": {"calendar_id": "cal_bot"},
    }
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "pending", "target_id": "evt_x",
        "target_kind": "calendar_event",
        "result": {"calendar_id": "cal_bot", "link": "https://..."},
    }]
    row = queries.record_bot_action_target_pending(
        "u1", target_id="evt_x", target_kind="calendar_event",
        result_patch={"link": "https://..."},
    )
    assert row["status"] == "pending"
    assert row["target_id"] == "evt_x"
    assert row["result"]["calendar_id"] == "cal_bot"


def test_record_bot_action_target_pending_requires_pending(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "result": {},
    }
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    row = queries.record_bot_action_target_pending("u1", result_patch={"import_ticket": "t1"})
    assert row is None
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
def record_bot_action_target_pending(
    action_id: str, *,
    target_id: str | None = None,
    target_kind: str | None = None,
    result_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Persist artifact handles during Phase 2.x while keeping the row pending.

    Used immediately after artifact-producing calls:
    - schedule Phase 2.2.5: calendar event_id
    - cancel Phase 2a.5: pre-cancel snapshot + original event_id
    - append_action_items Phase 2.1.5: created Bitable row ids
    - create_meeting_doc Phase 2.1.5/2.2.5/2.3.5: import ticket,
      source_file_token, docx file_token

    Returns the updated row, or None if the source row is no longer
    pending and the caller must re-read/dispatch by current state.
    """
    if target_id is None and target_kind is None and not result_patch:
        raise ValueError("record_bot_action_target_pending requires target or result_patch")

    existing = (
        sb_admin().table("bot_actions").select("result")
        .eq("id", action_id).maybe_single().execute().data
    ) or {}
    payload: dict[str, Any] = {"updated_at": _utc_now_iso()}
    if target_id is not None:
        payload["target_id"] = target_id
    if target_kind is not None:
        payload["target_kind"] = target_kind
    if result_patch:
        payload["result"] = {**(existing.get("result") or {}), **result_patch}

    res = (
        sb_admin().table("bot_actions").update(payload)
        .eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): record pending artifact targets for write-tool recovery"
```

### Task 3.7: mark_bot_action_reconciled_unknown + invariant

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing tests including invariant**

```python
def test_mark_reconciled_partial_keeps_lock(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "reconciled_unknown",
        "logical_key_locked": True,
        "result": {"reconciliation_kind": "partial_success"},
        "target_id": "evt_x",
    }]
    row = queries.mark_bot_action_reconciled_unknown(
        "u1", kind="partial_success",
        error="attendee_invite_failed",
        target_id="evt_x", target_kind="calendar_event",
    )
    assert row["logical_key_locked"] is True


def test_mark_reconciled_stuck_clears_lock(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "logical_key_locked": False,
        "result": {"reconciliation_kind": "stuck_pending"},
    }]
    row = queries.mark_bot_action_reconciled_unknown(
        "u1", kind="stuck_pending", error="reconciled: pending too long",
    )
    assert row["logical_key_locked"] is False


def test_mark_reconciled_partial_without_handle_raises(fake_admin):
    """Invariant: partial_success requires target_id OR import_ticket OR source_file_token."""
    fake_admin.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u1", "target_id": None, "result": {},
    }
    with pytest.raises(ValueError, match="partial_success requires"):
        queries.mark_bot_action_reconciled_unknown(
            "u1", kind="partial_success", error="x",
        )
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
def mark_bot_action_reconciled_unknown(
    action_id: str, *, kind: str, error: str,
    target_id: str | None = None,
    target_kind: str | None = None,
    result_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Spec §5.3 + §6.2 Lock-behavior table.

    INVARIANT: kind='partial_success' requires the post-update row to
    have at least one of (target_id, result.import_ticket,
    result.source_file_token). Otherwise the row would be permanently
    locked and unreachable from undo (the iter-13 deadlock).
    """
    if kind not in ("stuck_pending", "partial_success"):
        raise ValueError(f"unknown reconciliation_kind: {kind}")

    # Fetch existing row to compute post-update shape for invariant check
    existing = (
        sb_admin().table("bot_actions").select("*")
        .eq("id", action_id).maybe_single().execute().data
    ) or {}
    post_target_id = target_id if target_id is not None else existing.get("target_id")
    post_result = {**(existing.get("result") or {}), **(result_patch or {}),
                   "reconciliation_kind": kind}

    if kind == "partial_success":
        has_handle = (
            post_target_id is not None
            or "import_ticket" in post_result
            or "source_file_token" in post_result
        )
        if not has_handle:
            raise ValueError(
                "partial_success requires target_id, result.import_ticket, "
                "or result.source_file_token; would create a locked-but-"
                "unreachable row"
            )

    payload: dict[str, Any] = {
        "status": "reconciled_unknown",
        "error": error,
        "result": post_result,
        "logical_key_locked": (kind == "partial_success"),
        "updated_at": _utc_now_iso(),
    }
    if target_id is not None:
        payload["target_id"] = target_id
    if target_kind is not None:
        payload["target_kind"] = target_kind

    res = (
        sb_admin().table("bot_actions").update(payload)
        .eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): mark_bot_action_reconciled_unknown with partial_success invariant"
```

### Task 3.8: update_for_retry

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing test**

```python
def test_update_for_retry_transitions_failed_to_pending(fake_admin):
    captured = {}
    def fake_update(payload):
        captured.update(payload)
        return fake_admin.table.return_value.update.return_value
    fake_admin.table.return_value.update.side_effect = fake_update
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "pending", "attempt_count": 2,
        "logical_key_locked": True,
    }]
    row = queries.update_for_retry("u1", new_args={"x": 1}, logical_key="lk1")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 2
    assert "updated_at" in captured


def test_update_for_retry_returns_none_when_not_failed(fake_admin):
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []
    row = queries.update_for_retry("u1", new_args={"x": 1}, logical_key="lk1")
    assert row is None


def test_update_for_retry_raises_logical_conflict_on_unique_violation(fake_admin):
    """Re-claiming the lock can hit the partial UNIQUE if a different message has won the slot."""
    from postgrest.exceptions import APIError
    err = APIError({"code": "23505",
                    "message": 'duplicate key value violates unique constraint "bot_actions_logical_locked_uniq"'})
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.side_effect = err
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {
        "id": "u-winner", "status": "success",
    }
    with pytest.raises(queries.LogicalKeyConflict):
        queries.update_for_retry("u1", new_args={"x": 1}, logical_key="lk1")
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
def update_for_retry(
    action_id: str, *, new_args: dict[str, Any], logical_key: str,
) -> dict[str, Any] | None:
    """Transition failed → pending and re-claim logical_key_locked.
    Returns the updated row, or None if the row isn't in `failed`
    state (caller treats that as "concurrent retry won the race").
    Can raise LogicalKeyConflict if a different message has acquired
    the slot since this row failed — caller dispatches identically
    to Phase 1b's LogicalKeyConflict handler.

    NOTE: supabase-py PostgREST UPDATE doesn't expose a SET expression
    that increments a column atomically, so we read-modify-write
    attempt_count. The status='failed' guard makes that safe.
    """
    from postgrest.exceptions import APIError

    # Read attempt_count first
    existing = (
        sb_admin().table("bot_actions").select("attempt_count")
        .eq("id", action_id).maybe_single().execute().data
    )
    attempt = ((existing or {}).get("attempt_count") or 1) + 1

    try:
        res = (
            sb_admin().table("bot_actions").update({
                "status": "pending",
                "attempt_count": attempt,
                "args": new_args,
                "error": None,
                "logical_key_locked": True,
                "updated_at": _utc_now_iso(),
            }).eq("id", action_id).eq("status", "failed").execute()
        )
        return res.data[0] if res.data else None
    except APIError as e:
        msg = str(e.message if hasattr(e, "message") else e)
        if _extract_constraint_name(msg) == "bot_actions_logical_locked_uniq":
            existing_winner = (
                sb_admin().table("bot_actions").select("*")
                .eq("logical_key", logical_key)
                .eq("logical_key_locked", True)
                .maybe_single().execute().data
            )
            raise LogicalKeyConflict(existing_row=existing_winner, raw_error=e)
        raise
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): update_for_retry with attempt_count + logical-key reclaim"
```

### Task 3.9: last_bot_action_for_sender_in_chat with newest-row guard

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing tests for the three sentinels**

```python
class LastIsInFlight: pass
class LastWasUnreachable: pass


def test_last_returns_undoable_row_when_newest_is_success(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [{
        "id": "u-newest", "status": "success", "target_id": "evt_x",
        "action_type": "schedule_meeting",
        "created_at": "2026-05-03T10:00:00Z",
    }]
    row = queries.last_bot_action_for_sender_in_chat(
        chat_id="c1", sender_open_id="s1",
    )
    assert row["id"] == "u-newest"


def test_last_returns_in_flight_when_newest_is_live_pending(fake_admin):
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [{
        "id": "u-newest", "status": "pending",
        "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": None, "result": {},
        "action_type": "schedule_meeting",
    }]
    with freeze_time("2026-05-03T10:00:30Z"):  # under 5min
        row = queries.last_bot_action_for_sender_in_chat(
            chat_id="c1", sender_open_id="s1",
        )
    assert row == queries.LastIsInFlight


def test_last_returns_unreachable_when_newest_is_stuck_pending(fake_admin):
    """Newest row promotes to stuck_pending after GC → LastWasUnreachable."""
    fake_admin.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [{
        "id": "u-newest", "status": "pending",
        "created_at": "2026-05-03T10:00:00Z",
        "updated_at": "2026-05-03T10:00:00Z",
        "target_id": None, "result": {},
        "action_type": "append_action_items",
    }]
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u-newest", "status": "reconciled_unknown",
        "result": {"reconciliation_kind": "stuck_pending"},
        "logical_key_locked": False, "target_id": None,
    }]
    with freeze_time("2026-05-03T10:06:00Z"):
        row = queries.last_bot_action_for_sender_in_chat(
            chat_id="c1", sender_open_id="s1",
        )
    assert row == queries.LastWasUnreachable
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
class _Sentinel:
    pass


LastIsInFlight = _Sentinel()
LastWasUnreachable = _Sentinel()


def _is_undoable(row: dict[str, Any]) -> bool:
    """Spec §3.9 undoable predicate."""
    if row.get("status") not in ("success", "reconciled_unknown"):
        return False
    if row.get("action_type") == "undo_last_action":
        return False
    if row.get("target_id") is not None:
        return True
    if row.get("status") == "reconciled_unknown":
        result = row.get("result") or {}
        return "import_ticket" in result or "source_file_token" in result
    return False


def last_bot_action_for_sender_in_chat(
    *, chat_id: str, sender_open_id: str,
    action_type_in: tuple[str, ...] | None = None,
) -> dict[str, Any] | _Sentinel | None:
    """Spec §3.9 / §11 helper. Returns the asker's chronologically-
    newest row, or one of two sentinels:

    - LastIsInFlight: newest is pending and < 5min (still running).
    - LastWasUnreachable: newest is non-undoable terminal after GC.

    The caller surfaces both sentinels as user-facing messages
    rather than falling through to an older row (spec iter-15 #3 +
    iter-16 #2).
    """
    q = (
        sb_admin().table("bot_actions").select("*")
        .eq("chat_id", chat_id).eq("sender_open_id", sender_open_id)
    )
    if action_type_in:
        q = q.in_("action_type", list(action_type_in))
    rows = q.order("created_at", desc=True).limit(5).execute().data or []
    if not rows:
        return None

    newest = rows[0]

    # Live pending check (no GC yet)
    if newest.get("status") == "pending":
        age_source = newest.get("updated_at") or newest.get("created_at")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            age_source.replace("Z", "+00:00")
        )
        if age < _STUCK_PENDING_THRESHOLD:
            return LastIsInFlight
        # Aged → run GC
        newest = _lazy_gc_stuck_pending(newest)

    if _is_undoable(newest):
        return newest
    return LastWasUnreachable
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): last_bot_action_for_sender_in_chat with sentinel returns"
```

### Task 3.10: bootstrap lock helpers

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_queries_bot_actions.py`

- [ ] **Step 1: Write failing test**

```python
def test_acquire_bootstrap_lock_first_caller_succeeds(fake_admin):
    fake_admin.table.return_value.insert.return_value.execute.return_value.data = [{"id": "lock-1"}]
    res = queries.acquire_bootstrap_lock()
    assert res["id"] == "lock-1"


def test_acquire_bootstrap_lock_loser_returns_none(fake_admin):
    from postgrest.exceptions import APIError
    fake_admin.table.return_value.insert.return_value.execute.side_effect = APIError({
        "code": "23505",
        "message": 'duplicate key value violates unique constraint "bot_actions_message_action_uniq"',
    })
    res = queries.acquire_bootstrap_lock()
    assert res is None


def test_release_bootstrap_lock_deletes_row(fake_admin):
    queries.release_bootstrap_lock("lock-1")
    fake_admin.table.return_value.delete.return_value.eq.assert_called_with("id", "lock-1")
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
_BOOTSTRAP_LOCK_MESSAGE_ID = "__bootstrap_lock__"
_BOOTSTRAP_LOCK_ACTION_TYPE = "bootstrap_workspace_lock"


def acquire_bootstrap_lock() -> dict[str, Any] | None:
    """§4 self-healing lock. Returns the inserted lock row on success,
    None if another caller already holds it (caller polls).
    """
    from postgrest.exceptions import APIError
    try:
        res = sb_admin().table("bot_actions").insert({
            "message_id": _BOOTSTRAP_LOCK_MESSAGE_ID,
            "chat_id": "__system__",
            "sender_open_id": "__system__",
            "logical_key": _BOOTSTRAP_LOCK_MESSAGE_ID,
            "action_type": _BOOTSTRAP_LOCK_ACTION_TYPE,
            "status": "pending",
            "logical_key_locked": False,
            "args": {},
        }).execute()
        return res.data[0]
    except APIError as e:
        msg = str(e.message if hasattr(e, "message") else e)
        if _extract_constraint_name(msg) == "bot_actions_message_action_uniq":
            return None
        raise


def release_bootstrap_lock(lock_id: str) -> None:
    """Release the bootstrap lock by DELETE (NOT UPDATE).
    Audit trail is in a separate `bootstrap_workspace` action_type row
    written by the caller.
    """
    sb_admin().table("bot_actions").delete().eq("id", lock_id).execute()
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): acquire/release_bootstrap_lock with DELETE-on-release"
```

---

# Task Group 4: Feishu auth + SDK wrappers (10 tasks — v21 expanded)

> **Spec ref:** §3.1, §3.3, §3.4, §3.5, §3.6, §3.8, §4. Spec §3.3bis API endpoint vs SDK attribute path table is the source of truth for resource names.

### Task 4.1: Extract tenant_access_token issuer

**Files:**
- Create: `bot/feishu/auth.py`
- Modify: `bot/feishu/client.py` (only the `fetch_self_info` body — call the new helper)

- [ ] **Step 1: Read existing implementation in `feishu/client.py:67`**

This is the inline POST currently in `fetch_self_info`. We're extracting it without behavior change.

- [ ] **Step 2: Create `bot/feishu/auth.py`**

```python
"""Tenant access token issuer for raw HTTP calls to Feishu endpoints
the lark-oapi SDK doesn't cover (currently: contact search).

NOT a long-lived cache — re-issued per call. Feishu's tokens are
typically valid for 2 hours but we don't optimize that today.
"""
from __future__ import annotations

import httpx

from config import settings


async def get_tenant_access_token() -> str | None:
    """Return a fresh tenant_access_token, or None on failure."""
    async with httpx.AsyncClient(timeout=10.0) as ac:
        try:
            r = await ac.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": settings.feishu_app_id,
                    "app_secret": settings.feishu_app_secret,
                },
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                return None
            return data.get("tenant_access_token")
        except httpx.HTTPError:
            return None
```

- [ ] **Step 3: Refactor `fetch_self_info` to use it**

Edit `bot/feishu/client.py:67` — replace the inline POST with:
```python
from feishu.auth import get_tenant_access_token

# inside fetch_self_info:
tat = await get_tenant_access_token()
if not tat:
    logger.warning("could not obtain tenant_access_token")
    return None
# ... rest unchanged
```

- [ ] **Step 4: Manual smoke test (optional)**

If env has Feishu creds:
```bash
cd bot && python -c "import asyncio; from feishu.auth import get_tenant_access_token; print(asyncio.run(get_tenant_access_token()))"
```

- [ ] **Step 5: Commit**

```bash
git add bot/feishu/auth.py bot/feishu/client.py
git commit -m "refactor(feishu): extract tenant_access_token issuer to feishu/auth.py"
```

### Task 4.2: feishu/contact.py — search-by-name via raw httpx

**Files:**
- Create: `bot/feishu/contact.py`
- Test: `bot/tests/test_feishu_contact.py`

- [ ] **Step 1: Write failing test**

```python
"""Test the raw-httpx /open-apis/search/v1/user wrapper."""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from feishu import contact


@pytest.mark.asyncio
async def test_search_users_returns_resolved(monkeypatch):
    monkeypatch.setattr("feishu.contact.get_tenant_access_token", lambda: "tok123")
    with respx.mock:
        respx.get("https://open.feishu.cn/open-apis/search/v1/user").mock(
            return_value=Response(200, json={
                "code": 0,
                "data": {
                    "users": [
                        {"open_id": "ou_albert", "name": "Albert Wang", "email": "albert@x.com"},
                    ],
                },
            })
        )
        result = await contact.search_users(query="albert", page_size=20)
    assert result == [{"open_id": "ou_albert", "name": "Albert Wang", "email": "albert@x.com"}]


@pytest.mark.asyncio
async def test_search_users_returns_empty_on_4xx(monkeypatch):
    monkeypatch.setattr("feishu.contact.get_tenant_access_token", lambda: "tok123")
    with respx.mock:
        respx.get("https://open.feishu.cn/open-apis/search/v1/user").mock(
            return_value=Response(403, json={"code": 99991663})
        )
        result = await contact.search_users(query="albert", page_size=20)
    assert result == []
```

Note: `get_tenant_access_token` is async; tests use `monkeypatch` to return a sync string. We make `get_tenant_access_token` return a string (already does) and the production code awaits it; in tests we replace the function with a sync lambda that returns a string. Actually we need to make it a coroutine substitute — fix below uses `AsyncMock` or wraps:

Refine the monkeypatch:
```python
async def _fake_token():
    return "tok123"

monkeypatch.setattr("feishu.contact.get_tenant_access_token", _fake_token)
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

```python
"""contact.v3.user is in lark-oapi but contact.v3.user.search is NOT.
Per spec §3.1 step 3, name-based directory search uses raw httpx
against /open-apis/search/v1/user (the same endpoint lark-cli's
+search-user uses).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from feishu.auth import get_tenant_access_token

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://open.feishu.cn/open-apis/search/v1/user"


async def search_users(*, query: str, page_size: int = 20) -> list[dict[str, Any]]:
    """Return the list of {open_id, name, en_name, email, ...} candidates,
    or an empty list on any non-2xx (treated as 'unresolved').

    Spec §3.1 step 3 + error-handling section.
    """
    tat = await get_tenant_access_token()
    if not tat:
        logger.warning("contact.search_users: no tenant_access_token")
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as ac:
            r = await ac.get(
                _SEARCH_URL,
                headers={"Authorization": f"Bearer {tat}"},
                params={"query": query, "page_size": page_size},
            )
            if r.status_code == 401:
                # one retry with a fresh token
                tat2 = await get_tenant_access_token()
                if not tat2:
                    return []
                r = await ac.get(
                    _SEARCH_URL,
                    headers={"Authorization": f"Bearer {tat2}"},
                    params={"query": query, "page_size": page_size},
                )
            if r.status_code >= 400:
                logger.warning("contact.search_users %s: %s", r.status_code, r.text[:200])
                return []
            data = r.json()
            if data.get("code") != 0:
                return []
            return data.get("data", {}).get("users", [])
    except httpx.HTTPError as e:
        logger.warning("contact.search_users HTTP error: %s", e)
        return []
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git add bot/feishu/contact.py bot/tests/test_feishu_contact.py
git commit -m "feat(feishu): search_users via raw httpx (lark-oapi has no contact.user.search)"
```

### Task 4.3: feishu/contact.py — get_user (timezone) + batch_get_id

**Files:**
- Modify: `bot/feishu/contact.py`
- Test: `bot/tests/test_feishu_contact.py`

- [ ] **Step 1: Write failing test for get_user_with_user_id_type**

```python
@pytest.mark.asyncio
async def test_get_user_returns_timezone(monkeypatch):
    """contact.v3.user.get must pass user_id_type='open_id'."""
    captured = {}

    class FakeUserResource:
        def get(self, request):
            captured["request"] = request
            class FakeResp:
                code = 0
                msg = "ok"
                data = type("D", (), {"user": type("U", (), {
                    "open_id": "ou_x",
                    "time_zone": "Asia/Shanghai",
                })})

                def success(self): return True
            return FakeResp()

    fake_client = type("C", (), {})()
    fake_client.contact = type("X", (), {})()
    fake_client.contact.v3 = type("X", (), {})()
    fake_client.contact.v3.user = FakeUserResource()
    monkeypatch.setattr("feishu.client.feishu_client.client", fake_client)
    monkeypatch.setattr("feishu.contact._lark_client", lambda: fake_client)

    info = await contact.get_user(open_id="ou_x")
    assert info["time_zone"] == "Asia/Shanghai"
    # The request must have user_id_type set
    assert captured["request"].user_id_type == "open_id"
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement**

Append to `bot/feishu/contact.py`:
```python
import asyncio

import lark_oapi as lark
from lark_oapi.api.contact.v3.model import (
    GetUserRequest,
    BatchGetIdUserRequest,
    BatchGetIdUserRequestBody,
)


def _lark_client() -> lark.Client:
    """Lazy import to avoid circular import with feishu/client.py."""
    from feishu.client import feishu_client
    return feishu_client.client


async def get_user(*, open_id: str) -> dict[str, Any] | None:
    """contact.v3.user.get — used by today_iso for timezone (§3.2)."""
    req = (
        GetUserRequest.builder()
        .user_id(open_id)
        .user_id_type("open_id")
        .build()
    )
    # lark-oapi is sync; offload to a thread
    resp = await asyncio.to_thread(_lark_client().contact.v3.user.get, req)
    if not resp.success():
        return None
    user = getattr(resp.data, "user", None)
    if not user:
        return None
    # The model is a builder-style object; convert relevant fields
    return {
        "open_id": getattr(user, "open_id", None),
        "name": getattr(user, "name", None),
        "time_zone": getattr(user, "time_zone", None),
        "email": getattr(user, "email", None),
    }


async def batch_get_id_by_email_or_phone(
    *, emails: list[str] | None = None, mobiles: list[str] | None = None,
) -> dict[str, str]:
    """Map email/phone → open_id via contact.v3.user.batch_get_id.
    Returns dict keyed by the input email/phone; missing entries excluded.
    Spec §3.1 step 2.
    """
    if not emails and not mobiles:
        return {}
    body = BatchGetIdUserRequestBody.builder()
    if emails:
        body = body.emails(emails)
    if mobiles:
        body = body.mobiles(mobiles)
    req = (
        BatchGetIdUserRequest.builder()
        .request_body(body.build())
        .user_id_type("open_id")
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().contact.v3.user.batch_get_id, req)
    if not resp.success():
        return {}
    out: dict[str, str] = {}
    for entry in (resp.data.user_list or []):
        if entry.email and entry.user_id:
            out[entry.email] = entry.user_id
        if entry.mobile and entry.user_id:
            out[entry.mobile] = entry.user_id
    return out
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(feishu): contact.get_user (timezone) + batch_get_id_by_email_or_phone"
```

### Task 4.4: feishu/calendar.py — minimal wrappers

**Files:**
- Create: `bot/feishu/calendar.py`
- Test: `bot/tests/test_feishu_calendar.py`

This wraps:
- `calendar.v4.calendar.create` (bootstrap)
- `calendar.v4.calendar.primarys` (list_my_meetings)
- `calendar.v4.calendar_event.create / get / delete / list`
- `calendar.v4.calendar_event_attendee.create`
- `calendar.v4.freebusy.batch`

- [ ] **Step 1: Write failing tests for create_calendar + create_event with idempotency_key**

```python
"""Test calendar SDK wrappers — assert builder paths and required args."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from feishu import calendar


@pytest.mark.asyncio
async def test_create_calendar_uses_calendar_request_body(monkeypatch):
    captured = {}
    fake_resource = MagicMock()
    def fake_create(request):
        captured["request"] = request
        resp = MagicMock()
        resp.success.return_value = True
        resp.data.calendar.calendar_id = "cal_bot"
        return resp
    fake_resource.create = fake_create
    fake_lark = MagicMock()
    fake_lark.calendar.v4.calendar = fake_resource
    monkeypatch.setattr("feishu.calendar._lark_client", lambda: fake_lark)

    calendar_id = await calendar.create_calendar(summary="PMO Bot")
    assert calendar_id == "cal_bot"
    req = captured["request"]
    assert req.request_body.__class__.__name__ == "Calendar"
    assert req.request_body.summary == "PMO Bot"


@pytest.mark.asyncio
async def test_create_event_passes_idempotency_key_and_calendar_id(monkeypatch):
    captured = {}
    fake_resource = MagicMock()
    def fake_create(request):
        captured["request"] = request
        resp = MagicMock()
        resp.success.return_value = True
        resp.data.event.event_id = "evt_xyz"
        return resp
    fake_resource.create = fake_create
    fake_lark = MagicMock()
    fake_lark.calendar.v4.calendar_event = fake_resource
    monkeypatch.setattr("feishu.calendar._lark_client", lambda: fake_lark)

    event_id = await calendar.create_event(
        calendar_id="cal_bot",
        summary="X", description="d",
        start_time="2026-05-08T15:00:00+08:00",
        end_time="2026-05-08T15:30:00+08:00",
        attendee_ability="can_modify_event",
        reminders=[{"minutes": 15}],
        idempotency_key="schedule_meeting:uuid-1",
    )
    assert event_id == "evt_xyz"
    req = captured["request"]
    assert req.calendar_id == "cal_bot"
    assert req.idempotency_key == "schedule_meeting:uuid-1"
    assert req.request_body.start_time.timezone
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Implement (skeleton — full implementation has 6 functions)**

```python
"""Calendar v4 SDK wrappers. See spec §3.3 / §3.4 / §3.5 / §3.9 and the
§3.3bis API-endpoint-vs-SDK-path table for legal paths.

Every call is async via asyncio.to_thread because lark-oapi is sync.
"""
from __future__ import annotations

import asyncio
from typing import Any

import lark_oapi as lark
from lark_oapi.api.calendar.v4.model import (
    CreateCalendarRequest,
    CreateCalendarEventRequest,
    GetCalendarEventRequest,
    DeleteCalendarEventRequest,
    ListCalendarEventRequest,
    CreateCalendarEventAttendeeRequest,
    CreateCalendarEventAttendeeRequestBody,
    CalendarEventAttendee,
    PrimarysCalendarRequest,
    PrimarysCalendarRequestBody,
    BatchFreebusyRequest,
    BatchFreebusyRequestBody,
    Calendar,
    CalendarEvent,
)


def _lark_client() -> lark.Client:
    from feishu.client import feishu_client
    return feishu_client.client


async def create_calendar(*, summary: str) -> str:
    """Bootstrap: create the bot's primary calendar. Returns calendar_id."""
    body = Calendar.builder().summary(summary).build()
    req = CreateCalendarRequest.builder().request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar.create, req)
    if not resp.success():
        raise RuntimeError(f"create_calendar failed: {resp.code} {resp.msg}")
    return resp.data.calendar.calendar_id


async def primarys(*, user_open_ids: list[str]) -> list[dict[str, Any]]:
    """Resolve users' primary calendars. Spec §3.5."""
    body = PrimarysCalendarRequestBody.builder().user_ids(user_open_ids).build()
    req = (
        PrimarysCalendarRequest.builder()
        .user_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar.primarys, req)
    if not resp.success():
        return []
    out: list[dict[str, Any]] = []
    for cw in (resp.data.calendars or []):
        out.append({
            "user_id": cw.user_id,
            "calendar_id": cw.calendar.calendar_id if cw.calendar else None,
        })
    return out


async def freebusy_batch(
    *, user_open_ids: list[str], time_min: str, time_max: str,
) -> list[dict[str, Any]]:
    body = (
        BatchFreebusyRequestBody.builder()
        .user_ids(user_open_ids)
        .time_min(time_min).time_max(time_max)
        .include_external_calendar(False)
        .only_busy(True)
        .build()
    )
    req = (
        BatchFreebusyRequest.builder()
        .user_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.freebusy.batch, req)
    if not resp.success():
        raise RuntimeError(f"freebusy.batch failed: {resp.code} {resp.msg}")
    return [
        {"user_id": u.user_id, "busy_time": [
            {"start_time": b.start_time, "end_time": b.end_time}
            for b in (u.busy_time or [])
        ]}
        for u in (resp.data.freebusy_list or [])
    ]


async def create_event(
    *, calendar_id: str, summary: str, description: str = "",
    start_time: str, end_time: str,
    time_zone: str | None = None,
    attendee_ability: str = "can_modify_event",
    reminders: list[dict[str, Any]] | None = None,
    idempotency_key: str,
) -> str:
    """Spec §3.3 Phase 2.2. Returns the new event_id.

    `idempotency_key` is mandatory — keyed by `schedule_meeting:<bot_actions.id>`
    so a webhook retry doesn't create a duplicate event.
    """
    event = (
        CalendarEvent.builder()
        .summary(summary).description(description)
    )
    # The SDK exposes time_info via builder; check installed model for exact
    # field names (TimeInfo). Use start_time/end_time builders directly:
    event = event.start_time(_time_info(start_time, time_zone))
    event = event.end_time(_time_info(end_time, time_zone))
    event = event.attendee_ability(attendee_ability)
    if reminders:
        event = event.reminders(reminders)
    req = (
        CreateCalendarEventRequest.builder()
        .calendar_id(calendar_id)
        .idempotency_key(idempotency_key)
        .request_body(event.build())
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.create, req)
    if not resp.success():
        raise RuntimeError(f"calendar_event.create failed: {resp.code} {resp.msg}")
    return resp.data.event.event_id


def _time_info(rfc3339: str, time_zone: str | None) -> Any:
    """Build a TimeInfo from an RFC3339 string. Implementer should
    use the installed SDK field name `timezone` (not `time_zone`).
    """
    from datetime import datetime
    from lark_oapi.api.calendar.v4.model import TimeInfo
    dt = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    return (
        TimeInfo.builder()
        .timestamp(str(int(dt.timestamp())))
        .timezone(time_zone or str(dt.tzinfo))
        .build()
    )


async def get_event(
    *, calendar_id: str, event_id: str, need_attendee: bool = True,
) -> dict[str, Any]:
    req = (
        GetCalendarEventRequest.builder()
        .calendar_id(calendar_id)
        .event_id(event_id)
        .need_attendee(need_attendee)
        .user_id_type("open_id")
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.get, req)
    if not resp.success():
        # Surface 404 as None via raise — caller checks
        if resp.code in (1010001, 195101):  # event not found codes (verify in §3.9 implementation)
            raise EventNotFound(event_id)
        raise RuntimeError(f"calendar_event.get failed: {resp.code} {resp.msg}")
    # Convert to dict for whitelist-safe restore in §3.9 R1
    e = resp.data.event
    return {
        "event_id": e.event_id,
        "summary": e.summary,
        "description": e.description,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "visibility": getattr(e, "visibility", None),
        "attendee_ability": getattr(e, "attendee_ability", None),
        "reminders": getattr(e, "reminders", None),
        "location": getattr(e, "location", None),
        "color": getattr(e, "color", None),
        "attendees": [
            {"open_id": a.user_id, "type": a.type}
            for a in (resp.data.attendees or [])
        ],
    }


class EventNotFound(Exception):
    pass


async def delete_event(*, calendar_id: str, event_id: str) -> None:
    req = (
        DeleteCalendarEventRequest.builder()
        .calendar_id(calendar_id).event_id(event_id).build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.delete, req)
    if not resp.success():
        if resp.code in (1010001, 195101):
            raise EventNotFound(event_id)
        raise RuntimeError(f"calendar_event.delete failed: {resp.code} {resp.msg}")


async def invite_attendees(
    *, calendar_id: str, event_id: str, open_ids: list[str],
    need_notification: bool = True,
) -> None:
    body = (
        CreateCalendarEventAttendeeRequestBody.builder()
        .attendees([
            CalendarEventAttendee.builder().type("user").user_id(oid).build()
            for oid in open_ids
        ])
        .need_notification(need_notification)
        .build()
    )
    req = (
        CreateCalendarEventAttendeeRequest.builder()
        .calendar_id(calendar_id).event_id(event_id)
        .user_id_type("open_id")
        .request_body(body).build()
    )
    resp = await asyncio.to_thread(
        _lark_client().calendar.v4.calendar_event_attendee.create, req
    )
    if not resp.success():
        raise RuntimeError(f"attendee.create failed: {resp.code} {resp.msg}")
```

> **Implementer note:** the `_time_info` and 404 error code values (1010001 / 195101) need verification against the installed SDK at implementation time. Run `python -c "import lark_oapi.api.calendar.v4.model.time_info as ti; print(dir(ti))"` to inspect.

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git add bot/feishu/calendar.py bot/tests/test_feishu_calendar.py
git commit -m "feat(feishu): calendar v4 SDK wrappers (event create/get/delete + attendee + freebusy + primarys)"
```

### Task 4.5: feishu/bitable.py wrappers

**Files:**
- Create: `bot/feishu/bitable.py`
- Test: `bot/tests/test_feishu_bitable.py`

Wraps:
- `bitable.v1.app.create / get`
- `bitable.v1.app_table.create`
- `bitable.v1.app_table_record.batch_create / batch_delete / search / list`

- [ ] **Step 1-5: Same TDD pattern as Task 4.4**

Reference `lark_oapi/api/bitable/v1/version.py` for attribute names. Important: every call passes `user_id_type="open_id"` (Person field identity space, spec §3.6 / §3.7). `batch_create` passes `client_token=<bot_actions.id>` for Feishu-side idempotency (spec row 113).

Implementation skeleton in the file. Commit:
```bash
git commit -am "feat(feishu): bitable v1 SDK wrappers with user_id_type=open_id and client_token"
```

### Task 4.6: feishu/drive.py wrappers (Path A 3-step)

**Files:**
- Create: `bot/feishu/drive.py`
- Test: `bot/tests/test_feishu_drive.py`

Wraps:
- `drive.v1.file.upload_all`
- `drive.v1.file.create_folder`
- `drive.v1.file.delete`
- `drive.v1.import_task.create / get`

- [ ] **Step 1-5: TDD pattern as Task 4.4**

Implementation per spec §3.8 Path A (full builder syntax shown there). Each step async via `asyncio.to_thread`. The `delete` wrapper exposes `EventNotFound`-equivalent: `FileNotFound` for the §3.9 404-as-success rule.

Commit:
```bash
git commit -am "feat(feishu): drive v1 SDK wrappers (upload_all, import_task create/get, file delete)"
```

### Task 4.7: feishu/docx.py wrappers (RESURRECTED for spec v21)

> **iter-3 history:** This task was deleted in plan iter-3 because v1 only
> needed `create_meeting_doc` Path A (drive.v1.import_task). v21 reintroduces
> docx because **`read_doc` and `append_to_doc` need block-level access**
> (Path A only handles whole-doc creation). Path B append is now a first-class
> tool. Scope: read-any (block list) + write-own (block append/delete).

**Files:**
- Create: `bot/feishu/docx.py`
- Test: `bot/tests/test_feishu_docx.py`

- [ ] **Step 1: Write failing tests for `list_blocks`, `append_blocks`, and index-range `delete_blocks`**

```python
import pytest
from unittest.mock import MagicMock

from feishu import docx


@pytest.mark.asyncio
async def test_list_blocks_returns_paginated_list(monkeypatch):
    fake_resp_page1 = MagicMock(success=lambda: True)
    fake_resp_page1.data.items = [MagicMock(block_id="b1"), MagicMock(block_id="b2")]
    fake_resp_page1.data.has_more = True
    fake_resp_page1.data.page_token = "tok2"

    fake_resp_page2 = MagicMock(success=lambda: True)
    fake_resp_page2.data.items = [MagicMock(block_id="b3")]
    fake_resp_page2.data.has_more = False

    calls = []
    def fake_list(req):
        calls.append(req)
        return fake_resp_page1 if len(calls) == 1 else fake_resp_page2

    fake_client = MagicMock()
    fake_client.docx.v1.document_block.list = fake_list
    monkeypatch.setattr(docx, "_lark_client", lambda: fake_client)

    blocks = await docx.list_blocks("doc_token_123")
    assert [b.block_id for b in blocks] == ["b1", "b2", "b3"]


@pytest.mark.asyncio
async def test_delete_blocks_maps_block_ids_to_current_child_indexes(monkeypatch):
    fake_children = [
        MagicMock(block_id="keep_a"),
        MagicMock(block_id="del_1"),
        MagicMock(block_id="del_2"),
        MagicMock(block_id="keep_b"),
        MagicMock(block_id="del_3"),
    ]
    fake_get_resp = MagicMock(success=lambda: True)
    fake_get_resp.data.items = fake_children
    fake_get_resp.data.has_more = False

    delete_requests = []
    fake_delete_resp = MagicMock(success=lambda: True)

    fake_client = MagicMock()
    fake_client.docx.v1.document_block_children.get = lambda req: fake_get_resp
    fake_client.docx.v1.document_block_children.batch_delete = (
        lambda req: delete_requests.append(req) or fake_delete_resp
    )
    monkeypatch.setattr(docx, "_lark_client", lambda: fake_client)

    out = await docx.delete_blocks(
        "doc_token_123", "root", ["del_1", "del_2", "already_missing", "del_3"]
    )

    assert out == {"deleted": 3, "missing": 1}
    # Delete highest range first so lower indexes do not shift.
    assert [(r.body.start_index, r.body.end_index) for r in delete_requests] == [
        (4, 5),
        (1, 3),
    ]
```

- [ ] **Step 2: Run test → expect import error / NotImplemented**

```bash
cd bot && pytest tests/test_feishu_docx.py::test_list_blocks_returns_paginated_list -v
```

- [ ] **Step 3: Implement `bot/feishu/docx.py`**

```python
"""Docx v1 wrappers — block-level read/append/delete on documents."""
from __future__ import annotations

from typing import Any

import asyncio
import lark_oapi as lark
from lark_oapi.api.docx.v1 import (
    BatchDeleteDocumentBlockChildrenRequest,
    BatchDeleteDocumentBlockChildrenRequestBody,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    GetDocumentBlockChildrenRequest,
    ListDocumentBlockRequest,
)

from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def list_blocks(document_id: str) -> list[Any]:
    """Return all blocks of a document, walking pagination."""
    blocks: list[Any] = []
    page_token: str | None = None
    while True:
        req = (
            ListDocumentBlockRequest.builder()
            .document_id(document_id)
            .page_size(500)
        )
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(_lark_client().docx.v1.document_block.list, req.build())
        if not resp.success():
            raise RuntimeError(f"docx.list_blocks failed: {resp.code} {resp.msg}")
        blocks.extend(resp.data.items or [])
        if not resp.data.has_more:
            return blocks
        page_token = resp.data.page_token


async def list_child_blocks(document_id: str, parent_block_id: str) -> list[Any]:
    """Return direct children under parent_block_id, walking pagination."""
    children: list[Any] = []
    page_token: str | None = None
    while True:
        req = (
            GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(parent_block_id)
            .page_size(500)
        )
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(
            _lark_client().docx.v1.document_block_children.get, req.build()
        )
        if not resp.success():
            raise RuntimeError(
                f"docx.list_child_blocks failed: {resp.code} {resp.msg}"
            )
        children.extend(resp.data.items or [])
        if not resp.data.has_more:
            return children
        page_token = resp.data.page_token


async def append_blocks(
    document_id: str,
    parent_block_id: str,
    children: list[Any],
    *,
    index: int = -1,
    client_token: str | None = None,
) -> list[str]:
    """Append children blocks under parent_block_id; returns new block_ids."""
    body = (
        CreateDocumentBlockChildrenRequestBody.builder()
        .children(children)
        .index(index)
        .build()
    )
    req_builder = (
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(document_id)
        .block_id(parent_block_id)
        .request_body(body)
    )
    if client_token:
        req_builder = req_builder.client_token(client_token)
    resp = await asyncio.to_thread(
        _lark_client().docx.v1.document_block_children.create, req_builder.build()
    )
    if not resp.success():
        raise RuntimeError(f"docx.append_blocks failed: {resp.code} {resp.msg}")
    return [c.block_id for c in (resp.data.children or [])]


def _contiguous_ranges(indexes: list[int]) -> list[tuple[int, int]]:
    """Return inclusive (start, end) ranges for sorted child indexes."""
    if not indexes:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = indexes[0]
    for idx in indexes[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = prev = idx
    ranges.append((start, prev))
    return ranges


async def delete_blocks(
    document_id: str, parent_block_id: str, block_ids: list[str]
) -> dict[str, int]:
    """Delete specified child block IDs by mapping them to current indexes.

    lark-oapi's installed batch_delete body has only start_index/end_index,
    not block_ids. Treat missing block IDs as already deleted.
    """
    current_children = await list_child_blocks(document_id, parent_block_id)
    index_by_id = {c.block_id: i for i, c in enumerate(current_children)}
    indexes = sorted(index_by_id[b] for b in block_ids if b in index_by_id)
    missing = len(block_ids) - len(indexes)
    if not indexes:
        return {"deleted": 0, "missing": missing}

    for start, end in reversed(_contiguous_ranges(indexes)):
        body = (
            BatchDeleteDocumentBlockChildrenRequestBody.builder()
            .start_index(start)
            .end_index(end + 1)  # SDK/API range is start-inclusive, end-exclusive.
            .build()
        )
        req = (
            BatchDeleteDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(parent_block_id)
            .client_token(f"delete:{parent_block_id}:{start}:{end}")
            .request_body(body)
            .build()
        )
        resp = await asyncio.to_thread(
            _lark_client().docx.v1.document_block_children.batch_delete, req
        )
        if not resp.success():
            raise RuntimeError(
                f"docx.delete_blocks failed: code={resp.code} msg={resp.msg}"
            )
    return {"deleted": len(indexes), "missing": missing}
```

- [ ] **Step 4: Run tests → green**

- [ ] **Step 5: Commit**

```bash
git add bot/feishu/docx.py bot/tests/test_feishu_docx.py
git commit -m "feat(feishu): docx.v1 block list/append/delete wrappers"
```

### Task 4.8: feishu/wiki.py wrapper (NEW for spec v21)

> Only needed for `resolve_feishu_link` to redirect `/wiki/<token>` URLs
> to their underlying obj_token + obj_type. Single function.

**Files:**
- Create: `bot/feishu/wiki.py`
- Test: `bot/tests/test_feishu_wiki.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from unittest.mock import MagicMock

from feishu import wiki


@pytest.mark.asyncio
async def test_resolve_node_returns_obj_metadata(monkeypatch):
    fake_resp = MagicMock(success=lambda: True)
    fake_resp.data.node = MagicMock(obj_token="docx_xxx", obj_type="docx")
    fake_client = MagicMock()
    fake_client.wiki.v2.space.get_node = lambda req: fake_resp
    monkeypatch.setattr(wiki, "_lark_client", lambda: fake_client)

    out = await wiki.resolve_node("wikcnXXXXXX")
    assert out == {"obj_token": "docx_xxx", "obj_type": "docx"}
```

- [ ] **Step 2: Implement `bot/feishu/wiki.py`**

```python
"""Wiki v2 wrapper — resolve a /wiki/<token> URL to its underlying object."""
from __future__ import annotations

import asyncio
import lark_oapi as lark
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def resolve_node(token: str) -> dict[str, str]:
    """Given a wiki node token, return {'obj_token', 'obj_type'}.

    obj_type is one of 'docx', 'sheet', 'bitable', 'mindnote', 'file'.
    """
    req = GetNodeSpaceRequest.builder().token(token).build()
    resp = await asyncio.to_thread(_lark_client().wiki.v2.space.get_node, req)
    if not resp.success():
        raise RuntimeError(f"wiki.resolve_node failed: {resp.code} {resp.msg}")
    node = resp.data.node
    return {"obj_token": node.obj_token, "obj_type": node.obj_type}
```

- [ ] **Step 3: Run test → green; commit**

```bash
git add bot/feishu/wiki.py bot/tests/test_feishu_wiki.py
git commit -m "feat(feishu): wiki.v2 space resolver for /wiki/<token>"
```

### Task 4.9: feishu/links.py URL parser (NEW for spec v21)

> Pure function — no I/O except for the wiki redirect path. Powers
> `resolve_feishu_link` (Task 7.4).

**Files:**
- Create: `bot/feishu/links.py`
- Test: `bot/tests/test_feishu_links.py`

- [ ] **Step 1: Write failing tests covering each URL pattern**

```python
import pytest

from feishu import links


def test_parse_docx_url():
    out = links.parse_url("https://example.feishu.cn/docx/doxcnAAAA")
    assert out == {"kind": "docx", "token": "doxcnAAAA"}


def test_parse_sheet_url():
    out = links.parse_url("https://example.feishu.cn/sheets/shtcnBBBB?sheet=ABC")
    assert out == {"kind": "sheet", "token": "shtcnBBBB", "sheet_id": "ABC"}


def test_parse_base_url_with_table():
    out = links.parse_url(
        "https://example.feishu.cn/base/bascnCCCC?table=tblD&view=vewE"
    )
    assert out == {
        "kind": "bitable",
        "app_token": "bascnCCCC",
        "table_id": "tblD",
        "view_id": "vewE",
    }


def test_parse_wiki_url():
    out = links.parse_url("https://example.feishu.cn/wiki/wikcnFFFF")
    assert out == {"kind": "wiki", "token": "wikcnFFFF"}


def test_parse_unknown_url_raises():
    with pytest.raises(ValueError):
        links.parse_url("https://example.com/random")
```

- [ ] **Step 2: Implement `bot/feishu/links.py`**

```python
"""Pure URL parser for Feishu doc/wiki/sheet/base links.

resolve_feishu_link uses parse_url to figure out what kind of object the
user pasted, then optionally calls wiki.resolve_node for /wiki/ redirects.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_DOCX_RE = re.compile(r"^/docx/([A-Za-z0-9]+)$")
_DOC_LEGACY_RE = re.compile(r"^/doc/([A-Za-z0-9]+)$")
_SHEET_RE = re.compile(r"^/sheets/([A-Za-z0-9]+)$")
_BASE_RE = re.compile(r"^/base/([A-Za-z0-9]+)$")
_WIKI_RE = re.compile(r"^/wiki/([A-Za-z0-9]+)$")


def parse_url(url: str) -> dict[str, str]:
    """Parse a Feishu URL into {kind, token, ...}.

    Recognised kinds: 'docx', 'doc' (legacy), 'sheet', 'bitable', 'wiki'.
    Raises ValueError for unrecognised URLs.
    """
    parsed = urlparse(url.strip())
    path = parsed.path
    qs = parse_qs(parsed.query or "")

    if m := _DOCX_RE.match(path):
        return {"kind": "docx", "token": m.group(1)}
    if m := _DOC_LEGACY_RE.match(path):
        return {"kind": "doc", "token": m.group(1)}
    if m := _SHEET_RE.match(path):
        out = {"kind": "sheet", "token": m.group(1)}
        if sheet_id := qs.get("sheet", [None])[0]:
            out["sheet_id"] = sheet_id
        return out
    if m := _BASE_RE.match(path):
        out = {"kind": "bitable", "app_token": m.group(1)}
        if table_id := qs.get("table", [None])[0]:
            out["table_id"] = table_id
        if view_id := qs.get("view", [None])[0]:
            out["view_id"] = view_id
        return out
    if m := _WIKI_RE.match(path):
        return {"kind": "wiki", "token": m.group(1)}

    raise ValueError(f"unrecognized Feishu URL: {url}")
```

- [ ] **Step 3: Run tests → green; commit**

```bash
git add bot/feishu/links.py bot/tests/test_feishu_links.py
git commit -m "feat(feishu): URL parser for docx/wiki/base/sheet links"
```

### Task 4.10: Verify SDK call shapes against installed lark-oapi

**Files:** none (manual verification)

- [ ] **Step 1: For every wrapper call site, grep installed SDK to confirm method/param names**

```bash
python -c "
import lark_oapi
import importlib
import inspect

paths = [
    'lark_oapi.api.calendar.v4.model.create_calendar_event_request',
    'lark_oapi.api.calendar.v4.model.calendar_event',
    'lark_oapi.api.bitable.v1.model.batch_create_app_table_record_request',
    'lark_oapi.api.bitable.v1.model.create_app_table_request',
    'lark_oapi.api.drive.v1.model.upload_all_file_request',
    'lark_oapi.api.drive.v1.model.import_task',
    'lark_oapi.api.docx.v1.model.list_document_block_request',
    'lark_oapi.api.docx.v1.model.create_document_block_children_request',
    'lark_oapi.api.docx.v1.model.batch_delete_document_block_children_request',
    'lark_oapi.api.wiki.v2.model.get_node_space_request',
]
for p in paths:
    mod = importlib.import_module(p)
    print('---', p)
    for name, cls in inspect.getmembers(mod, inspect.isclass):
        if not name.startswith('_'):
            print(' ', name)
"
```

Compare output to the wrapper code in `feishu/calendar.py`, `bitable.py`, `drive.py`. If any field/method name mismatches the installed SDK, fix the wrapper before moving on.

- [ ] **Step 2: Run all feishu/ tests**

```bash
cd bot && pytest tests/test_feishu_*.py -v
```

Expected: all pass.

- [ ] **Step 3: No commit if all green**

---

# Task Group 5: Bootstrap script (1 task)

> **Spec ref:** §4.

### Task 5.1: bootstrap_bot_workspace.py

> **Depends on:** Task 4.4 (`feishu.calendar.create_calendar`), Task 4.5 (`feishu.bitable.bootstrap_base`), Task 4.6 (`feishu.drive.create_folder`), Task 3.2 (`upsert_bot_workspace`), Task 3.10 (`acquire/release_bootstrap_lock`).

**Files:**
- Create: `bot/scripts/__init__.py` (empty)
- Create: `bot/scripts/bootstrap_bot_workspace.py`

- [ ] **Step 1: Write the script**

```python
"""One-shot script: create the bot's calendar / Bitable base / Docs folder
and persist their IDs in bot_workspace. Re-runnable: detects existing
workspace row and exits cleanly.

Usage: python -m scripts.bootstrap_bot_workspace
"""
from __future__ import annotations

import asyncio
import logging
import sys

from db import queries
from feishu import calendar, bitable, drive

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> int:
    existing = queries.get_bot_workspace()
    if existing:
        logger.info("bot_workspace already bootstrapped: %s", existing)
        return 0

    lock = queries.acquire_bootstrap_lock()
    if not lock:
        logger.error("another process holds the bootstrap lock; refuse to proceed")
        return 1

    try:
        cal_id = await calendar.create_calendar(summary="包工头的日历")
        logger.info("created calendar: %s", cal_id)

        base_app_token, action_items_table_id, meetings_table_id = (
            await bitable.bootstrap_base()
        )
        logger.info("created Bitable base: %s", base_app_token)

        folder_token = await drive.create_folder(name="包工头的文档柜")
        logger.info("created Docs folder: %s", folder_token)

        queries.upsert_bot_workspace(
            calendar_id=cal_id,
            base_app_token=base_app_token,
            action_items_table_id=action_items_table_id,
            meetings_table_id=meetings_table_id,
            docs_folder_token=folder_token,
        )
        logger.info("bot_workspace persisted")
    finally:
        queries.release_bootstrap_lock(lock["id"])

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Implement the missing helpers in feishu/bitable.py and feishu/drive.py**

`bitable.bootstrap_base()` returns `(app_token, action_items_table_id, meetings_table_id)` — creates the base, then both tables with the schema in spec §4 step 3.

`drive.create_folder(name)` returns `folder_token`.

These should be added to the wrappers in Task Group 4 if not already; if missed, do them here.

- [ ] **Step 3: Manual run against dev tenant (REQUIRED)**

```bash
cd bot && python -m scripts.bootstrap_bot_workspace
```

Expected: logs show creation of calendar, base, folder; `bot_workspace` row exists in DB; running again logs "already bootstrapped".

- [ ] **Step 4: Manually verify in Feishu UI**

Open the bot's account in Feishu and confirm:
- "包工头的日历" appears in calendar list
- "包工头的工作台" Bitable base exists with `action_items` and `meetings` tables
- "包工头的文档柜" folder exists in Drive

- [ ] **Step 5: Commit**

```bash
git add bot/scripts/
git commit -m "feat(bootstrap): bot_workspace bootstrap script with lock + idempotent re-run"
```

---

# Task Group 6: RequestContext refactor + MCP module split (5 tasks — v21 expanded)

> **Spec ref:** §5.0, §11 step 5, §10 row 119 (split MCP servers).
>
> **v21 change**: this group used to be a 3-task pure refactor. v21 also
> splits the single `bot/agent/tools.py` into 5 domain MCP modules
> (`tools_meta`, `tools_calendar`, `tools_bitable`, `tools_doc`,
> `tools_external`), so the refactor + split are landed as one atomic
> 5-task transaction with a single commit at the end of 6.5.
>
> **CRITICAL — atomic transaction across 6.1 → 6.5**: do NOT commit until
> Task 6.5 step 5. The repo is in a broken state during 6.1–6.4 because
> `runner.py` and `app.py` reference symbols that don't exist yet. Tests
> only pass after the whole transaction is complete.

### Task 6.1: Rename tools.py → tools_meta.py + define RequestContext + factory pattern

**Files:**
- Create: `bot/agent/request_context.py`
- Move: `bot/agent/tools.py` → `bot/agent/tools_meta.py` (use `git mv` to preserve history)
- Modify: `bot/agent/tools_meta.py` (rewrite `build_pmo_mcp` → `build_meta_mcp(ctx)`; remove `_current_conversation_key_var`)
- Test: `bot/tests/test_request_context.py`

- [ ] **Step 1: Write failing test for closure capture**

```python
"""Verify that build_meta_mcp(ctx) tools see the latest ctx mutation."""
import pytest

from agent.request_context import RequestContext


def test_request_context_closure_sees_mutations():
    ctx = RequestContext()
    captured: list[str] = []

    def closure_reader():
        captured.append(ctx.message_id)

    ctx.message_id = "first"
    closure_reader()
    ctx.message_id = "second"
    closure_reader()

    assert captured == ["first", "second"]


def test_request_context_default_values():
    ctx = RequestContext()
    assert ctx.message_id == ""
    assert ctx.chat_id == ""
    assert ctx.sender_open_id == ""
    assert ctx.conversation_key == ""
```

- [ ] **Step 2: Run, expect ImportError**

- [ ] **Step 3: Implement RequestContext**

`bot/agent/request_context.py`:
```python
"""Per-pooled-client mutable request scope. See spec §5.0.

The runner mutates fields inside its slot.lock acquisition before
each client.query(). Tools read fields by closure over the dataclass
instance — no module globals, no contextvars, no fences.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RequestContext:
    message_id: str = ""
    chat_id: str = ""
    sender_open_id: str = ""
    conversation_key: str = ""
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Rename file and refactor to factory**

```bash
git mv bot/agent/tools.py bot/agent/tools_meta.py
```

Then edit `bot/agent/tools_meta.py`:

```python
"""pmo_meta MCP server — read-only profile/turn/activity tools + meta
helpers (today_iso, resolve_people, undo_last_action, generate_image).

The 8 calendar/bitable/doc/external tools live in tools_calendar.py,
tools_bitable.py, tools_doc.py, tools_external.py respectively. Each
file exports its own build_<domain>_mcp(ctx) factory. See spec §10
row 119 for why we split.
"""
from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_meta_tools(ctx: RequestContext):
    """Return tool definitions bound to ctx.

    Tests call this helper directly so they can invoke `tool_def.handler`
    without depending on private internals of the SDK's MCP server object.
    """

    @tool("today_iso", "...", {})
    async def today_iso(args: dict) -> dict:
        # body uses ctx.sender_open_id (Task 7.1)
        ...

    @tool("list_users", "...", {})
    async def list_users(args: dict) -> dict:
        ...  # body unchanged from old tools.py

    # ... lookup_user, get_recent_turns, get_project_overview,
    # get_activity_stats, generate_image (uses ctx.conversation_key)
    # resolve_people (Task 7.2), undo_last_action (Task 9.1)

    return [
        today_iso, list_users, lookup_user, get_recent_turns,
        get_project_overview, get_activity_stats, generate_image,
        resolve_people, undo_last_action,
    ]


def build_meta_mcp(ctx: RequestContext):
    """Factory: returns the meta MCP server bound to ctx. Called once per
    _PooledClient. See spec §5.0.
    """
    return create_sdk_mcp_server(
        name="pmo_meta",  # ← name change cascades to allowed_tools prefix
        version="0.1.0",
        tools=build_meta_tools(ctx),
    )
```

Delete the module-global `_current_conversation_key_var` and
`set_current_conversation` function. For `generate_image`, replace
`_current_conversation_key_var` reads with `ctx.conversation_key`.

> **Note**: `resolve_people` and `undo_last_action` are stubbed in this
> task (just enough to make `build_meta_mcp(ctx)` import); their real
> implementations land in Task 7.2 and Task 9.1 respectively.

- [ ] **Step 6: Run new test**

```bash
cd bot && pytest tests/test_request_context.py -v
```

Expected: pass.

The full test suite WILL break here because `runner.py` still references the old `build_pmo_mcp()` and `tools.py` paths. That's fixed in 6.2–6.4.

- [ ] **Step 7: NO COMMIT YET — proceed to Task 6.2**

### Task 6.2: Skeleton four new MCP module files

> Each file gets a `build_<domain>_tools(ctx)` helper, a
> `build_<domain>_mcp(ctx)` factory, and one internal `_not_ready` tool.
> The placeholder is not user-facing and MUST NOT be added to
> `allowed_tools`. Tests call `build_<domain>_tools(ctx)` directly so
> they can invoke `tool_def.handler(...)` without relying on SDK server
> internals.
>
> Why the placeholder exists: the installed `claude_agent_sdk` only
> registers MCP `tools/list` / `tools/call` handlers when `tools` is
> truthy. A truly empty `tools=[]` SDK MCP server returns `Method
> 'tools/list' not found` during initialization. The placeholder keeps
> the server protocol-valid until real tools are added in Task Groups
> 7–9.

**Files:**
- Create: `bot/agent/tools_calendar.py`
- Create: `bot/agent/tools_bitable.py`
- Create: `bot/agent/tools_doc.py`
- Create: `bot/agent/tools_external.py`

- [ ] **Step 1: Create skeletons (one identical pattern, 4 files)**

`bot/agent/tools_calendar.py`:
```python
"""pmo_calendar MCP server — schedule_meeting, cancel_meeting,
list_my_meetings. Real bodies land in Task Group 8 (write tools).
"""
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_calendar_tools(ctx: RequestContext):
    @tool(
        "_calendar_not_ready",
        "Internal placeholder so the SDK MCP server has protocol handlers before real tools land.",
        {},
    )
    async def _calendar_not_ready(args: dict) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "{\"error\":\"calendar tools not implemented yet\"}"}], "isError": True}

    return [_calendar_not_ready]


def build_calendar_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_calendar",
        version="0.1.0",
        tools=build_calendar_tools(ctx),  # replaced by Tasks 8.1, 8.2, 8.3
    )
```

`bot/agent/tools_bitable.py`:
```python
"""pmo_bitable MCP server — append_action_items, query_action_items,
create_bitable_table, append_to_my_table, query_my_table,
describe_my_table. Real bodies land in Tasks 7.7/7.8 + 8.4 + 8.8/8.9.
"""
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_bitable_tools(ctx: RequestContext):
    @tool(
        "_bitable_not_ready",
        "Internal placeholder so the SDK MCP server has protocol handlers before real tools land.",
        {},
    )
    async def _bitable_not_ready(args: dict) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "{\"error\":\"bitable tools not implemented yet\"}"}], "isError": True}

    return [_bitable_not_ready]


def build_bitable_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_bitable", version="0.1.0", tools=build_bitable_tools(ctx),
    )
```

`bot/agent/tools_doc.py`:
```python
"""pmo_doc MCP server — create_meeting_doc, create_doc, append_to_doc.
Shared private helper `_drive_import_markdown` is defined here too.
Real bodies land in Tasks 8.5, 8.6, 8.7.
"""
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_doc_tools(ctx: RequestContext):
    @tool(
        "_doc_not_ready",
        "Internal placeholder so the SDK MCP server has protocol handlers before real tools land.",
        {},
    )
    async def _doc_not_ready(args: dict) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "{\"error\":\"doc tools not implemented yet\"}"}], "isError": True}

    return [_doc_not_ready]


def build_doc_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_doc", version="0.1.0", tools=build_doc_tools(ctx),
    )
```

`bot/agent/tools_external.py`:
```python
"""pmo_external MCP server — read_doc, read_external_table,
resolve_feishu_link. Read-any tools (no workspace gate). Real bodies
land in Tasks 7.4, 7.5, 7.6.
"""
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_external_tools(ctx: RequestContext):
    @tool(
        "_external_not_ready",
        "Internal placeholder so the SDK MCP server has protocol handlers before real tools land.",
        {},
    )
    async def _external_not_ready(args: dict) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "{\"error\":\"external tools not implemented yet\"}"}], "isError": True}

    return [_external_not_ready]


def build_external_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_external", version="0.1.0", tools=build_external_tools(ctx),
    )
```

- [ ] **Step 2: Verify imports**

```bash
cd bot && python -c "
from agent.tools_calendar import build_calendar_mcp
from agent.tools_bitable import build_bitable_mcp
from agent.tools_doc import build_doc_mcp
from agent.tools_external import build_external_mcp
from agent.request_context import RequestContext
ctx = RequestContext()
build_calendar_mcp(ctx); build_bitable_mcp(ctx)
build_doc_mcp(ctx); build_external_mcp(ctx)
print('all 4 skeletons import cleanly')
"
```

Expected: prints success. No commit yet.

### Task 6.3: Wire RequestContext + 5 MCP servers through runner.py

**Files:**
- Modify: `bot/agent/runner.py`

- [ ] **Step 1: Update imports**

Replace the old `from agent import tools as agent_tools` with imports of all 5 builders:

```python
from agent.request_context import RequestContext
from agent.tools_meta import build_meta_mcp
from agent.tools_calendar import build_calendar_mcp
from agent.tools_bitable import build_bitable_mcp
from agent.tools_doc import build_doc_mcp
from agent.tools_external import build_external_mcp
```

- [ ] **Step 2: Add ctx field to _PooledClient**

```python
@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    ctx: RequestContext = field(default_factory=RequestContext)
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

- [ ] **Step 3: Update _get_client — register 5 MCP servers, keep allowed_tools read-only for now**

```python
async def _get_client(conversation_key: str) -> _PooledClient:
    async with _pool_lock:
        slot = _pool.get(conversation_key)
        if slot is None:
            ctx = RequestContext()
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                allowed_tools=[
                    # Existing read tools only. Task 10 expands this list
                    # after the real domain tools exist. Do NOT whitelist
                    # future tool names while their modules still only carry
                    # `_not_ready` placeholders.
                    "mcp__pmo_meta__list_users",
                    "mcp__pmo_meta__lookup_user",
                    "mcp__pmo_meta__get_recent_turns",
                    "mcp__pmo_meta__get_project_overview",
                    "mcp__pmo_meta__get_activity_stats",
                    "mcp__pmo_meta__today_iso",
                    "mcp__pmo_meta__generate_image",
                ],
                mcp_servers={
                    "pmo_meta": build_meta_mcp(ctx),
                    "pmo_calendar": build_calendar_mcp(ctx),
                    "pmo_bitable": build_bitable_mcp(ctx),
                    "pmo_doc": build_doc_mcp(ctx),
                    "pmo_external": build_external_mcp(ctx),
                },
                disallowed_tools=[...],  # unchanged
                max_turns=settings.agent_max_duration_seconds,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            slot = _PooledClient(client=client, ctx=ctx)
            _pool[conversation_key] = slot
        slot.last_used = time.monotonic()
        return slot
```

> Task 10 performs the full v21 whitelist rewrite once Tasks 7–9 have
> replaced the `_not_ready` placeholders with real tools. Until then,
> keeping the whitelist read-only preserves existing behavior for the
> Task 6.5 regression smoke.

- [ ] **Step 4: Update answer_streaming signature + ctx mutation**

```python
async def answer_streaming(
    conversation_key: str, question: str,
    *, message_id: str, chat_id: str, sender_open_id: str,
):
    slot = await _get_client(conversation_key)
    async with slot.lock:
        slot.busy = True
        slot.ctx.message_id = message_id
        slot.ctx.chat_id = chat_id
        slot.ctx.sender_open_id = sender_open_id
        slot.ctx.conversation_key = conversation_key
        # REMOVE: agent_tools.set_current_conversation(conversation_key)
        try:
            await slot.client.query(question)
            # ... rest unchanged
```

- [ ] **Step 5: Update answer() signature + delegate**

```python
async def answer(
    conversation_key: str, question: str,
    *, message_id: str, chat_id: str, sender_open_id: str,
) -> str:
    answer_text = ""
    async for ev in answer_streaming(
        conversation_key, question,
        message_id=message_id, chat_id=chat_id, sender_open_id=sender_open_id,
    ):
        if ev["kind"] == "final":
            answer_text = ev["text"]
    return answer_text or "(空回答 — 试试换个问法?)"
```

- [ ] **Step 6: NO COMMIT — proceed to 6.4**

### Task 6.4: Update app.py call sites + prefix-strip for new MCP names

**Files:**
- Modify: `bot/app.py`

- [ ] **Step 1: Update streaming call site (around app.py:184)**

```python
async for event in agent_runner.answer_streaming(
    conversation_key, framed_question,
    message_id=ev.message_id, chat_id=ev.chat_id, sender_open_id=ev.sender_open_id,
):
    ...
```

- [ ] **Step 2: Update fallback answer() call site (around app.py:158)**

```python
answer = await asyncio.wait_for(
    agent_runner.answer(
        conversation_key, framed_question,
        message_id=ev.message_id, chat_id=ev.chat_id,
        sender_open_id=ev.sender_open_id,
    ),
    timeout=settings.agent_max_duration_seconds,
)
```

- [ ] **Step 3: Update tool-name prefix strip (app.py:255)**

The old code stripped a single `mcp__pmo__` prefix when displaying tool
calls in the chat trace. v21 has 5 prefixes; replace it with a helper:

```python
# Old:
# display_name = tool_name.removeprefix("mcp__pmo__")

# New:
_PMO_PREFIXES = (
    "mcp__pmo_meta__",
    "mcp__pmo_calendar__",
    "mcp__pmo_bitable__",
    "mcp__pmo_doc__",
    "mcp__pmo_external__",
)


def _strip_pmo_prefix(tool_name: str) -> str:
    for p in _PMO_PREFIXES:
        if tool_name.startswith(p):
            return tool_name[len(p):]
    return tool_name


display_name = _strip_pmo_prefix(tool_name)
```

- [ ] **Step 4: NO COMMIT — proceed to 6.5**

### Task 6.5: Verify + atomic commit for the whole 6.1–6.4 transaction

- [ ] **Step 1: Run full pytest suite**

```bash
cd bot && pytest tests/ -v
```

Expected: all pass. The new MCP modules expose only internal
`_*_not_ready` placeholder tools so the SDK MCP protocol has
`tools/list` / `tools/call` handlers. Those placeholders are NOT in
`allowed_tools`; the whitelist remains the existing read-only meta
tools until Task 10 expands it after real tools exist.

- [ ] **Step 2: Manual smoke — read-only flow still works**

Send a question to the bot in Feishu (e.g., "@包工头 albert 昨天做了啥").
Expected: existing read-only behavior unchanged. The 7 read tools now
live under `mcp__pmo_meta__*` prefix; chat trace should show tool names
without that prefix.

- [ ] **Step 3: Commit (the whole 6.1+6.2+6.3+6.4 transaction)**

```bash
git add bot/agent/request_context.py bot/agent/tools_meta.py \
        bot/agent/tools_calendar.py bot/agent/tools_bitable.py \
        bot/agent/tools_doc.py bot/agent/tools_external.py \
        bot/agent/runner.py bot/app.py bot/tests/test_request_context.py
git rm bot/agent/tools.py 2>/dev/null || true  # already moved by git mv
git commit -m "refactor(agent): split tools.py into 5 domain MCP modules + RequestContext closure"
```

> **Why one commit and not five**: each intermediate state breaks
> imports or tests. Splitting tools.py without updating runner.py
> breaks the agent at startup; updating runner.py without skeletons
> for the new modules raises ImportError. Atomic transaction is the
> only safe unit. (Same pattern used in spec v17 for the schema +
> queries transaction.)

---

# Task Group 7: Read tools (8 tasks — v21 expanded)

> **Spec ref:** §3.1 (resolve_people), §3.2 (today_iso extension), §3.7 (query_action_items), §3.11 (read_doc), §3.15 (query_my_table), §3.16 (describe_my_table), §3.17 (read_external_table), §3.18 (resolve_feishu_link).
>
> Read tools are simpler than write tools — no Phase 2.X.5, no idempotency,
> no `bot_actions` rows. Read-any tools (`read_doc`, `read_external_table`,
> `resolve_feishu_link`) live in `tools_external.py`. Read-own bitable
> tools (`query_my_table`, `describe_my_table`) live in `tools_bitable.py`.

### Task 7.1: today_iso extension (timezone field)

**Files:**
- Modify: `bot/agent/tools_meta.py` (the `today_iso` inner function)
- Test: `bot/tests/test_tools_meta_today_iso.py`

- [ ] TDD per spec §3.2: tool calls `feishu.contact.get_user(open_id=ctx.sender_open_id)` and adds `user_timezone` + `user_today_local` fields.

- [ ] Commit: `feat(tools): today_iso returns user_timezone via contact.user.get`

### Task 7.2: resolve_people (3-tier resolution)

**Files:**
- Modify: `bot/agent/tools_meta.py` (new tool)
- Test: `bot/tests/test_tools_meta_resolve_people.py`

- [ ] Implement per spec §3.1:
  - Step 1: query `profiles` + `feishu_links` (existing `db.queries.lookup_by_feishu_open_id` is for the reverse direction; add new `lookup_handle_or_email` query if needed)
  - Step 2: input shape regex (email / phone) → `feishu.contact.batch_get_id_by_email_or_phone`
  - Step 3: name → `feishu.contact.search_users`
- [ ] Returns `{resolved, ambiguous, unresolved}` shape per spec.
- [ ] Tool description directive about ambiguous handling.
- [ ] Commit.

### Task 7.3: query_action_items

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_query_action_items.py`

- [ ] Reads from the bot's Bitable `action_items` table via `feishu.bitable.search_records(table_id=ws.action_items_table_id, ...)` with optional filters (owner / project / status / since / until).
- [ ] Pass `user_id_type="open_id"`.
- [ ] Commit.

### Task 7.4: resolve_feishu_link (NEW v21)

> **Spec ref:** §3.18. Pure URL → metadata resolver. Wires `feishu/links.py`
> + `feishu/wiki.py` so the LLM can paste a Feishu URL and get back
> `{kind, token, ...}` for use in subsequent tool calls.

**Files:**
- Modify: `bot/agent/tools_external.py`
- Test: `bot/tests/test_tools_external_resolve_feishu_link.py`

- [ ] **Step 1: Failing test for docx + base + wiki redirect**

```python
import json
import pytest
from unittest.mock import AsyncMock

from agent.request_context import RequestContext
from agent.tools_external import build_external_tools


@pytest.mark.asyncio
async def test_resolve_docx_url(monkeypatch):
    ctx = RequestContext()
    tool_def = next(t for t in build_external_tools(ctx) if t.name == "resolve_feishu_link")
    out = await tool_def.handler({"url": "https://example.feishu.cn/docx/dxAAAA"})
    assert out["content"][0]["text"] == json.dumps(
        {"kind": "docx", "token": "dxAAAA"}, ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_resolve_wiki_redirects(monkeypatch):
    from feishu import wiki
    monkeypatch.setattr(
        wiki, "resolve_node",
        AsyncMock(return_value={"obj_token": "dxBBBB", "obj_type": "docx"}),
    )
    ctx = RequestContext()
    tool_def = next(t for t in build_external_tools(ctx) if t.name == "resolve_feishu_link")
    out = await tool_def.handler({"url": "https://example.feishu.cn/wiki/wikC"})
    payload = json.loads(out["content"][0]["text"])
    assert payload == {"kind": "docx", "token": "dxBBBB", "via_wiki": "wikC"}
```

- [ ] **Step 2: Implement in `tools_external.py`**

```python
from feishu import links, wiki


@tool(
    "resolve_feishu_link",
    "Parse a Feishu URL (docx / wiki / base / sheet) and return its kind "
    "and underlying token. Use this when the user pastes a doc/base link "
    "before calling read_doc / read_external_table.",
    {"url": str},
)
async def resolve_feishu_link(args: dict) -> dict:
    try:
        parsed = links.parse_url(args["url"])
        if parsed["kind"] == "wiki":
            node = await wiki.resolve_node(parsed["token"])
            kind_map = {
                "docx": "docx", "doc": "doc",
                "bitable": "bitable", "sheet": "sheet",
            }
            redirected = {
                "kind": kind_map.get(node["obj_type"], node["obj_type"]),
                "token": node["obj_token"],
                "via_wiki": parsed["token"],
            }
            return _ok(redirected)
        return _ok(parsed)
    except ValueError as e:
        return _err(str(e))
```

- [ ] **Step 3: Return tool from `build_external_tools`**, run tests → green; commit:

```bash
git add bot/agent/tools_external.py bot/tests/test_tools_external_resolve_feishu_link.py
git commit -m "feat(tools): resolve_feishu_link — parse Feishu URL → {kind, token}"
```

### Task 7.5: read_doc (NEW v21)

> **Spec ref:** §3.11. Read-any tool: any docx the bot can
> `docs:document:readonly`-access (or that the user shared with it).
> Walks blocks, renders to markdown, truncates at `max_chars` (default
> 20000 characters).

**Files:**
- Modify: `bot/agent/tools_external.py`
- Test: `bot/tests/test_tools_external_read_doc.py`

- [ ] **Step 1: Failing test — render simple doc + truncation + 403 mapping**

```python
@pytest.mark.asyncio
async def test_read_doc_renders_blocks(monkeypatch):
    from feishu import docx
    fake_blocks = [
        MagicMock(block_type=2, text=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="Hello world"))
        ])),
        MagicMock(block_type=4, heading2=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="Section A"))
        ])),
    ]
    monkeypatch.setattr(docx, "list_blocks", AsyncMock(return_value=fake_blocks))

    ctx = RequestContext()
    tool_def = next(t for t in build_external_tools(ctx) if t.name == "read_doc")
    out = await tool_def.handler({"doc_link_or_token": "doc_xxx"})
    payload = json.loads(out["content"][0]["text"])
    assert "Hello world" in payload["markdown"]
    assert "## Section A" in payload["markdown"]
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_read_doc_403_returns_friendly_error(monkeypatch):
    from feishu import docx
    monkeypatch.setattr(
        docx, "list_blocks",
        AsyncMock(side_effect=RuntimeError("docx.list_blocks failed: 99991663 PermissionDenied")),
    )
    ctx = RequestContext()
    tool_def = next(t for t in build_external_tools(ctx) if t.name == "read_doc")
    out = await tool_def.handler({"doc_link_or_token": "doc_xxx"})
    assert out["isError"] is True
    payload = json.loads(out["content"][0]["text"])
    assert "PermissionDenied" in payload["error"] or "403" in payload["error"]
```

- [ ] **Step 2: Implement (with markdown renderer for text/heading/list/code/quote/table block types per spec §3.11)**

Key behaviors:
- Normalize `doc_link_or_token`: if it looks like a Feishu URL, call
  `resolve_feishu_link` first; otherwise treat it as a raw docx token.
- Walk via `feishu.docx.list_blocks(doc_token)`.
- Render each block to markdown using a `_render_block(block)` helper (see spec §3.11 mapping).
- After rendering, truncate at `max_chars` (default 20000); set
  `truncated=True` and append the spec's clear truncation marker. v21
  does **not** expose `start_block_id`; if the user needs more, the
  bot should ask for a narrower section or a smaller pasted excerpt.
- Map Feishu's `99991663` (Permission denied) to a friendly error message that suggests the user share the doc.

- [ ] **Step 3: Run tests → green; commit:**

```bash
git add bot/agent/tools_external.py bot/tests/test_tools_external_read_doc.py
git commit -m "feat(tools): read_doc — render arbitrary docx to markdown"
```

### Task 7.6: read_external_table (NEW v21)

> **Spec ref:** §3.17. Read-any bitable. Rate-limited to 5 calls per hour
> per `conversation_key` (in-memory deque per slot, reset on bot restart).
> `page_size` defaults to 50 and is capped at 200. Pagination uses
> Feishu `page_token` / `next_page_token`, not offset.

**Files:**
- Modify: `bot/agent/tools_external.py`
- Test: `bot/tests/test_tools_external_read_external_table.py`

- [ ] **Step 1: Failing test — happy path + rate-limit + page_size cap**

(Use `freezegun` to advance time across the rate-limit window.)

- [ ] **Step 2: Implement** with module-level `_external_table_calls:
  `dict[str, deque[float]]` keyed by `ctx.conversation_key`. Drop
  entries older than 3600s. Normalize `base_link_or_app_token` via
  `resolve_feishu_link` for URL inputs, pass through `page_token`, cap
  `page_size` at 200, and return `{records, has_more, next_page_token}`.

- [ ] **Step 3: Run tests → green; commit:**

```bash
git add bot/agent/tools_external.py bot/tests/test_tools_external_read_external_table.py
git commit -m "feat(tools): read_external_table with 5/hour rate limit"
```

### Task 7.7: describe_my_table (NEW v21)

> **Spec ref:** §3.16. Returns the schema (field name + field_type) of one
> of the bot's own bitable tables. Workspace gate: refuse any
> `app_token != ws.base_app_token`. Refuse `table_id == ws.action_items_table_id`
> (LLM should use `query_action_items` for that).

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_my_table.py` (shared file with 7.8)

- [ ] **Step 1: Failing test — workspace-gate refusal + happy path**

- [ ] **Step 2: Implement using `feishu.bitable.list_fields(app_token, table_id)`. Return `{table_id, fields: [{name, type}]}`.**

- [ ] **Step 3: Commit:** `feat(tools): describe_my_table — schema readout for bot-owned tables`

### Task 7.8: query_my_table (NEW v21)

> **Spec ref:** §3.15. Same workspace gate as 7.7. Filter/sort syntax mirrors
> `query_action_items`. `page_size` defaults to 50 and is capped at 200.
> Returns `{records, has_more, next_page_token}`.

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_my_table.py` (shared file)

- [ ] **Step 1: Failing test — workspace-gate refusal + filter pass-through**

- [ ] **Step 2: Implement using `feishu.bitable.search_records`.**

- [ ] **Step 3: Commit:** `feat(tools): query_my_table — read bot-owned bitable rows`

---

# Task Group 8: Write tools (9 tasks — v21 expanded)

> Each task follows the spec §3.X for inputs / phases / failure handling,
> and the §5.1 skeleton for the three-phase pattern.
>
> **v21 added:** create_doc (8.6), append_to_doc (8.7),
> create_bitable_table (8.8), append_to_my_table (8.9). These all reuse
> the same workspace gate + Phase -1 / 0 / 1 / 2.X / 2.X.5 / 3 pattern;
> append_to_doc additionally enforces an authorship gate (the doc must
> have been created by the bot, identified by a `bot_actions` row with
> `target_kind='docx'` and
> `action_type IN ('create_doc','create_meeting_doc')`; prior append
> rows do not grant authorship).
>
> **File mapping** (where each tool body lives — important since the spec
> v21 split MCP modules):
> - schedule_meeting / cancel_meeting / list_my_meetings → `tools_calendar.py`
> - append_action_items / create_bitable_table / append_to_my_table → `tools_bitable.py`
> - create_meeting_doc / create_doc / append_to_doc → `tools_doc.py`

### Task 8.1: schedule_meeting (split into 8.1a–8.1g for granularity)

> **Depends on:** Task Group 3 (DB helpers), Task 4.4 (calendar wrappers), Task 6.x (RequestContext).
> **Spec ref:** §3.3 phases 0–3, §5.1 skeleton, §10 row 100 (effective_attendees), row 112 (idempotency_key).
>
> The spec defines 8 distinct phases. We split into 7 sub-tasks, each one failing test → impl → pass → commit. Skeleton appears once in 8.1a; later sub-tasks just delta against it.

#### Task 8.1a: schedule_meeting Phase -1 + 0 (validation + Phase 0 dedup)

**Files:**
- Modify: `bot/agent/tools_calendar.py` (add `schedule_meeting` inner function inside `build_calendar_tools(ctx)`)
- Test: `bot/tests/test_tools_calendar_schedule_meeting.py`

- [ ] **Step 1: Write the first three failing tests**

```python
"""Tests for schedule_meeting tool. Mocks DB helpers + Feishu calendar wrappers."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.request_context import RequestContext
from agent.tools_calendar import build_calendar_tools


@pytest.fixture
def ctx():
    return RequestContext(
        message_id="msg_1", chat_id="oc_1", sender_open_id="ou_asker",
        conversation_key="oc_1:ou_asker",
    )


@pytest.fixture
def schedule_meeting(ctx, monkeypatch):
    """Build the MCP server fresh per test, return the schedule_meeting handler."""
    tool_def = next(t for t in build_calendar_tools(ctx) if t.name == "schedule_meeting")
    return tool_def.handler


@pytest.mark.asyncio
async def test_phase_minus_1_rejects_missing_required_args(schedule_meeting):
    result = await schedule_meeting({"title": "X"})  # no start_time, no attendees
    assert result["isError"] is True
    body = result["content"][0]["text"]
    assert "start_time" in body or "attendee_open_ids" in body


@pytest.mark.asyncio
async def test_phase_minus_1_rejects_non_rfc3339_start_time(schedule_meeting):
    result = await schedule_meeting({
        "title": "X", "start_time": "tomorrow 3pm",
        "attendee_open_ids": ["ou_a"],
    })
    assert result["isError"] is True
    assert "RFC3339" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_phase_minus_1_allows_empty_attendees_when_asker_auto_included(
    schedule_meeting, monkeypatch,
):
    """Self-only meetings/focus blocks are valid because include_asker defaults True."""
    monkeypatch.setattr(
        "db.queries.get_locked_by_logical_key",
        lambda _key: {
            "status": "success",
            "result": {"event_id": "evt_self", "link": "https://..."},
        },
    )
    result = await schedule_meeting({
        "title": "Focus block",
        "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": [],
    })
    assert result.get("isError") is not True


@pytest.mark.asyncio
async def test_phase_0_returns_dedup_when_logical_key_locked(
    schedule_meeting, monkeypatch,
):
    """If get_locked_by_logical_key returns a successful prior row,
    return its result with deduplicated_from_logical_key=True."""
    monkeypatch.setattr(
        "db.queries.get_locked_by_logical_key",
        lambda _key: {
            "status": "success",
            "result": {"event_id": "evt_prior", "link": "https://..."},
        },
    )
    result = await schedule_meeting({
        "title": "X",
        "start_time": "2026-05-08T15:00:00+08:00",
        "duration_minutes": 30,
        "attendee_open_ids": ["ou_a"],
    })
    body = json.loads(result["content"][0]["text"])
    assert body["deduplicated_from_logical_key"] is True
    assert body["event_id"] == "evt_prior"
```

- [ ] **Step 2: Run, expect failures (tool not yet defined)**

```bash
cd bot && pytest tests/test_tools_schedule_meeting.py -v
```

- [ ] **Step 3: Implement the skeleton in `tools_calendar.py` inside `build_calendar_tools(ctx)`**

```python
@tool(
    "schedule_meeting",
    "Schedule a Feishu calendar meeting. Inputs: title, start_time "
    "(RFC3339 with timezone — call today_iso first), duration_minutes "
    "(default 30), attendee_open_ids (must come from resolve_people; "
    "asker is auto-added unless include_asker=False), description, "
    "reminder_minutes (default 15).",
    {
        "title": str, "start_time": str, "duration_minutes": int,
        "attendee_open_ids": list, "description": str,
        "reminder_minutes": int, "include_asker": bool,
    },
)
async def schedule_meeting(args: dict) -> dict:
    from db import queries
    from agent.canonical_args import canonicalize_args, compute_logical_key

    # Phase -1: validation
    if not args.get("title"):
        return _err("title is required")
    if not args.get("start_time"):
        return _err("start_time is required (RFC3339 with timezone)")
    try:
        from datetime import datetime
        start = datetime.fromisoformat(args["start_time"].replace("Z", "+00:00"))
        if start.tzinfo is None:
            return _err("start_time must include timezone (RFC3339)")
    except ValueError:
        return _err(f"start_time is not RFC3339: {args['start_time']!r}")
    if not args.get("attendee_open_ids") and args.get("include_asker") is False:
        return _err(
            "attendee_open_ids must be non-empty when include_asker=False "
            "(resolve via resolve_people)"
        )

    canonical = canonicalize_args(args, action_type="schedule_meeting")
    logical_key = compute_logical_key(
        chat_id=ctx.chat_id, sender_open_id=ctx.sender_open_id,
        action_type="schedule_meeting", canonical_args=canonical,
    )

    # Phase 0: logical-key dedup
    existing = queries.get_locked_by_logical_key(logical_key)
    if existing:
        if existing["status"] == "success":
            return _ok({**existing["result"], "deduplicated_from_logical_key": True})
        if existing["status"] == "reconciled_unknown":
            return _err(
                f"a previous identical call left a partial result on Feishu "
                f"(target_id={existing.get('target_id')}); please ask me to "
                f"undo it before re-issuing"
            )
        return _err("a previous identical call is in flight")

    # Phases 1+ — TODO in next sub-tasks
    return _err("not yet implemented past Phase 0")
```

- [ ] **Step 4: Run, expect first three tests pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(tools): schedule_meeting Phase -1 + Phase 0 (validation + dedup)"
```

#### Task 8.1b-pre: schedule_meeting Phase 1a (exact-message idempotency lookup)

> **Spec ref:** §5.1 skeleton — Phase 1a runs **before** the Phase 1b
> insert. It dispatches based on whether a row already exists for
> `(message_id, action_type)`:
> - `success` → return cached result (webhook retry idempotency)
> - `pending` → return "in flight"
> - `reconciled_unknown` → return "please undo first"
> - `failed` → call `update_for_retry` to bump attempt_count and
>   transition back to pending; on `LogicalKeyConflict` from
>   re-acquiring the lock, dispatch identically to Phase 1b's logical
>   conflict
> - `undone` → reject (the action was reversed; user must re-issue
>   as a fresh utterance with a new logical_key)
>
> Without Phase 1a, an exact-message retry after a technical failure
> never reaches `update_for_retry` and just lands as a
> MessageActionConflict — the `failed` row is never reclaimed and the
> error path is misreported as "concurrent call in flight" (spec
> §5.1 + Codex plan-review iter-3 #2).

**Files:**
- Modify: `bot/agent/tools_calendar.py` (insert Phase 1a before the existing Phase 1)
- Modify: `bot/tests/test_tools_calendar_schedule_meeting.py`

- [ ] **Step 1: Write failing tests for the five Phase 1a branches**

```python
@pytest.mark.asyncio
async def test_phase_1a_success_returns_cached_no_feishu_call(schedule_meeting, monkeypatch):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr("db.queries.get_bot_action", lambda mid, at: {
        "status": "success",
        "result": {"event_id": "evt_existing", "link": "https://..."},
    })
    # No Feishu mocks → if any are called, AttributeError
    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    body = json.loads(result["content"][0]["text"])
    assert body["event_id"] == "evt_existing"


@pytest.mark.asyncio
async def test_phase_1a_pending_returns_in_flight(schedule_meeting, monkeypatch):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr("db.queries.get_bot_action", lambda mid, at: {
        "status": "pending", "result": {},
    })
    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    assert result["isError"] is True
    assert "in flight" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_phase_1a_reconciled_unknown_partial_returns_undo_prompt(schedule_meeting, monkeypatch):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr("db.queries.get_bot_action", lambda mid, at: {
        "status": "reconciled_unknown",
        "target_id": "evt_orphan",
        "result": {"reconciliation_kind": "partial_success"},
    })
    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    assert result["isError"] is True
    assert "undo" in result["content"][0]["text"].lower() or "撤销" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_phase_1a_failed_calls_update_for_retry_then_continues(schedule_meeting, monkeypatch):
    """failed row → update_for_retry transitions to pending → continue from Phase 2.0."""
    calls = {"update_for_retry": 0}
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr("db.queries.get_bot_action", lambda mid, at: {
        "id": "u-failed", "status": "failed", "result": {},
    })

    def fake_retry(action_id, *, new_args, logical_key):
        calls["update_for_retry"] += 1
        return {"id": "u-failed", "status": "pending", "attempt_count": 2}
    monkeypatch.setattr("db.queries.update_for_retry", fake_retry)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: None)  # Phase 2.0 fails

    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    assert calls["update_for_retry"] == 1
    assert "bot_workspace" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_phase_1a_undone_rejects_fresh_request(schedule_meeting, monkeypatch):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr("db.queries.get_bot_action", lambda mid, at: {
        "status": "undone", "result": {},
    })
    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    assert result["isError"] is True
    assert "fresh" in result["content"][0]["text"].lower() or "已撤销" in result["content"][0]["text"]
```

- [ ] **Step 2: Run, expect failures**

- [ ] **Step 3: Insert Phase 1a logic in `schedule_meeting` body BETWEEN Phase 0 and Phase 1**

```python
    # Phase 1a: idempotency check (exact-message retry path)
    existing = queries.get_bot_action(ctx.message_id, "schedule_meeting")
    if existing:
        st = existing.get("status")
        if st == "success":
            return _ok(existing.get("result") or {})
        if st == "pending":
            return _err("a previous identical call is in flight")
        if st == "reconciled_unknown":
            return _err(
                f"a previous identical call left a partial result on Feishu "
                f"(target_id={existing.get('target_id')}); please ask me to "
                f"undo it before re-issuing"
            )
        if st == "undone":
            return _err(
                "this action was undone; if you want to redo it, "
                "issue it as a fresh request"
            )
        if st == "failed":
            try:
                retried = queries.update_for_retry(
                    existing["id"], new_args=args, logical_key=logical_key,
                )
            except queries.LogicalKeyConflict as e:
                # A different message acquired the slot since this row
                # failed; same dispatch as Phase 1b.
                winner = e.existing_row
                if winner and winner.get("status") == "success":
                    return _ok({**(winner.get("result") or {}),
                                "deduplicated_from_logical_key": True})
                if winner and winner.get("status") == "reconciled_unknown":
                    return _err(
                        f"a previous identical call left a partial result on "
                        f"Feishu (target_id={winner.get('target_id')}); "
                        f"please undo first"
                    )
                return _err("a previous identical call is in flight")
            if retried is None:
                return _err("a concurrent retry won the race; try again in a moment")
            action_row = retried
            action_id = action_row["id"]
            # Skip Phase 1b — we already have a pending row
            phase_1b_skipped = True
        # 'failed' branch falls through after action_row is set above
```

Then guard the existing Phase 1b code so it only runs when Phase 1a didn't claim a row:

```python
    if not locals().get("phase_1b_skipped", False):
        # Phase 1: pending insert + constraint dispatch (unchanged from 8.1b)
        try:
            action_row = queries.insert_bot_action_pending(...)
        except queries.MessageActionConflict as e:
            ...  # (unchanged)
        except queries.LogicalKeyConflict as e:
            ...  # (unchanged)
        action_id = action_row["id"]
```

- [ ] **Step 4: Run, expect pass — all 8.1a, 8.1b-pre, and 8.1b tests**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(tools): schedule_meeting Phase 1a (exact-message idempotency lookup with failed-retry)"
```

#### Task 8.1b: schedule_meeting Phase 1 (pending insert with constraint dispatch)

> **Note (post-iter-3 fix):** Phase 1b now runs only when Phase 1a's
> lookup returned None. Task 8.1b-pre adds the gating `if not
> phase_1b_skipped` block; this task's implementation is unchanged
> from the original plan, just nested inside that block.

**Files:**
- Modify: `bot/agent/tools_calendar.py`
- Modify: `bot/tests/test_tools_calendar_schedule_meeting.py`

- [ ] **Step 1: Write failing tests for the three insert outcomes**

```python
@pytest.mark.asyncio
async def test_phase_1_happy_inserts_pending_and_continues(schedule_meeting, monkeypatch):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    inserted = {"id": "u-1", "status": "pending"}
    monkeypatch.setattr(
        "db.queries.insert_bot_action_pending", lambda **kw: inserted,
    )
    # Force later phases to error so we can detect Phase 1 ran
    monkeypatch.setattr(
        "db.queries.get_bot_workspace", lambda: None,  # → Phase 2.0 fails
    )
    result = await schedule_meeting({
        "title": "X",
        "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    # We expect a workspace-missing error from Phase 2.0
    assert "bot_workspace" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_phase_1_message_conflict_returns_cached_success(
    schedule_meeting, monkeypatch,
):
    from db.queries import MessageActionConflict
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)

    def fake_insert(**kw):
        raise MessageActionConflict(existing_row={
            "status": "success", "result": {"event_id": "evt_existing"},
        })
    monkeypatch.setattr("db.queries.insert_bot_action_pending", fake_insert)

    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    body = json.loads(result["content"][0]["text"])
    assert body["event_id"] == "evt_existing"


@pytest.mark.asyncio
async def test_phase_1_logical_conflict_returns_dedup_or_in_flight(
    schedule_meeting, monkeypatch,
):
    from db.queries import LogicalKeyConflict
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)

    def fake_insert(**kw):
        raise LogicalKeyConflict(existing_row={
            "status": "success", "result": {"event_id": "evt_winner"},
        })
    monkeypatch.setattr("db.queries.insert_bot_action_pending", fake_insert)

    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],
    })
    body = json.loads(result["content"][0]["text"])
    assert body["deduplicated_from_logical_key"] is True
    assert body["event_id"] == "evt_winner"
```

- [ ] **Step 2: Run, expect failures**

- [ ] **Step 3: Add Phase 1 to schedule_meeting body** (after Phase 0)

```python
    # Phase 1: pending insert + constraint dispatch
    try:
        action_row = queries.insert_bot_action_pending(
            message_id=ctx.message_id,
            chat_id=ctx.chat_id,
            sender_open_id=ctx.sender_open_id,
            action_type="schedule_meeting",
            args=args,
            logical_key=logical_key,
        )
    except queries.MessageActionConflict as e:
        existing = e.existing_row
        if existing and existing.get("status") == "success":
            return _ok(existing["result"])
        return _err("a concurrent call is in flight; try again in a moment")
    except queries.LogicalKeyConflict as e:
        existing = e.existing_row
        if existing and existing.get("status") == "success":
            return _ok({**existing["result"], "deduplicated_from_logical_key": True})
        if existing and existing.get("status") == "reconciled_unknown":
            return _err(
                f"a previous identical call left a partial result on Feishu "
                f"(target_id={existing.get('target_id')}); please undo first"
            )
        return _err("a previous identical call is in flight")

    action_id = action_row["id"]
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(tools): schedule_meeting Phase 1 with MessageAction/LogicalKey conflict dispatch"
```

#### Task 8.1c: schedule_meeting Phase 2.0 (workspace lookup + effective_attendees)

- [ ] **Step 1: Failing test — verifies asker is unioned into the attendee list**

```python
@pytest.mark.asyncio
async def test_phase_2_0_unions_asker_into_attendees(
    schedule_meeting, monkeypatch, ctx,
):
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda _: None)
    monkeypatch.setattr(
        "db.queries.insert_bot_action_pending", lambda **kw: {"id": "u-1"},
    )
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {
        "calendar_id": "cal_bot", "base_app_token": "app_x",
        "action_items_table_id": "tbl_a", "meetings_table_id": "tbl_m",
        "docs_folder_token": "fld_x",
    })
    captured = {}

    async def fake_freebusy_batch(*, user_open_ids, **kw):
        captured["attendees"] = user_open_ids
        # Force Phase 2.1 conflict to short-circuit Phase 2.2+
        return [{"user_id": "ou_a", "busy_time": [{
            "start_time": kw["time_min"], "end_time": kw["time_max"],
        }]}]
    monkeypatch.setattr("feishu.calendar.freebusy_batch", fake_freebusy_batch)
    monkeypatch.setattr(
        "db.queries.mark_bot_action_success", lambda *a, **kw: {"id": "u-1"},
    )

    result = await schedule_meeting({
        "title": "X", "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a"],  # NOT including the asker
    })
    # Asker MUST be in the freebusy list
    assert "ou_asker" in captured["attendees"]
    assert "ou_a" in captured["attendees"]
```

- [ ] **Step 2: Run, expect failure**

- [ ] **Step 3: Add Phase 2.0 logic**

```python
    # Phase 2.0: workspace + effective_attendees
    workspace = queries.get_bot_workspace()
    if not workspace:
        queries.mark_bot_action_failed(
            action_id, error="bot_workspace not bootstrapped"
        )
        return _err(
            "bot_workspace is not bootstrapped — run "
            "scripts/bootstrap_bot_workspace.py against this environment first"
        )
    bot_calendar_id = workspace["calendar_id"]

    include_asker = args.get("include_asker", True)
    effective_attendees = list(set(args["attendee_open_ids"]))
    if include_asker and ctx.sender_open_id not in effective_attendees:
        effective_attendees.append(ctx.sender_open_id)
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(tools): schedule_meeting Phase 2.0 with effective_attendees auto-include-asker"
```

#### Task 8.1d: schedule_meeting Phase 2.1 (freebusy + conflict-as-success)

- [ ] **Step 1: Failing test — conflict marks success with outcome=conflict**

```python
@pytest.mark.asyncio
async def test_phase_2_1_conflict_marks_success_outcome(
    schedule_meeting, monkeypatch, ctx,
):
    # ... mocks like above, but freebusy returns a busy slot overlapping
    # request window; assert mark_bot_action_success is called with
    # result_patch={"outcome": "conflict", "conflicts": [...]}
```

- [ ] **Step 3: Phase 2.1 implementation**

```python
    # Phase 2.1: freebusy
    from feishu import calendar as fcal
    end_time = (start + timedelta(minutes=int(args.get("duration_minutes") or 30))).isoformat()
    freebusy = await fcal.freebusy_batch(
        user_open_ids=effective_attendees,
        time_min=args["start_time"], time_max=end_time,
    )
    conflicts = [
        {"open_id": u["user_id"], "busy_time": u["busy_time"]}
        for u in freebusy if u.get("busy_time")
    ]
    if conflicts:
        queries.mark_bot_action_success(
            action_id, result_patch={"outcome": "conflict", "conflicts": conflicts},
        )
        return _ok({"outcome": "conflict", "conflicts": conflicts})
```

- [ ] **Step 4-5: pass + commit**

#### Task 8.1e: schedule_meeting Phase 2.2 + 2.2.5 (create event with idempotency_key + intermediate persist)

- [ ] Failing test asserts:
  - `idempotency_key="schedule_meeting:<action_id>"` is passed to `calendar.create_event`
  - `queries.record_bot_action_target_pending` is called with `target_id=event_id`,
    `target_kind="calendar_event"`, and `result_patch.calendar_id` BEFORE Phase 2.3

- [ ] Implementation per spec §3.3 Phase 2.2 + 2.2.5 and Task 3.6a.

- [ ] Commit.

#### Task 8.1f: schedule_meeting Phase 2.3 (invite + partial-success handling)

- [ ] Failing tests:
  - Happy: invite_attendees called with `effective_attendees` and `user_id_type=open_id`
  - Failure: partial → `mark_bot_action_reconciled_unknown(kind='partial_success')` with `reconciliation_kind` in result; logical_key_locked stays True

- [ ] Implementation per spec §3.3 Phase 2.3 + iter-9 fix.

- [ ] Commit.

#### Task 8.1g: schedule_meeting Phase 3 (success terminal)

- [ ] Failing test:
  - `mark_bot_action_success` called with `result_patch={'link': ..., 'attendees': effective_attendees}` — note **effective_attendees, NOT raw input** (iter-15 #4)

- [ ] Implementation per spec §3.3 Phase 3.

- [ ] Commit.

### Task 8.2: cancel_meeting

> **Important — every write tool's body MUST follow the 8.1a + 8.1b-pre + 8.1b skeleton.** That means:
> - Phase -1: tool-specific input validation
> - Phase 0: `get_locked_by_logical_key` (logical-key dedup)
> - **Phase 1a: `get_bot_action(message_id, action_type)` lookup with all 5 status branches** (success/pending/reconciled_unknown/undone/failed→update_for_retry)
> - Phase 1b: `insert_bot_action_pending` (only when 1a returned None)
> - Phase 2.x: tool-specific Feishu calls, with intermediate `target_id`/`target_kind`
>   persistence via `queries.record_bot_action_target_pending` after each artifact-producing sub-step
> - Phase 3: `mark_bot_action_success` and (where applicable) `retire_source_action(source_row_id)` for related rows
>
> Tasks 8.2–8.5 list the **Phase 2.x specifics** below; Phase -1 / 0 / 1a / 1b / 3 are the same skeleton from 8.1a–c, and are NOT repeated. When in doubt, copy 8.1a's tool body and adjust Phase 2.x.

**Files:**
- Modify: `bot/agent/tools_calendar.py`
- Test: `bot/tests/test_tools_calendar_cancel_meeting.py`

Phases per spec §3.4:
- Resolution rules: `event_id` (status-gated, action_type IN schedule|restore) OR `last:true` (newest-row guard via `last_bot_action_for_sender_in_chat`, with already-cancelled idempotency check)
- Phase -1: extract `calendar_id` and `source_meeting_action_id` from source row
- Phase 0 / 1a / 1b: standard skeleton (8.1a-c)
- Phase 2a: `get_event(need_attendee=True, user_id_type="open_id")` → `pre_cancel_event_snapshot`
- Phase 2a.5: persist `target_id=<original_event_id>`, `target_kind='calendar_event_cancel'`, `result.pre_cancel_event_snapshot`, `result.calendar_id`, `result.source_meeting_action_id`
- Phase 2b: `delete_event`
- Phase 3: `mark_bot_action_success` for the cancel row + `retire_source_action(source_meeting_action_id)` to transition the original schedule row to `undone`

- [ ] Tests cover: explicit event_id (success/pending/undone/failed gates), `last:true` newest-row sentinels, idempotency double-cancel, cross-chat refusal.
- [ ] Commit: `feat(tools): cancel_meeting with snapshot-before-delete and source-row retire`

### Task 8.3: list_my_meetings

**Files:**
- Modify: `bot/agent/tools_calendar.py`
- Test: `bot/tests/test_tools_calendar_list_my_meetings.py`

Spec §3.5:
- target defaults to `"self"` → `ctx.sender_open_id`
- `primarys` to get user's calendar_id → `calendar_event.list(user_id_type="open_id")`
- Returns `{bot_known_events, user_calendar_events, visibility_note}`
- `bot_known_events` joins `bot_actions WHERE action_type IN ('schedule_meeting','restore_schedule_meeting') AND status IN ('success','reconciled_unknown') AND target_id IS NOT NULL AND result.attendees ⊇ {target}`

**JSON containment query in supabase-py**: PostgreSQL has the `@>`
("contains") operator on jsonb. PostgREST exposes it via the `cs`
(contains) filter. Concrete supabase-py call shape:

```python
sb_admin().table("bot_actions").select("*") \
    .in_("action_type", ["schedule_meeting", "restore_schedule_meeting"]) \
    .in_("status", ["success", "reconciled_unknown"]) \
    .not_.is_("target_id", "null") \
    .filter("result", "cs", json.dumps({"attendees": [target_open_id]})) \
    .execute()
```

The `result @> '{"attendees": ["ou_xxx"]}'::jsonb` SQL expression is
true iff `result.attendees` contains `"ou_xxx"` as one element. Note
the `cs` filter expects the right side as a JSON-serialized string,
not a Python dict.

- [ ] Tests cover: self default, explicit target, primarys 0 results graceful return, visibility_note rendering.
- [ ] Commit.

### Task 8.4: append_action_items

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_append_action_items.py`

Spec §3.6:
- Phase -1: ambiguous-project flow → return `needs_input` without `bot_actions` row
- Phase 0/1: same as schedule
- Phase 2: `bitable.batch_create_records` with `client_token=<bot_actions.id>` + hidden `source_action_id` per record
- Ambiguous failure (post-send 5xx/timeout): query by `source_action_id`, retry once with same `client_token`, otherwise `mark_reconciled_unknown(partial_success)`
- Phase 3: persist `target_kind='bitable_records'`, `target_id=<table_id>`, `result.record_ids`

- [ ] Tests cover: needs_input, happy path, retry-after-network-error, partial success.
- [ ] Commit.

### Task 8.5: create_meeting_doc (Path A 3-step)

**Files:**
- Modify: `bot/agent/tools_doc.py`
- Test: `bot/tests/test_tools_doc_create_meeting_doc.py`

Spec §3.8 Path A. Path B can be a future addition; keep this implementation Path A only.

- Phase 2.1: `drive.upload_all` — log `source_file_token` to stderr BEFORE Phase 2.1.5 UPDATE
- Phase 2.1.5: persist `result.source_file_token`
- Phase 2.2: `import_task.create` — log `ticket` to stderr before Phase 2.2.5
- Phase 2.2.5: persist `result.import_ticket`
- Phase 2.3: poll `import_task.get` (5-min timeout)
- Phase 2.3.5: persist `target_id=<doc_token>`, `target_kind='docx'`, `result.url`
- Phase 3: success

Failure handling per spec §3.8: pre-upload fail → `failed`; ambiguous (cleanup-failure / poll-timeout) → `reconciled_unknown(partial_success)`.

- [ ] Tests cover all 5 failure paths.
- [ ] Commit.

> **Refactor before 8.6**: factor the Path A 3-step body into a private
> helper `_drive_import_markdown(ctx, *, action_id, title, markdown) → {
> doc_token, source_file_token, import_ticket, url}` so create_doc (8.6)
> can reuse it without duplicating the four-phase logic. The helper is a
> module-level function in `tools_doc.py` (NOT exposed as a tool).

### Task 8.6: create_doc (NEW v21 — generic doc creation, no meeting linkage)

> **Spec ref:** §3.10. Same Path A pipeline as `create_meeting_doc`,
> but takes a `title` + `markdown` directly and does NOT cross-link to a
> meeting `bot_action`. Workspace gate: target folder must be
> `ws.docs_folder_token`.

**Files:**
- Modify: `bot/agent/tools_doc.py`
- Test: `bot/tests/test_tools_doc_create_doc.py`

- [ ] **Step 1: Failing test — happy path uses shared `_drive_import_markdown` helper**

```python
@pytest.mark.asyncio
async def test_create_doc_happy_path(monkeypatch, fake_ctx):
    monkeypatch.setattr(
        tools_doc, "_drive_import_markdown",
        AsyncMock(return_value={
            "doc_token": "doc_AAA",
            "source_file_token": "src_BBB",
            "import_ticket": "tkt_CCC",
            "url": "https://example.feishu.cn/docx/doc_AAA",
        }),
    )
    # ... fake bot_actions stub asserts target_id=doc_AAA, target_kind='docx'
```

- [ ] **Step 2: Implement** — copy 8.5's Phase -1 / 0 / 1 / 3 skeleton; in Phase 2 call `_drive_import_markdown(ctx, action_id=row.id, title=args["title"], markdown=args["markdown"])`. action_type is `'create_doc'`. Logical_key canonicalization includes title + sha256(markdown).

- [ ] **Step 3: Commit:** `feat(tools): create_doc — generic Path A markdown→docx upload`

### Task 8.7: append_to_doc (NEW v21 — block-level append with authorship gate)

> **Spec ref:** §3.12. Append a markdown snippet as new blocks at the end of
> a doc the bot owns. **Authorship gate**: refuse unless the document is
> referenced as `target_id` in some `bot_actions WHERE action_type IN
> ('create_doc','create_meeting_doc') AND target_kind='docx' AND status IN
> ('success','reconciled_unknown')`. Save the new block IDs in
> `result.appended_block_ids` plus `result.parent_block_id` so undo can
> call `docx.delete_blocks(document_id, parent_block_id, block_ids)`.

**Files:**
- Modify: `bot/agent/tools_doc.py`
- Test: `bot/tests/test_tools_doc_append_to_doc.py`

- [ ] **Step 1: Failing tests — authorship gate refusal + happy path + undo block-id capture**

- [ ] **Step 2: Implement**:
  - Phase -1: workspace + authorship gate via new `queries.is_doc_authored_by_bot(doc_token)` helper (one SELECT against `bot_actions`).
  - Phase 0/1: standard skeleton.
  - Phase 2: render markdown to docx blocks, prepend the
    `<!-- bot_action_id=<row.id> -->` marker block, list current root
    children for reconciliation context, then call
    `feishu.docx.append_blocks(document_id=doc_token,
    parent_block_id=<root>, children=blocks, client_token=row.id)`;
    capture returned `block_ids`.
  - Phase 2.5: `record_bot_action_target_pending(target_kind='docx_block_append', target_id=doc_token, result={"appended_block_ids": [...], "parent_block_id": <root>, "append_marker_block_id": <first marker block id>})`.
  - Phase 3: `mark_bot_action_success`.
- [ ] **Step 3: Commit:** `feat(tools): append_to_doc with authorship gate + block-id capture`

### Task 8.8: create_bitable_table (NEW v21 — schema-defined table inside bot's base)

> **Spec ref:** §3.13. Create a new table inside `ws.base_app_token`. Workspace
> gate: refuse any other `app_token`. Validates field type names against the
> Feishu enum (text / number / single_select / multi_select / date / user / etc.).

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_create_table.py`

- [ ] **Step 1: Failing tests — schema validation + duplicate-name idempotency**

- [ ] **Step 2: Implement**:
  - Phase 2: `feishu.bitable.create_table(app_token=ws.base_app_token, name=args["name"], fields=args["schema"])`. Capture `table_id`.
  - Phase 2.5: `record_bot_action_target_pending(target_kind='bitable_table', target_id=table_id)`.
  - Phase 3: success.
- [ ] **Step 3: Commit:** `feat(tools): create_bitable_table with workspace gate + schema validation`

### Task 8.9: append_to_my_table (NEW v21 — write rows to a bot-owned table)

> **Spec ref:** §3.14. Same workspace gate. Refuses
> `table_id == ws.action_items_table_id` and redirects the LLM to
> `append_action_items` (which has the project-disambiguation Phase -1 flow).

**Files:**
- Modify: `bot/agent/tools_bitable.py`
- Test: `bot/tests/test_tools_bitable_my_table.py` (shared file with 7.7/7.8)

- [ ] **Step 1: Failing tests — refusal for action_items_table_id, happy path with `client_token`**

- [ ] **Step 2: Implement**:
  - Phase 2: `feishu.bitable.batch_create_records(table_id=args["table_id"], records=args["records"], client_token=row.id)` with hidden `source_action_id` per record.
  - Phase 2.5: `record_bot_action_target_pending(target_kind='bitable_records', target_id=args["table_id"], result={"record_ids": [...]})`.
  - Phase 3: success.
- [ ] **Step 3: Commit:** `feat(tools): append_to_my_table — write rows to bot-owned bitable`

---

# Task Group 9: undo_last_action (1 task, but big)

> **Spec ref:** §3.9. This is the safety net (§1.4); ship together with the write tools.

### Task 9.1: undo_last_action — split into 9.1a–9.1g for granularity

> **Depends on:** Task Group 3 (DB helpers including `last_bot_action_for_sender_in_chat`), Task 4.4 + 4.5 + 4.6 (Feishu wrappers), Tasks 8.1–8.5 (the rows undo will compensate must already be writable).
> **Spec ref:** §3.9 entire section, §1.4 acceptance criteria.

#### Task 9.1a: undo_last_action shell + last_for_me sentinel handling

**Files:**
- Modify: `bot/agent/tools_meta.py` (`undo_last_action` is part of `build_meta_mcp(ctx)`)
- Test: `bot/tests/test_tools_meta_undo_last_action.py`

- [ ] **Step 1: Failing tests for sentinel paths + explicit selectors**

```python
import pytest
from unittest.mock import MagicMock
from agent.request_context import RequestContext
from agent.tools_meta import build_meta_tools
from db import queries


@pytest.fixture
def ctx():
    return RequestContext(
        message_id="undo_msg_1", chat_id="oc_1", sender_open_id="ou_asker",
    )


@pytest.fixture
def undo(ctx):
    tool_def = next(t for t in build_meta_tools(ctx) if t.name == "undo_last_action")
    return tool_def.handler


@pytest.mark.asyncio
async def test_last_is_in_flight_returns_friendly_message(undo, monkeypatch):
    monkeypatch.setattr(
        "db.queries.last_bot_action_for_sender_in_chat",
        lambda **kw: queries.LastIsInFlight,
    )
    result = await undo({"last_for_me": True})
    body = result["content"][0]["text"]
    assert "still" in body.lower() or "进行中" in body


@pytest.mark.asyncio
async def test_last_was_unreachable_returns_explicit_refusal(undo, monkeypatch):
    monkeypatch.setattr(
        "db.queries.last_bot_action_for_sender_in_chat",
        lambda **kw: queries.LastWasUnreachable,
    )
    result = await undo({"last_for_me": True})
    body = result["content"][0]["text"]
    # Spec §3.9: must NOT silently fall through to older row
    assert "无法自动撤销" in body or "请人工" in body


@pytest.mark.asyncio
async def test_no_target_returns_no_op(undo, monkeypatch):
    monkeypatch.setattr(
        "db.queries.last_bot_action_for_sender_in_chat", lambda **kw: None,
    )
    result = await undo({"last_for_me": True})
    assert "no recent action" in result["content"][0]["text"].lower() \
        or "没找到" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_target_id_kind_resolves_source_row_in_current_chat(undo, monkeypatch):
    monkeypatch.setattr(
        "db.queries.get_bot_action_by_target",
        lambda **kw: {
            "id": "u1", "chat_id": "oc_1", "action_type": "schedule_meeting",
            "status": "success", "target_id": "evt_x", "target_kind": "calendar_event",
        },
    )
    result = await undo({"target_id": "evt_x", "target_kind": "calendar_event"})
    assert "undo dispatch for schedule_meeting" in result["content"][0]["text"]
```

- [ ] **Step 3: Implement the shell**

```python
@tool(
    "undo_last_action",
    "Undo the most recent write the bot did for this asker in this chat. "
    "Inputs: one of {last_for_me: true} | {action_id: <bot_actions.id>} | "
    "{target_id, target_kind}. Compensating delete for schedule/append/doc; "
    "restore-from-snapshot for cancel.",
    {"last_for_me": bool, "action_id": str, "target_id": str, "target_kind": str},
)
async def undo_last_action(args: dict) -> dict:
    from db import queries
    # Resolve target row
    if args.get("last_for_me"):
        target = queries.last_bot_action_for_sender_in_chat(
            chat_id=ctx.chat_id, sender_open_id=ctx.sender_open_id,
        )
        if target is queries.LastIsInFlight:
            return _err("你最新一次操作还在进行中——稍等一下再说要撤销")
        if target is queries.LastWasUnreachable:
            return _err(
                "你最近一次操作我没法自动撤销，请人工检查 — "
                "我没有把更早的操作当成『刚才那个』去撤销"
            )
        if target is None:
            return _err("我没找到你最近的操作")
        source_row = target
    elif args.get("action_id"):
        source_row = queries.get_bot_action_by_id(args["action_id"])
        if not source_row:
            return _err("action_id 找不到对应行")
        # chat_id scope check
        if source_row["chat_id"] != ctx.chat_id:
            return _err("不能在另一个群里撤销其他群发起的操作")
    elif args.get("target_id") and args.get("target_kind"):
        source_row = queries.get_bot_action_by_target(
            chat_id=ctx.chat_id,
            target_id=args["target_id"],
            target_kind=args["target_kind"],
        )
        if not source_row:
            return _err("target_id/target_kind 找不到当前群可撤销的操作")
    elif args.get("target_id") or args.get("target_kind"):
        return _err("undo_last_action requires both target_id and target_kind")
    else:
        return _err("undo_last_action requires {last_for_me} or {action_id} or {target_id+target_kind}")

    # Refuse undo of an undo (spec §3.9 / row 109)
    if source_row.get("action_type") == "undo_last_action":
        return _err("不能撤销撤销")

    # Dispatch — TODO sub-tasks 9.1b+
    return _err(f"undo dispatch for {source_row['action_type']} not yet implemented")
```

Add DB selectors to `db/queries.py`:
- `get_bot_action_by_id(uuid)` — one-liner SELECT by primary key.
- `get_bot_action_by_target(chat_id, target_id, target_kind)` — SELECT the newest row
  scoped to the current `chat_id`, with `status IN ('success','reconciled_unknown')`,
  `action_type != 'undo_last_action'`, and exact `target_id`/`target_kind`.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(tools): undo_last_action shell with last_for_me sentinel handling"
```

#### Task 9.1b: undo dispatch for schedule_meeting + restore_schedule_meeting (Case B)

- [ ] **Failing tests**: include 404-as-success per spec row 114.

```python
@pytest.mark.asyncio
async def test_undo_schedule_deletes_event_and_marks_undone(undo, monkeypatch):
    monkeypatch.setattr(
        "db.queries.last_bot_action_for_sender_in_chat",
        lambda **kw: {
            "id": "u-1", "action_type": "schedule_meeting", "status": "success",
            "target_id": "evt_x", "target_kind": "calendar_event",
            "result": {"calendar_id": "cal_bot", "attendees": ["ou_a"]},
            "chat_id": ctx.chat_id, "sender_open_id": ctx.sender_open_id,
        },
    )
    captured = {}

    async def fake_delete(*, calendar_id, event_id):
        captured["delete"] = (calendar_id, event_id)
    monkeypatch.setattr("feishu.calendar.delete_event", fake_delete)
    monkeypatch.setattr(
        "db.queries.retire_source_action",
        lambda action_id: captured.setdefault("undone", action_id),
    )

    result = await undo({"last_for_me": True})
    assert captured["delete"] == ("cal_bot", "evt_x")
    assert captured["undone"] == "u-1"


@pytest.mark.asyncio
async def test_undo_schedule_404_treated_as_success(undo, monkeypatch):
    """Spec §3.9 + row 114: delete returning 404/EventNotFound → success path."""
    monkeypatch.setattr(
        "db.queries.last_bot_action_for_sender_in_chat",
        lambda **kw: {
            "id": "u-1", "action_type": "schedule_meeting", "status": "success",
            "target_id": "evt_gone", "target_kind": "calendar_event",
            "result": {"calendar_id": "cal_bot"},
            "chat_id": ctx.chat_id, "sender_open_id": ctx.sender_open_id,
        },
    )
    from feishu.calendar import EventNotFound

    async def fake_delete(*, calendar_id, event_id):
        raise EventNotFound(event_id)
    monkeypatch.setattr("feishu.calendar.delete_event", fake_delete)

    captured = {}
    monkeypatch.setattr(
        "db.queries.retire_source_action",
        lambda action_id: captured.setdefault("undone", action_id),
    )
    result = await undo({"last_for_me": True})
    assert captured["undone"] == "u-1"  # marked undone despite 404
    assert "isError" not in result or result.get("isError") is not True
```

- [ ] **Implementation**:

```python
    # In the dispatch chain
    if source_row["action_type"] in ("schedule_meeting", "restore_schedule_meeting"):
        # If undoing a restore_schedule_meeting that's still partial_success,
        # route to Case A handling — Task 9.1d.
        # **For 9.1b only**: stub `_undo_restore_partial` as a NotImplemented
        # raise so 9.1b's tests pass (they don't exercise the partial path).
        # 9.1d will replace the stub with the real implementation.
        if source_row.get("status") == "reconciled_unknown" and source_row["action_type"] == "restore_schedule_meeting":
            return await _undo_restore_partial(source_row)  # 9.1d
        from feishu.calendar import EventNotFound, delete_event
        cal_id = (source_row.get("result") or {}).get("calendar_id")
        event_id = source_row["target_id"]
        try:
            await delete_event(calendar_id=cal_id, event_id=event_id)
        except EventNotFound:
            pass  # 404 = success, the desired end state already holds
        queries.retire_source_action(source_row["id"])
        queries.write_undo_audit(
            action_id=source_row["id"],
            ctx=ctx,
            outcome="deleted",
        )
        return _ok({"outcome": "deleted", "target_id": event_id})
```

Add `write_undo_audit` to `db/queries.py` — inserts an `undo_last_action` audit row with `target_id=<original action_id>`, `target_kind='bot_action_undo'`, `result={"outcome": ...}`.

- [ ] **Commit**: `feat(tools): undo schedule_meeting + restore (success path) with 404-as-success`

#### Task 9.1c: undo dispatch for cancel_meeting — probe-then-restore (R0–R4)

This is the most complex undo path. Reference spec §3.9 cancel_meeting branch verbatim — that section has full pseudocode for R0-R4. The plan task is to translate it 1:1.

- [ ] **Failing tests**:
  1. probe returns 200 (event still there) → no restore, just retire cancel row
  2. probe returns 404 → R0..R4 happy path
  3. R3 invite fails after R2 persisted → restore row becomes `partial_success`

- [ ] **Implementation**: implement `_undo_cancel_with_restore(source_row)` per §3.9 cancel branch. The implementer must:
  1. Probe `calendar.get_event` — if event exists, transition cancel row directly to `undone` (no actual delete happened) and return.
  2. R0: idempotent UPDATE original schedule row's status → `undone` (predicate per spec row 110).
  3. R1: build a whitelisted CalendarEvent body (summary/description/start_time/end_time/visibility/attendee_ability/reminders/location/color — explicitly NOT event_id/organizer_calendar_id/status/create_time/app_link/attendees) — see spec row 104.
  4. R2: `insert_bot_action_pending` with `action_type='restore_schedule_meeting'` + log new event_id to stderr before the UPDATE per spec row 88.
  5. R3: invite via `attendee.create` — failure → `mark_bot_action_reconciled_unknown(kind='partial_success')` per spec row 105.
  6. R4: mark restore row + cancel row in lockstep.

- [ ] **Commit**: `feat(tools): undo cancel_meeting with probe-then-restore R0..R4`

#### Task 9.1d: undo restore_schedule_meeting partial recovery (Case A)

- [ ] **Failing tests** for the three Case A outcomes per spec §3.9 + row 108:
  - probe attendees match `result.attendees` → mark restore row `success`
  - probe shows missing attendees → retry invite; on retry-success mark `success`; on retry-failure return "要不要删？" message
  - probe returns 404 → mark `undone`

- [ ] **Implementation**: implement `_undo_restore_partial(source_row)`.

- [ ] **Commit**.

#### Task 9.1e: undo dispatch for append_action_items + 404-per-record idempotency

- [ ] **Failing tests**:
  - happy: all `record_ids` present → `batch_delete` all
  - per-record 404: some records already gone → query by `source_action_id`, delete only surviving ones, mark `undone` only when 0 remain
  - all already deleted → mark `undone` immediately, no batch_delete call

- [ ] **Implementation** per spec row 114 + row 125.

- [ ] **Commit**.

#### Task 9.1f: undo dispatch for create_meeting_doc — dispatch by target_kind

- [ ] **Failing tests** for the 4 target_kind shapes per spec §3.9:
  - `target_kind='docx'` → delete docx + best-effort delete `.md`; `.md`-delete-404 OK
  - `target_kind='file'` → delete `.md`; 404 OK
  - `target_kind=NULL with import_ticket` → re-poll, then dispatch on poll outcome
  - `target_kind=NULL with source_file_token only` → just delete `.md` (iter-13 window)

- [ ] **Implementation** per spec §3.9 docx branch.

- [ ] **Commit**.

#### Task 9.1g: undo dispatch for the 4 new v21 write tools

> **Spec ref:** §3.9 v21 dispatch table additions. Each new write-tool
> action_type gets its own dispatch arm, mirroring the existing pattern
> (404-as-success idempotency, no fences).

- [ ] **Failing tests** — one test per action_type, asserting target_kind dispatch and 404-as-success:
  - `action_type='create_doc', target_kind='docx'` → `drive.delete_file(token=target_id, type='docx')` + `.md` cleanup; both 404-tolerant.
  - `action_type='append_to_doc', target_kind='docx_block_append'` → `docx.delete_blocks(document_id=target_id, parent_block_id=result.parent_block_id, block_ids=result.appended_block_ids)`; missing stored block IDs → success/already deleted. **Does NOT delete the doc itself** — only the blocks this action appended.
  - `action_type='create_bitable_table', target_kind='bitable_table'` → `bitable.delete_table(app_token=ws.base_app_token, table_id=target_id)`; 404 OK.
  - `action_type='append_to_my_table', target_kind='bitable_records'` → `bitable.batch_delete_records(app_token=ws.base_app_token, table_id=target_id, record_ids=result.record_ids)`; per-record 404 tolerated and reported as `{partial: true, deleted: N, missing: M}`.

- [ ] **Implementation**: extend the dispatcher in `tools_meta.undo_last_action` (Task 9.1a's match-statement) with these four new arms. Each arm transitions the source row `success → undone` via `retire_source_action(row.id)` and writes a trace-only audit row with `action_type='undo_last_action'`, `target_id=<original action_id>`, `target_kind='bot_action_undo'`, and `result.source_action_type=<source action_type>`. Do NOT create `undo_create_doc` / `undo_append_to_doc` action types — those would pass the undoable predicate and reintroduce undo-of-undo.

- [ ] **Commit**: `feat(tools): undo dispatch for create_doc / append_to_doc / create_bitable_table / append_to_my_table`

---

# Task Group 10: Wire into agent runner (1 task)

> Task 6.3 registers all 5 MCP servers but intentionally keeps
> `allowed_tools` limited to the existing read-only meta tools. This
> task performs the full v21 whitelist rewrite only after Tasks 7–9
> have replaced the `_not_ready` placeholders with real tools.

### Task 10.1: Expand allowed_tools + replace SYSTEM_PROMPT tool inventory

**Files:**
- Modify: `bot/agent/runner.py` (`allowed_tools` + `SYSTEM_PROMPT` constant)

Per spec §9 + §10 row 122 (v21 expansion):

1. Replace the `allowed_tools` list (Task 6.3 left it read-only) with
   the full domain-grouped v21 list from spec §11 step 6.
2. Replace the tool inventory list (around `runner.py:84`) to include all
   18 tools, grouped by domain:
   - **Meta**: today_iso, list_users, lookup_user, get_recent_turns, get_project_overview, get_activity_stats, generate_image, resolve_people, undo_last_action
   - **Calendar**: schedule_meeting, cancel_meeting, list_my_meetings
   - **Bitable**: append_action_items, query_action_items, create_bitable_table, append_to_my_table, query_my_table, describe_my_table
   - **Doc**: create_meeting_doc, create_doc, append_to_doc
   - **External (read-any)**: read_doc, read_external_table, resolve_feishu_link
3. Remove the "这是只读问答助手" / "你不能：写代码、改文件、跑命令" lines.
4. Append the v21 directive block (extends §9 with the new tools):
   ```
   你现在可以在飞书做事，不只是回答问题。

   默认行为：用文字回复。只有在用户意图明确指向某个写工具时才调用：
   订会/取消会议/看日程 → calendar 工具；记一下/写到表里 → action_items / append_to_my_table；
   写成文档 → create_meeting_doc / create_doc / append_to_doc；
   建表 → create_bitable_table；
   读他人/外部资源 → read_doc / read_external_table / resolve_feishu_link.

   硬规则：
   - 调用任何接受人员参数的工具前必须先调 resolve_people。如果它返回 ambiguous 或 unresolved，必须先反问用户澄清。绝不要猜。
   - 传给 schedule_meeting 的所有时间必须是 RFC3339 with timezone。先调 today_iso 拿到提问者所在时区。
   - schedule_meeting 返回 conflict 时，把冲突告诉用户并提议替代时间，不要盲目重试。
   - 不要修改不是你创建的飞书资源。只能取消/编辑你自己 bot_actions 中的事件、文档、表。
   - append_to_doc 仅作用于由 bot 自己创建的文档（authorship gate）；不要尝试 append 到用户分享给你的链接。
   - read_external_table 每小时每会话最多 5 次。命中限制就告诉用户改用文字描述。
   - 用户粘贴飞书 URL 时先调 resolve_feishu_link 拿到 {kind, token}，再决定调 read_doc 还是 read_external_table。
   - list_my_meetings 返回非空 visibility_note 或 user_calendar_events 看起来稀疏时，把这个不确定性告诉用户；绝不在没承认可见性限制的情况下断言"你没有会"。
   - 第一人称日历问题（"我下午有啥会" / "我下周三有空吗"），调用 list_my_meetings 时不传 target — 工具默认返回 asker。绝不为了拿 asker 的 open_id 而调 resolve_people。
   ```

- [ ] Commit: `feat(agent): replace SYSTEM_PROMPT tool inventory for v21 — 18 tools across 5 MCP servers`

---

# Task Group 11: Smoke tests against real Feishu (1 task, ~1 hour manual)

> **Spec ref:** §11 step 9. Mandatory before declaring done.

### Task 11.1: End-to-end smoke in private Feishu group

**Files:** None (manual)

- [ ] **Step 1: Start the bot locally pointing at dev Supabase + dev Feishu app**

```bash
cd bot && uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: In a private group, run each scenario from spec §11 step 9**

Each scenario is a checkbox item — verify in Feishu UI + DB:

- [ ] Schedule a meeting with two attendees → confirm event in Feishu Calendar UI + `bot_actions` row with `status='success'` + meeting visible to both attendees with `attendee_ability=can_modify_event`.
- [ ] Append 3 action items linked to the event above → confirm rows in `action_items` table, owners populated, project resolved.
- [ ] Ambiguous append: ask "记一下要发邮件" with no project hint and confirm `needs_input: "project"` returned + no `bot_actions` row + no Bitable rows. Provide a project, retry, confirm rows appear.
- [ ] Create a meeting-notes doc → confirm Docx in 文档柜 + link works.
- [ ] **Undo each via `undo_last_action`** → confirm Feishu artifacts deleted + `bot_actions` rows transitioned to `undone` + fresh `undo_last_action` audit rows.
- [ ] **Cancel-then-undo restore**: schedule, cancel, undo → confirm new event with same title/time/attendees + different event_id + agent reply mentions `restore_caveats`.
- [ ] Logical_key dedup, single-process: same scheduling request twice within 60s → confirm second returns `deduplicated_from_logical_key: true` + no second meeting in Feishu.
- [ ] Logical_key window expiry: schedule, wait >60s, repeat → confirm second creates a fresh meeting.
- [ ] Group chat: user A schedules, user B says "取消刚才那个会" → bot refuses.
- [ ] Bootstrap recovery: manually delete the bot's Bitable base, then issue a write request → bot self-heals + posts warning.
- [ ] **§1.4 acceptance: undo across LRU eviction**: schedule a meeting, wait long enough that the original `message_id` falls out of `feishu/events.py:_seen_events` LRU (~5 min), then say "撤销刚才那个" — confirm undo still resolves via `chat_id+sender_open_id` scoping (NOT message_id) and successfully deletes the event.
- [ ] **Spec §11 step 9 cross-process logical-key dedup**: from a shell, fire two concurrent `curl` POSTs to `/feishu/webhook` carrying the **same chat_id, sender_open_id, and tool args** but **different `message_id`s** (simulate webhook retries from two upstream replicas). Confirm:
  - Exactly ONE meeting is created on Feishu (not two).
  - Exactly ONE `bot_actions` row exists for the locked `logical_key`; the loser request hit the DB partial-UNIQUE before INSERT and therefore has no loser row.
  - The loser response/log shows the logical-key conflict outcome (either `deduplicated_from_logical_key=True` if the winner had committed by replay time, or an "in flight" refusal if the winner was still pending).
  - This proves the DB partial UNIQUE — `bot_actions_logical_locked_uniq` — is the load-bearing cross-process exclusion (spec §5.2 + §6.2).
  - Sample shell snippet:
    ```bash
    PAYLOAD='{"header":{"event_id":"e1","event_type":"im.message.receive_v1"},"event":{"sender":{"sender_id":{"open_id":"ou_test"}},"message":{"chat_id":"oc_test","chat_type":"p2p","message_id":"MSG_A","message_type":"text","content":"{\"text\":\"@bot 帮我和 ou_a 订下周三 3 点的会\"}","mentions":[]}}}'
    P2=$(echo "$PAYLOAD" | sed 's/MSG_A/MSG_B/; s/e1/e2/')
    curl -s -X POST localhost:8000/feishu/webhook -d "$PAYLOAD" &
    curl -s -X POST localhost:8000/feishu/webhook -d "$P2" &
    wait
    psql "$SUPABASE_DB_URL" -c \
      "SELECT id, message_id, status, logical_key,
              result->>'deduplicated_from_logical_key' AS dedup
       FROM bot_actions
       WHERE message_id IN ('MSG_A','MSG_B')
       ORDER BY created_at;"
    ```
  - Expected: one row only, `status='success'`, with the shared `logical_key`.
    Validate the second request's HTTP body or app logs for the replay/refusal outcome;
    do not expect a second `bot_actions` row.
- [ ] **§3.9 + iter-20 row 114: 404-as-success undo replay**: schedule a meeting, manually delete the event in the Feishu UI (simulating "delete succeeded but DB UPDATE crashed mid-flight"), then ask the bot to undo — confirm the bot treats `EventNotFound` as the desired end state, marks the source row `undone`, and writes a normal undo audit row (not a failure).
- [ ] **iter-21 doc cleanup non-404 retryability**: schedule a doc that produces a docx + source `.md` in 文档柜. Manually revoke `drive:drive` scope on the bot in Feishu admin (or simulate with auth-error injection). Ask the bot to undo — confirm undo treats the cleanup failure as retryable, leaves the source row's status unchanged, and on a re-undo (after restoring scope) completes successfully.

#### v21 additions (spec §11 step 9 v21)

- [ ] **v21 / §3.10 create_doc**: ask the bot to "起草一份关于 X 的笔记，保存到文档柜". Confirm a Docx is created in the bot's docs folder and a `bot_actions` row exists with `action_type='create_doc', target_kind='docx'`. The doc title and body match what the LLM produced.
- [ ] **v21 / §3.12 append_to_doc authorship gate**: ask the bot to "在我刚才那篇笔记后面追加一段进度". Confirm new blocks appear at the end of the same doc; `bot_actions` row has `target_kind='docx_block_append'`, `result.appended_block_ids` is non-empty, and `result.parent_block_id` is set. Then paste a Feishu doc URL the bot did NOT create and ask for an append — confirm refusal with an authorship-gate message.
- [ ] **v21 / §3.12 undo append_to_doc deletes only the new blocks**: undo the append above; confirm the appended blocks vanish but the original doc body is intact, and the source `create_doc` row remains `success` (NOT `undone`).
- [ ] **v21 / §3.13 create_bitable_table**: ask the bot to "建一张多维表，叫『风险登记』，字段：标题/责任人/状态". Confirm new table appears in the bot's base; `bot_actions` row has `target_kind='bitable_table'`.
- [ ] **v21 / §3.14 append_to_my_table redirects action_items**: ask the bot to write a row into the `action_items` table directly via `append_to_my_table` — confirm tool refuses and the LLM (per system prompt) falls back to `append_action_items`.
- [ ] **v21 / §3.15 query_my_table workspace gate**: ask the bot to query a table in some external base (paste an unrelated `app_token` via the args). Confirm refusal with workspace-gate message.
- [ ] **v21 / §3.11 read_doc happy path**: share a docx with the bot, ask "把这篇文档总结成 3 点" → confirm the bot calls `read_doc` and returns a summary based on actual content (markdown rendering preserves headings + lists).
- [ ] **v21 / §3.11 read_doc 20000-character truncation**: paste a very long doc (>20000 rendered chars) → confirm `truncated=true` in tool result and the bot's reply mentions it only saw the returned prefix / asks for a narrower section if needed.
- [ ] **v21 / §3.17 read_external_table rate limit**: in one chat, ask the bot 6 times in quick succession to read different external bitables → confirm the 6th call returns the rate-limit error and the bot apologizes + suggests text description.
- [ ] **v21 / §3.18 resolve_feishu_link wiki redirect**: paste a `/wiki/<token>` URL whose underlying object is a docx → confirm the bot internally calls `resolve_feishu_link` (visible in tool trace), gets back `{kind: 'docx', token: '...', via_wiki: '...'}`, then proceeds to `read_doc`.

- [ ] **Step 3: Document failures in `docs/specs/2026-05-02-pmo-bot-write-tools-design.md` §10 omissions table for follow-up**

- [ ] **Step 4: Commit smoke-test notes**

```bash
git add docs/specs/
git commit -m "test(smoke): record results of v1 write-tools E2E smoke run"
```

---

## Done Criteria

- All migrations applied to production Supabase.
- `bootstrap_bot_workspace.py` run once on production tenant.
- Spec §11 step 9 smoke test passes for every scenario.
- All `pytest` tests green: `cd bot && pytest tests/ -v` returns 0.
- The bot still answers read-only questions correctly (regression check).

## Reference: Task count summary

| Group | Tasks | Estimate |
|---|---|---|
| 0. Test infra | 1 | 30 min |
| 1. Scope verification | 1 | 30 min (manual + admin console) |
| 2. Schema migrations | 4 | 1 hour |
| 3. db/queries.py | 10 | 4-5 hours |
| 4. Feishu wrappers | 10 | 6-7 hours (v21: docx/wiki/links restored + 2 new) |
| 5. Bootstrap script | 1 | 1 hour |
| 6. RequestContext refactor + MCP module split | 5 | 3 hours (v21: +rename, +4 module skeletons, +app.py prefix) |
| 7. Read tools | 8 | 4-5 hours (v21: +5 — resolve_feishu_link, read_doc, read_external_table, describe_my_table, query_my_table) |
| 8. Write tools | 9 | 9-12 hours (v21: +4 — create_doc, append_to_doc, create_bitable_table, append_to_my_table) |
| 9. undo_last_action | 1 | 5 hours (split into 9.1a–9.1g; v21 added 9.1g for 4 new write-tool arms) |
| 10. Runner wiring | 1 | 30 min (v21: 10.1 absorbed into 6.3) |
| 11. Smoke test | 1 | 2-3 hours (v21: +10 new scenarios) |
| **Total** | **52** (8.1 split into 7 sub-tasks; 9.1 split into 7 sub-tasks; 4.7 restored, +4.8 wiki, +4.9 links; +6.2 skeletons, +6.4 app.py, +6.5 verify; +5 read tools; +4 write tools; +9.1g undo arms) | **~45 hours** |

Spread over 5-7 working days for one developer.

> **v21 changelog vs plan v4** (for Codex review):
> - File structure table: 5 MCP module files instead of 1 tools.py; 8 new tool tests; 3 new feishu wrappers (docx/wiki/links).
> - Task Group 4: 8 → 10 tasks (4.7 docx restored, 4.8 wiki added, 4.9 links added).
> - Task Group 6: 3 → 5 tasks; the original 6.1 swallowed the file rename, 6.2 adds 4 module skeletons, 6.4 adds app.py prefix-strip update, 6.5 verifies the atomic transaction.
> - Task Group 7: 3 → 8 tasks (resolve_feishu_link, read_doc, read_external_table, describe_my_table, query_my_table added).
> - Task Group 8: 5 → 9 tasks (create_doc, append_to_doc, create_bitable_table, append_to_my_table added). Path A 3-step factored into shared `_drive_import_markdown` helper.
> - Task Group 9: 9.1f → 9.1g for the 4 new write-tool undo arms.
> - Task Group 10: collapsed to 1 task (allowed_tools moved into 6.3).
> - Task Group 11: +10 v21 smoke scenarios.
