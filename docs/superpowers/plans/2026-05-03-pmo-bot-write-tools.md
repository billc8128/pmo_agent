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

| Path | Purpose |
|---|---|
| `backend/supabase/migrations/0010_bot_workspace.sql` | Single-row config table for bot's calendar/base/folder ids |
| `backend/supabase/migrations/0011_bot_actions.sql` | Idempotency + audit + lock table |
| `bot/scripts/__init__.py` | (empty) |
| `bot/scripts/bootstrap_bot_workspace.py` | One-shot script to create bot's calendar / Bitable / Docs folder |
| `bot/agent/request_context.py` | `RequestContext` dataclass — per-pooled-client mutable scope |
| `bot/feishu/calendar.py` | Calendar SDK wrappers (freebusy, event create/get/delete, attendee invite, primarys) |
| `bot/feishu/bitable.py` | Bitable SDK wrappers (app create/get, table create, record batch_create/batch_delete/search) |
| `bot/feishu/drive.py` | Drive SDK wrappers (file upload_all/delete/create_folder, import_task create/get) |
| `bot/feishu/docx.py` | Docx SDK wrappers (Path B fallback only — `document.create`, `document_block_children.create`) |
| `bot/feishu/contact.py` | Contact: `user.get` (timezone), `batch_get_id` (email/phone), and **raw httpx** wrapper for `/open-apis/search/v1/user` |
| `bot/feishu/auth.py` | Shared `tenant_access_token` issuer extracted from `feishu/client.py:67` (factored out to support contact search) |
| `bot/agent/canonical_args.py` | `compute_logical_key` + canonicalization helpers (sorted-keys JSON, UTC time normalization) |
| `bot/tests/__init__.py` | (empty) |
| `bot/tests/conftest.py` | pytest fixtures: in-memory bot_actions stub, fake `RequestContext`, time-freeze |
| `bot/tests/test_canonical_args.py` | logical_key hashing tests |
| `bot/tests/test_queries_bot_actions.py` | DB helper tests (insert / mark_failed / mark_undone / GC / unique-constraint dispatch) |
| `bot/tests/test_request_context.py` | RequestContext closure-capture sanity test |
| `bot/tests/test_tools_resolve_people.py` | resolve_people tests (3-tier resolution, error handling) |
| `bot/tests/test_tools_today_iso.py` | today_iso extension test (timezone fetch) |
| `bot/tests/test_tools_schedule_meeting.py` | schedule_meeting Phase -1 / 0 / 1 / 2 / 3 tests including conflict + partial-success |
| `bot/tests/test_tools_cancel_meeting.py` | cancel_meeting tests (last:true + explicit event_id, status gates, source_meeting_action_id) |
| `bot/tests/test_tools_list_my_meetings.py` | list_my_meetings tests (self default, primarys lookup, dual result sets) |
| `bot/tests/test_tools_append_action_items.py` | append tests (ambiguous flow, target persistence, ambiguous post-send failure) |
| `bot/tests/test_tools_query_action_items.py` | query tests |
| `bot/tests/test_tools_create_meeting_doc.py` | doc tests (Path A 3-step, partial paths, undo cleanup) |
| `bot/tests/test_tools_undo_last_action.py` | undo tests (per dispatch type, 404-as-success, last_for_me sentinels) |

### Modified files

| Path | Change |
|---|---|
| `bot/requirements.txt` | Add `pytest`, `pytest-asyncio`, `respx`, `freezegun` |
| `bot/feishu/client.py` | Replace inline `tenant_access_token` POST in `fetch_self_info` with `feishu/auth.py:get_tenant_access_token()` (no functional change) |
| `bot/db/queries.py` | Add `bot_workspace` + `bot_actions` helpers (~12 new functions) |
| `bot/agent/tools.py` | Convert `build_pmo_mcp` to factory `build_pmo_mcp(ctx)`, add 8 new tools as inner functions, extend `today_iso` |
| `bot/agent/runner.py` | Add `RequestContext` per `_PooledClient`, `answer*` accept `message_id`/`chat_id`/`sender_open_id` kwargs, expand `allowed_tools`, replace `SYSTEM_PROMPT` tool inventory |
| `bot/agent/imaging.py` | (no change to signature; caller in tools.py changes how it's called) |
| `bot/app.py` | `_handle_message` calls `answer_streaming(...)` with new kwargs |
| `bot/README.md` | Document the 10 Feishu scopes that need to be enabled |

### Removed (pure deletions)

| Path / lines | Why |
|---|---|
| `bot/agent/tools.py:29-31` (`_current_conversation_key_var` + `set_current_conversation`) | Replaced by `RequestContext` closure |
| `bot/agent/runner.py:244` (`agent_tools.set_current_conversation(conversation_key)`) | Same |

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

# Task Group 3: db/queries.py — bot_actions helpers (10 tasks)

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
    """Return args with stable key order and normalized values.

    For schedule_meeting, start_time is normalized to UTC so equivalent
    +08:00 / +00:00 representations collide on the same logical_key.
    """
    out = dict(args)
    if action_type == "schedule_meeting" and "start_time" in out:
        try:
            dt = datetime.fromisoformat(out["start_time"])
            if dt.tzinfo is not None:
                out["start_time"] = dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            # leave as-is; downstream validation will catch malformed input
            pass
    # json.dumps with sort_keys gives a deterministic byte sequence
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
            # else: lost race — fall through to return current row state

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
    payload: dict[str, Any] = {"status": "success"}
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
        }).eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None


def mark_bot_action_undone(action_id: str) -> dict[str, Any] | None:
    res = (
        sb_admin().table("bot_actions").update({
            "status": "undone", "logical_key_locked": False,
        }).eq("id", action_id).eq("status", "pending").execute()
    )
    return res.data[0] if res.data else None
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(db): mark_bot_action_success/failed/undone with transition guards"
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
    fake_admin.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{
        "id": "u1", "status": "pending", "attempt_count": 2,
        "logical_key_locked": True,
    }]
    row = queries.update_for_retry("u1", new_args={"x": 1}, logical_key="lk1")
    assert row["status"] == "pending"
    assert row["attempt_count"] == 2


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

# Task Group 4: Feishu auth + SDK wrappers (8 tasks)

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

- [ ] **Step 1: Write failing test for create_event with idempotency_key**

```python
"""Test calendar SDK wrappers — assert builder paths and required args."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from feishu import calendar


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
    CalendarEvent,
)


def _lark_client() -> lark.Client:
    from feishu.client import feishu_client
    return feishu_client.client


async def create_calendar(*, summary: str) -> str:
    """Bootstrap: create the bot's primary calendar. Returns calendar_id."""
    body = CalendarEvent.builder().summary(summary).build()  # CalendarBody is similar
    # Actually CreateCalendarRequest takes a Calendar object; check installed model
    # signature — spec §4 says simply calendar.create with summary.
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
    inspect lark_oapi.api.calendar.v4.model.TimeInfo for exact field
    layout — `timestamp` (epoch seconds) + `time_zone` is the most
    common shape.
    """
    from datetime import datetime
    from lark_oapi.api.calendar.v4.model import TimeInfo
    dt = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    return (
        TimeInfo.builder()
        .timestamp(str(int(dt.timestamp())))
        .time_zone(time_zone or str(dt.tzinfo))
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

### Task 4.7: feishu/docx.py wrappers (Path B fallback only)

**Files:**
- Create: `bot/feishu/docx.py`
- Test: `bot/tests/test_feishu_docx.py`

Path B is documented in spec §3.8 as fallback; ship it because Path A might not have permissions in production at first.

Wraps:
- `docx.v1.document.create`
- `docx.v1.document_block_children.create`

Commit:
```bash
git commit -am "feat(feishu): docx v1 wrappers (document.create + document_block_children.create) for Path B fallback"
```

### Task 4.8: Verify SDK call shapes against installed lark-oapi

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
    'lark_oapi.api.drive.v1.model.upload_all_file_request',
    'lark_oapi.api.drive.v1.model.import_task',
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

# Task Group 6: RequestContext refactor (3 tasks)

> **Spec ref:** §5.0, §11 step 5. Pure refactor — no behavior change.

### Task 6.1: Define RequestContext + tools.py factory pattern

**Files:**
- Create: `bot/agent/request_context.py`
- Modify: `bot/agent/tools.py` (rewrite `build_pmo_mcp` to take `ctx`; remove `_current_conversation_key_var`)
- Test: `bot/tests/test_request_context.py`

- [ ] **Step 1: Write failing test for closure capture**

```python
"""Verify that build_pmo_mcp(ctx) tools see the latest ctx mutation."""
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

- [ ] **Step 5: Refactor `tools.py` — convert `build_pmo_mcp` to factory**

Replace `def build_pmo_mcp()` with:
```python
def build_pmo_mcp(ctx: RequestContext):
    """Factory: returns an MCP server whose tool implementations close
    over `ctx`. Called once per _PooledClient. See spec §5.0.
    """
    # Wrap every existing @tool with a closure that reads ctx.* directly
    # instead of the old _current_conversation_key_var.
    @tool("today_iso", ..., {})
    async def today_iso(args: dict) -> dict:
        # ... existing body, unchanged
        ...

    # ... all 7 existing read tools wrapped similarly

    return create_sdk_mcp_server(
        name="pmo", version="0.1.0",
        tools=[today_iso, list_users, lookup_user, get_recent_turns,
               get_project_overview, get_activity_stats, generate_image],
    )
```

Remove the module-global `_current_conversation_key_var` and `set_current_conversation` function.

For `generate_image`, replace `_current_conversation_key_var` reads with `ctx.conversation_key`.

- [ ] **Step 6: Run all existing tests + new test**

```bash
cd bot && pytest tests/ -v
```

Expected: all pass; no behavioral change since runner.py still calls `build_pmo_mcp()` with no arg.

We need to fix that next, in Task 6.2. For now this commit will leave runner.py temporarily broken; do it as one transaction:

- [ ] **Step 7: NO COMMIT YET — proceed to Task 6.2**

### Task 6.2: Wire RequestContext through runner.py

**Files:**
- Modify: `bot/agent/runner.py`

- [ ] **Step 1: Add ctx field to _PooledClient**

```python
@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    ctx: RequestContext = field(default_factory=RequestContext)
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
```

Add `from agent.request_context import RequestContext`.

- [ ] **Step 2: Update _get_client to create ctx + pass to factory**

```python
async def _get_client(conversation_key: str) -> _PooledClient:
    async with _pool_lock:
        slot = _pool.get(conversation_key)
        if slot is None:
            ctx = RequestContext()
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                allowed_tools=[...],  # unchanged for now
                mcp_servers={"pmo": build_pmo_mcp(ctx)},  # ← pass ctx
                disallowed_tools=[...],
                max_turns=settings.agent_max_duration_seconds,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            slot = _PooledClient(client=client, ctx=ctx)
            _pool[conversation_key] = slot
        slot.last_used = time.monotonic()
        return slot
```

- [ ] **Step 3: Update answer_streaming signature + ctx mutation**

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

- [ ] **Step 4: Update answer() signature + delegate**

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

- [ ] **Step 5: NO COMMIT — proceed to 6.3**

### Task 6.3: Update app.py call sites

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

- [ ] **Step 3: Run all tests**

```bash
cd bot && pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Manual smoke (read-only flow still works)**

Send a question to the bot in Feishu (e.g., "@包工头 albert 昨天做了啥").
Expected: existing read-only behavior unchanged.

- [ ] **Step 5: Commit (the whole 6.1+6.2+6.3 transaction)**

```bash
git add bot/agent/request_context.py bot/agent/tools.py bot/agent/runner.py bot/app.py bot/tests/test_request_context.py
git commit -m "refactor(agent): RequestContext closure replaces module global; runner threads message_id/chat_id/sender_open_id"
```

---

# Task Group 7: Read tools (3 tasks)

> **Spec ref:** §3.1, §3.2 (extension), §3.7. These are simpler than write tools — no Phase 2.X.5, no idempotency.

### Task 7.1: today_iso extension (timezone field)

**Files:**
- Modify: `bot/agent/tools.py` (the `today_iso` inner function)
- Test: `bot/tests/test_tools_today_iso.py`

- [ ] TDD per spec §3.2: tool calls `feishu.contact.get_user(open_id=ctx.sender_open_id)` and adds `user_timezone` + `user_today_local` fields.

- [ ] Commit: `feat(tools): today_iso returns user_timezone via contact.user.get`

### Task 7.2: resolve_people (3-tier resolution)

**Files:**
- Modify: `bot/agent/tools.py` (new tool)
- Test: `bot/tests/test_tools_resolve_people.py`

- [ ] Implement per spec §3.1:
  - Step 1: query `profiles` + `feishu_links` (existing `db.queries.lookup_by_feishu_open_id` is for the reverse direction; add new `lookup_handle_or_email` query if needed)
  - Step 2: input shape regex (email / phone) → `feishu.contact.batch_get_id_by_email_or_phone`
  - Step 3: name → `feishu.contact.search_users`
- [ ] Returns `{resolved, ambiguous, unresolved}` shape per spec.
- [ ] Tool description directive about ambiguous handling.
- [ ] Commit.

### Task 7.3: query_action_items

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_query_action_items.py`

- [ ] Reads from the bot's Bitable `action_items` table via `feishu.bitable.search_records(table_id=ws.action_items_table_id, ...)` with optional filters (owner / project / status / since / until).
- [ ] Pass `user_id_type="open_id"`.
- [ ] Commit.

---

# Task Group 8: Write tools (5 tasks, one per tool)

> Each task follows the spec §3.X for inputs / phases / failure handling, and the §5.1 skeleton for the three-phase pattern.

### Task 8.1: schedule_meeting

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_schedule_meeting.py`

Phases per spec §3.3:
- Phase -1: validate args (RFC3339, ≥ 1 attendee)
- Phase 0: `get_locked_by_logical_key` dedup
- Phase 1: `insert_bot_action_pending` with constraint dispatch
- Phase 2.0: read `bot_workspace.calendar_id` + compute `effective_attendees` (auto-add asker)
- Phase 2.1: `freebusy_batch` — conflict → mark `success` with `result.outcome='conflict'`
- Phase 2.2: `create_event` with `idempotency_key=schedule_meeting:<action_id>`
- Phase 2.2.5: persist `target_id=event_id`, `target_kind='calendar_event'`
- Phase 2.3: `invite_attendees` — failure → `mark_reconciled_unknown(kind='partial_success')`
- Phase 3: `mark_bot_action_success` with `result.attendees=effective_attendees` and `result.link`

- [ ] Tests cover: happy path, freebusy conflict, partial success (attendee invite fails), MessageActionConflict (return cached), LogicalKeyConflict (return deduplicated), needs more arg (Phase -1 fail).
- [ ] Commit: `feat(tools): schedule_meeting with idempotency_key and partial-success handling`

### Task 8.2: cancel_meeting

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_cancel_meeting.py`

Phases per spec §3.4:
- Resolution rules: `event_id` (status-gated, action_type IN schedule|restore) OR `last:true` (newest-row guard via `last_bot_action_for_sender_in_chat`, with already-cancelled idempotency check)
- Phase -1: extract `calendar_id` and `source_meeting_action_id` from source row
- Phase 1: pending insert
- Phase 2a: `get_event(need_attendee=True, user_id_type="open_id")` → `pre_cancel_event_snapshot`
- Phase 2a.5: persist `target_id=<original_event_id>`, `target_kind='calendar_event_cancel'`, `result.pre_cancel_event_snapshot`, `result.calendar_id`, `result.source_meeting_action_id`
- Phase 2b: `delete_event`
- Phase 3: `mark_bot_action_success` + transition source row to `undone`

- [ ] Tests cover: explicit event_id (success/pending/undone/failed gates), `last:true` newest-row sentinels, idempotency double-cancel, cross-chat refusal.
- [ ] Commit: `feat(tools): cancel_meeting with snapshot-before-delete and source-row retire`

### Task 8.3: list_my_meetings

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_list_my_meetings.py`

Spec §3.5:
- target defaults to `"self"` → `ctx.sender_open_id`
- `primarys` to get user's calendar_id → `calendar_event.list(user_id_type="open_id")`
- Returns `{bot_known_events, user_calendar_events, visibility_note}`
- `bot_known_events` joins `bot_actions WHERE action_type IN ('schedule_meeting','restore_schedule_meeting') AND status IN ('success','reconciled_unknown') AND target_id IS NOT NULL AND result.attendees ⊇ {target}`

- [ ] Tests cover: self default, explicit target, primarys 0 results graceful return, visibility_note rendering.
- [ ] Commit.

### Task 8.4: append_action_items

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_append_action_items.py`

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
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_create_meeting_doc.py`

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

---

# Task Group 9: undo_last_action (1 task, but big)

> **Spec ref:** §3.9. This is the safety net (§1.4); ship together with the write tools.

### Task 9.1: undo_last_action with full dispatch

**Files:**
- Modify: `bot/agent/tools.py`
- Test: `bot/tests/test_tools_undo_last_action.py`

- Inputs: `target = {last_for_me: true} | {action_id: ...} | {target_id, target_kind}`
- `last_for_me` resolution via `last_bot_action_for_sender_in_chat(action_type_in=None)` — handle `LastIsInFlight` / `LastWasUnreachable` sentinels, exclude `action_type='undo_last_action'`
- Dispatch by `action_type`:
  - `schedule_meeting` / `restore_schedule_meeting` → `delete_event` (treat 404 as success)
  - `cancel_meeting` → probe-then-restore-from-snapshot per §3.9 (if probe-200: just retire cancel row; if 404: R0 retire source schedule row → R1 build whitelisted body → R2 insert `restore_schedule_meeting` pending → R3 invite (failure → partial_success) → R4 finalize)
  - `restore_schedule_meeting` undo → dispatch by current row status (Case A: probe attendees, retry/finalize; Case B: delete event + mark undone)
  - `append_action_items` → query by `source_action_id`, batch_delete remaining, treat per-record 404 as fine, mark undone when none remain
  - `create_meeting_doc` → dispatch by `target_kind`: `docx` → delete docx + best-effort delete `.md`; `file` → delete `.md`; NULL+import_ticket → re-poll then dispatch
- Mark source row `undone`; record `undo_last_action` audit row with `target_id=<original action_id>`, `target_kind='bot_action_undo'`

- [ ] Tests cover every dispatch arm + 404-as-success + cancel-probe-200 (no restore needed) + cancel-restore happy path + cancel-restore R3 partial.
- [ ] Commit: `feat(tools): undo_last_action with per-action_type dispatch and cancel-restore R0..R4`

---

# Task Group 10: Wire into agent runner (2 tasks)

### Task 10.1: Expand allowed_tools

**Files:**
- Modify: `bot/agent/runner.py`

- [ ] Add 8 new `mcp__pmo__*` entries to the `allowed_tools` list at `runner.py:179`:
  ```python
  "mcp__pmo__resolve_people",
  "mcp__pmo__schedule_meeting",
  "mcp__pmo__cancel_meeting",
  "mcp__pmo__list_my_meetings",
  "mcp__pmo__append_action_items",
  "mcp__pmo__query_action_items",
  "mcp__pmo__create_meeting_doc",
  "mcp__pmo__undo_last_action",
  ```
- [ ] Commit: `feat(agent): expose 8 new MCP tools to the LLM`

### Task 10.2: Replace SYSTEM_PROMPT tool inventory + read-only sentence

**Files:**
- Modify: `bot/agent/runner.py` (`SYSTEM_PROMPT` constant)

Per spec §9 (iter-30 / row 117):

1. Replace the tool inventory list (around `runner.py:84`) to include the 8 new tools.
2. Remove the "这是只读问答助手" / "你不能：写代码、改文件、跑命令" lines.
3. Append the §9 directive block:
   ```
   你现在可以在飞书做事，不只是回答问题。

   默认行为：用文字回复。只有在用户意图明确指向某个写工具时才调用：
   订会/取消会议/看日程 → calendar 工具；记一下/写到表里 → action_items 工具；写成文档/整理纪要 → create_meeting_doc.

   硬规则：
   - 调用任何接受人员参数的工具前必须先调 resolve_people。如果它返回 ambiguous 或 unresolved，必须先反问用户澄清。绝不要猜。
   - 传给 schedule_meeting 的所有时间必须是 RFC3339 with timezone。先调 today_iso 拿到提问者所在时区。
   - schedule_meeting 返回 conflict 时，把冲突告诉用户并提议替代时间，不要盲目重试。
   - 不要修改不是你创建的飞书资源。只能取消/编辑你自己 bot_actions 中的事件。
   - list_my_meetings 返回非空 visibility_note 或 user_calendar_events 看起来稀疏时，把这个不确定性告诉用户；绝不在没承认可见性限制的情况下断言"你没有会"。
   - 第一人称日历问题（"我下午有啥会" / "我下周三有空吗"），调用 list_my_meetings 时不传 target — 工具默认返回 asker。绝不为了拿 asker 的 open_id 而调 resolve_people。
   ```

- [ ] Commit: `feat(agent): replace SYSTEM_PROMPT tool inventory + add Feishu write directives`

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
| 4. Feishu wrappers | 8 | 5-6 hours |
| 5. Bootstrap script | 1 | 1 hour |
| 6. RequestContext refactor | 3 | 2 hours |
| 7. Read tools | 3 | 2 hours |
| 8. Write tools | 5 | 6-8 hours (cancel_meeting + create_doc are big) |
| 9. undo_last_action | 1 | 4 hours (largest single task) |
| 10. Runner wiring | 2 | 30 min |
| 11. Smoke test | 1 | 1-2 hours |
| **Total** | **40** | **~30 hours** |

Spread over 3-5 working days for one developer.
