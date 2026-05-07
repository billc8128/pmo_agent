# Proactive PMO Agent 2.0c Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in Feishu group memory, passive chat retrieval, simple PMO people notes, and a later observer path that can proactively speak with strict trust budgets.

**Architecture:** Phase 1 stores opted-in group messages as a fact layer and exposes scoped retrieval tools to the existing conversational agent. People memory is a small natural-language note per teammate, updated from observed work context. Phase 2 adds an observer loop that creates candidates but still routes through investigation and delivery safety gates.

**Tech Stack:** FastAPI bot, Feishu event webhooks, Supabase/Postgres migrations + service-role queries, Claude Agent SDK MCP tools, existing 1.0c investigation pipeline, pytest, Vercel/Railway/Supabase deploy flow.

---

## 0. Cut Points

This plan has two release cuts:

1. **2.0c Phase 1: Passive Memory**
   - group opt-in
   - message storage
   - chat retrieval tools
   - people `pmo_notes`
   - no proactive observer messages

2. **2.0c Phase 2: Observer**
   - observer candidates
   - trust budgets
   - investigator integration
   - proactive messages / mentions

Do not implement Phase 2 until Phase 1 has passed real Feishu e2e and
users have tried passive retrieval in at least one active group.

---

## 1. File Map

### Migrations

- Create: `backend/supabase/migrations/0024_chat_memory.sql`
  - `chat_memory_settings`
  - `chat_messages`
  - `people_memory`
  - service-role-only RLS
  - indexes and comments

- Future Phase 2 create: `backend/supabase/migrations/0025_observer_candidates.sql`
  - `observer_candidates`
  - `observer_feedback`
  - budget indexes

### Bot data layer

- Modify: `bot/db/queries.py`
  - chat memory settings helpers
  - chat message insert/search/window helpers
  - people memory get/upsert helpers
  - Phase 2 observer candidate helpers later

- Create: `bot/chat_memory/redaction.py` or reuse existing
  `bot/external/redaction.py`
  - shared redaction wrapper for Feishu chat text

- Create: `bot/chat_memory/ingest.py`
  - idempotent conversion from Feishu parsed message to
    `chat_messages` row

- Create: `bot/chat_memory/people.py`
  - `person_key` resolution
  - people-note update prompt wrapper

- Create: `bot/chat_memory/cleanup_loop.py`
  - retention cleanup for expired `chat_messages`

### Feishu / app entry

- Modify: `bot/app.py`
  - store non-@ messages for enabled chats
  - continue existing agent flow for @ messages
  - do not wake the agent for ordinary messages

- Modify: `bot/feishu/events.py`
  - ensure parsed event exposes message id, parent/root id, sender
    display fields, mention metadata, timestamp

### Agent tools and prompts

- Modify: `bot/agent/tools_meta.py`
  - current meta MCP server already owns `get_recent_turns`,
    subscriptions, and workspace/team lookup; chat/people memory
    tools belong here unless implementation discovers a cleaner split
- Modify: `bot/agent/tools.py` only if the top-level tool bundle needs
  explicit wiring changes
  - add `get_recent_chat_messages`
  - add `search_chat_messages`
  - add `get_chat_window`
  - add `get_people_memory`
  - add `suggest_people_for_topic`
  - add `enable_chat_memory`, `disable_chat_memory`, `chat_memory_status`

- Modify: `bot/agent/runner.py`
  - prompt contract: use chat-memory tools for "刚才/今天/我们聊的/todo/谁适合"
  - do not create subscriptions for past-context questions

### Tests

- Create: `bot/tests/test_chat_memory.py`
- Create: `bot/tests/test_people_memory.py`
- Modify: existing Feishu/app tests if present
- Modify: proactive/integration tests only where tool registration expects
  a fixed tool list

### Docs

- Keep updated: `docs/specs/2026-05-07-proactive-agent-2.0c-spec.md`
- Keep updated: this plan
- Optional after implementation: update
  `docs/specs/2026-05-06-proactive-agent-2.0-strategy.md` sequence text
  to mention Phase 1 passive memory before observer

---

## 2. Pre-flight

- [ ] **Step 1: Confirm worktree state**

Run:

```bash
git status --short
```

Expected: only known untracked files unrelated to this feature, or a
clean tree.

- [ ] **Step 2: Confirm deployed baseline**

Run:

```bash
git log --oneline -5
```

Expected: latest commits include 2.0a, 2.0b, and Feishu OAuth fixes.

- [ ] **Step 3: Confirm Feishu event permissions**

In Feishu Open Platform, confirm the app receives group message events
for messages that do not @ the bot. If Feishu only sends @ events with
current permissions, add the required event subscription / app scope
before implementation.

Exit criterion: we know whether ordinary group messages will reach
`/feishu/webhook`. If not, do not start code until Feishu app config is
fixed.

- [ ] **Step 4: Confirm Railway target**

Run:

```bash
railway status
```

Expected: CLI is linked to the bot service/environment that currently
serves `/feishu/webhook`. Do not deploy from this plan until the target
is explicit.

---

## 3. Task 1 — Migration 0024: Chat and People Memory

**Files:**
- Create: `backend/supabase/migrations/0024_chat_memory.sql`
- Test: `bot/tests/test_chat_memory.py`

- [ ] **Step 1: Write SQL shape tests**

Add tests that read the migration file and assert:

```python
def test_0024_creates_chat_memory_tables():
    sql = Path("backend/supabase/migrations/0024_chat_memory.sql").read_text()
    assert "create table if not exists public.chat_memory_settings" in sql
    assert "create table if not exists public.chat_messages" in sql
    assert "create table if not exists public.people_memory" in sql
    assert "feishu_message_id" in sql
    assert "references public.chat_memory_settings(chat_id) on delete cascade" in sql
    assert "between 1 and 730" in sql
    assert "to_tsvector('simple', text_redacted)" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py::test_0024_creates_chat_memory_tables -q
```

Expected: FAIL because migration does not exist.

- [ ] **Step 3: Add migration**

Create `0024_chat_memory.sql` with:

- `chat_memory_settings` per spec §4.3
- `chat_messages` per spec §4.4
  - `chat_id` references `chat_memory_settings(chat_id) on delete cascade`
- `people_memory` per spec §5.1
  - includes `metadata jsonb` for note updater audit fields
- comments stating stored payloads are redacted, not raw Feishu bodies
- RLS enabled with no public policies
- indexes:
  - `chat_messages_chat_time_idx`
  - `chat_messages_sender_time_idx`
  - `chat_messages_parent_idx`
  - `chat_messages_text_fts_idx`
  - `people_memory_profile_idx`
  - `people_memory_feishu_idx`

- [ ] **Step 4: Run migration shape tests**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py::test_0024_creates_chat_memory_tables -q
```

Expected: PASS.

- [ ] **Step 5: Real DB smoke on sandbox**

Apply to Supabase sandbox, then in a transaction:

```sql
begin;
insert into public.chat_memory_settings (chat_id, enabled, enabled_at)
values ('oc_test', true, now());

insert into public.chat_messages (
  feishu_message_id, chat_id, chat_type, sender_open_id,
  text_redacted, occurred_at
) values (
  'om_test_1', 'oc_test', 'group', 'ou_test',
  'vibelive dev push 后要总结 diff', now()
);

select count(*) from public.chat_messages where chat_id='oc_test';

delete from public.chat_memory_settings where chat_id='oc_test';
select count(*) from public.chat_messages where chat_id='oc_test';
rollback;
```

Expected: first count = 1, second count = 0, proving delete cascade.

- [ ] **Step 6: Commit**

```bash
git add backend/supabase/migrations/0024_chat_memory.sql bot/tests/test_chat_memory.py
git commit -m "feat(db): add chat and people memory tables"
```

---

## 4. Task 2 — DB Query Helpers

**Files:**
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_chat_memory.py`, `bot/tests/test_people_memory.py`

- [ ] **Step 1: Write tests with fake Supabase tables**

Cover:

- `is_chat_memory_enabled(chat_id)` returns False on no row
- `enable_chat_memory(...)` upserts enabled row
- `disable_chat_memory(...)` sets enabled False and disabled fields
- `insert_chat_message(...)` uses idempotent upsert / ignore duplicate
- `search_chat_messages(...)` constrains by chat_id
- `get_chat_window(...)` never crosses chat_id
- `upsert_people_memory(...)` writes by `person_key`
- `get_people_memory(...)` returns objective fields + note

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py bot/tests/test_people_memory.py -q
```

Expected: FAIL because helpers are missing.

- [ ] **Step 3: Implement helpers in `queries.py`**

Helper signatures:

```python
def is_chat_memory_enabled(chat_id: str) -> bool: ...
def enable_chat_memory(chat_id: str, *, user_id: str | None, open_id: str | None, retention_days: int = 90) -> dict: ...
def disable_chat_memory(chat_id: str, *, user_id: str | None, open_id: str | None) -> dict: ...
def chat_memory_status(chat_id: str) -> dict | None: ...
def insert_chat_message(row: dict) -> dict | None: ...
def get_recent_chat_messages(chat_id: str, *, since: str | None, until: str | None, limit: int = 80, sender: str | None = None) -> list[dict]: ...
def search_chat_messages(chat_id: str, *, query: str, since: str | None, until: str | None, limit: int = 30) -> list[dict]: ...
def get_chat_window(chat_id: str, *, anchor_message_id: str, before: int = 12, after: int = 12) -> list[dict]: ...
def upsert_people_memory(person_key: str, **fields) -> dict: ...
def get_people_memory(query: str, *, limit: int = 5) -> list[dict]: ...
```

Keep all helpers service-role only. Do not expose arbitrary chat_id to
LLM tools without request-context scoping.

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py bot/tests/test_people_memory.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/db/queries.py bot/tests/test_chat_memory.py bot/tests/test_people_memory.py
git commit -m "feat(bot): add chat memory query helpers"
```

---

## 5. Task 3 — Feishu Message Capture Without Waking Agent

**Files:**
- Modify: `bot/feishu/events.py`
- Create: `bot/chat_memory/ingest.py`
- Modify: `bot/app.py`
- Test: `bot/tests/test_chat_memory.py`

- [ ] **Step 1: Write failing tests**

Tests:

1. Enabled chat + non-@ group message stores row and returns `"stored"`
   without calling `_handle_message`.
2. Disabled chat + non-@ group message returns `"group not addressed"`
   and stores nothing.
3. Enabled chat + @ message stores row and still calls `_handle_message`.
4. Duplicate Feishu message id does not create a second row.
5. Enabled chat + @ message is stored exactly once: webhook stores it
   before `_handle_message`, and `_handle_message` must not store again.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Extend parsed Feishu event**

Ensure `ParsedMessageEvent` exposes:

- `message_id`
- `chat_id`
- `chat_type`
- `sender_open_id`
- `sender_display_name` if available
- `text`
- `is_at_bot`
- `parent_message_id`
- `root_message_id`
- `mentions`
- `create_time` / occurred_at
- `message_type`

- [ ] **Step 4: Implement `chat_memory.ingest.store_message_if_enabled`**

Pseudo-code:

```python
def store_message_if_enabled(ev: ParsedMessageEvent, sender_identity: dict | None = None) -> bool:
    if ev.chat_type != "group":
        return False
    if not queries.is_chat_memory_enabled(ev.chat_id):
        return False
    row = build_chat_message_row(ev, sender_identity)
    queries.insert_chat_message(row)
    return True
```

Use redacted text. Do not store raw encrypted event body.

- [ ] **Step 5: Modify `feishu_webhook` flow**

Flow:

```python
parsed = feishu_events.parse_message_event(body)
if parsed is None:
    return PlainTextResponse("ignored")

stored = await maybe_store_message(parsed)

if parsed.chat_type == "group" and not parsed.is_at_bot:
    return PlainTextResponse("stored" if stored else "group not addressed")

asyncio.create_task(_handle_message(parsed))
return PlainTextResponse("ok")
```

Ownership rule: `feishu_webhook` is the only place that stores inbound
Feishu chat messages. `_handle_message` and downstream agent code must
not write `chat_messages`; they only read memory through tools.

Avoid duplicate DB lookups where possible, but correctness first.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/app.py bot/feishu/events.py bot/chat_memory/ingest.py bot/tests/test_chat_memory.py
git commit -m "feat(bot): capture opted-in group messages"
```

---

## 6. Task 4 — Chat Memory Control Tools

**Files:**
- Modify: `bot/agent/tools_meta.py`
- Modify: `bot/agent/tools.py` only if needed for top-level wiring
- Modify: `bot/agent/runner.py`
- Test: `bot/tests/test_chat_memory.py`

- [ ] **Step 1: Write failing tool tests**

Cover:

- `enable_chat_memory` fails outside group chat
- `enable_chat_memory` writes current chat_id only
- `disable_chat_memory` writes current chat_id only
- `chat_memory_status` returns enabled/disabled text fields

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Add tools**

Tools:

```text
enable_chat_memory(retention_days=90)
disable_chat_memory()
chat_memory_status()
```

They must use `RequestContext.chat_id` and `RequestContext.chat_type`.
They must reject arbitrary chat_id arguments.

- [ ] **Step 4: Prompt update**

In `bot/agent/runner.py`, add:

- "开始记录这个群" / "停止记录这个群" / "这个群有没有开启记忆" are memory
  management requests.
- After successful management tool call, stop and summarize the result.
- Do not investigate repo/turns for memory-management requests.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/agent/runner.py bot/agent/tools_meta.py bot/agent/tools.py bot/tests/test_chat_memory.py
git commit -m "feat(bot): add chat memory control tools"
```

---

## 7. Task 5 — Passive Chat Retrieval Tools

**Files:**
- Modify: `bot/agent/tools_meta.py`
- Modify: `bot/agent/tools.py` only if needed for top-level wiring
- Modify: `bot/agent/runner.py`
- Test: `bot/tests/test_chat_memory.py`

- [ ] **Step 1: Write failing tests**

Tests:

- `get_recent_chat_messages` returns only current chat rows
- `search_chat_messages` does not accept caller-supplied chat_id
- `get_chat_window` returns ordered messages around anchor
- disabled chat returns a clear "memory not enabled" error
- Chinese substring query works even if FTS misses

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement tools**

Tool names and behavior:

```text
get_recent_chat_messages(since?, until?, limit?, sender?)
search_chat_messages(query, since?, until?, limit?)
get_chat_window(anchor_message_id, before?, after?)
```

All tools:

- use current chat_id from request context
- cap limit (`recent` max 120, `search` max 50, `window` max 50 total)
- return compact rows with timestamp, sender label, text, message_id
- never expose raw payload

- [ ] **Step 4: Prompt update**

Add rules:

- For "刚才", "今天", "我们聊的", "达成一致", "TODO", "谁负责",
  use chat-memory tools before answering.
- If memory is disabled, say that and suggest enabling it.
- Separate consensus from proposals.

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: PASS.

- [ ] **Step 6: Run full bot tests**

Run:

```bash
python -m pytest bot/tests -q
```

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add bot/agent/runner.py bot/agent/tools_meta.py bot/agent/tools.py bot/db/queries.py bot/tests/test_chat_memory.py
git commit -m "feat(bot): add passive chat memory retrieval"
```

---

## 8. Task 6 — People Memory Helpers and Tools

**Files:**
- Create: `bot/chat_memory/people.py`
- Modify: `bot/agent/tools_meta.py`
- Modify: `bot/agent/tools.py` only if needed for top-level wiring
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_people_memory.py`

- [ ] **Step 1: Write failing tests**

Cover:

- `person_key_for_identity(profile_id=...) == "profile:{id}"`
- unbound Feishu sender uses `feishu:{open_id}`
- `get_people_memory("hellobit")` returns matching note
- `suggest_people_for_topic(...)` returns "not enough signal" when no
  notes exist
- conversational tool list does **not** include
  `update_people_memory_note`
- updater prompt rejects sensitive/personality/performance judgment
  in fixture output

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_people_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement people helper**

Functions:

```python
def person_key_for_identity(*, profile_id: str | None, feishu_open_id: str | None) -> str: ...
def build_people_note_prompt(old_note: str, context: list[dict]) -> str: ...
def sanitize_people_note(note: str) -> str: ...
```

`sanitize_people_note` should not pretend to solve safety completely;
it can remove obvious forbidden phrases and the prompt should carry the
real boundary.

- [ ] **Step 4: Add conversational tools**

Tools:

```text
get_people_memory(query, limit=5)
suggest_people_for_topic(topic, limit=5)
```

Do **not** register `update_people_memory_note` with the conversation
MCP server. Note writes happen through `bot/chat_memory/people_loop.py`
or an internal helper called by tests/background jobs only.

- [ ] **Step 5: Prompt update**

Add:

- People memory is a work-context note, not a scorecard.
- Suggest people politely; do not assign ownership unless the chat
  explicitly did.
- Do not use private DM context for group recommendations.

- [ ] **Step 6: Run tests**

Run:

```bash
python -m pytest bot/tests/test_people_memory.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/chat_memory/people.py bot/db/queries.py bot/agent/tools_meta.py bot/agent/tools.py bot/tests/test_people_memory.py
git commit -m "feat(bot): add PMO people memory notes"
```

---

## 9. Task 7 — Background People Memory Updater

**Files:**
- Create: `bot/chat_memory/people_loop.py`
- Modify: `bot/app.py`
- Test: `bot/tests/test_people_memory.py`

- [ ] **Step 1: Write failing tests**

Tests:

- loop selects only enabled chats with recent messages
- skips people with no new evidence
- caps people/messages per run
- enforces global daily rewrite cap
- enforces per-note input/output token caps
- debounces same-person rewrites within 10 minutes
- writes note through `upsert_people_memory`
- never sends Feishu messages

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_people_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement loop**

Loop defaults:

- interval: 30 minutes
- max chats per run: 10
- max people per chat: 20
- max messages per chat: 80
- max note rewrites per day globally: 200
- max input tokens per note rewrite: 4k
- max output tokens per note rewrite: 600
- max input tokens per run: 30k
- same person debounce: 10 minutes
- model: same lightweight model family used for classifier/gatekeeper
  unless current config says otherwise

The loop can be disabled by env:

```text
PEOPLE_MEMORY_LOOP_ENABLED=false
PEOPLE_MEMORY_DAILY_REWRITE_CAP=200
```

- [ ] **Step 4: Wire into FastAPI lifespan**

In `bot/app.py`, start the loop next to decider/investigator/delivery
tasks, guarded by config.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest bot/tests/test_people_memory.py bot/tests/test_proactive_notifications.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/app.py bot/chat_memory/people_loop.py bot/tests/test_people_memory.py
git commit -m "feat(bot): refresh people memory in background"
```

---

## 10. Task 8 — Chat Memory Retention Cleanup

**Files:**
- Create: `bot/chat_memory/cleanup_loop.py`
- Modify: `bot/db/queries.py`
- Modify: `bot/app.py`
- Test: `bot/tests/test_chat_memory.py`

- [ ] **Step 1: Write failing tests**

Tests:

- expired messages are deleted based on each chat's `retention_days`
- disabled chats still honor retention
- messages newer than the cutoff are preserved
- cleanup run is capped so one large chat cannot block the loop

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest bot/tests/test_chat_memory.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement cleanup helper and loop**

Helper:

```python
def delete_expired_chat_messages(*, limit: int = 5000) -> int: ...
```

Loop defaults:

- interval: 24 hours
- delete limit per run: 5000 rows
- disabled by env only for emergency:

```text
CHAT_MEMORY_CLEANUP_ENABLED=true
```

- [ ] **Step 4: Wire into FastAPI lifespan**

Start next to other background tasks. The loop must not affect message
capture if cleanup fails; log and retry on next interval.

- [ ] **Step 5: Run tests and commit**

```bash
python -m pytest bot/tests/test_chat_memory.py -q
git add bot/chat_memory/cleanup_loop.py bot/db/queries.py bot/app.py bot/tests/test_chat_memory.py
git commit -m "feat(bot): clean up expired chat memory"
```

---

## 11. Task 9 — Phase 1 End-to-End Validation

**Files:**
- No required code files unless tests expose gaps
- Update docs if behavior changed

- [ ] **Step 1: Run full bot tests**

Run:

```bash
python -m pytest bot/tests -q
```

Expected: all tests pass.

- [ ] **Step 2: Apply 0024 to Supabase sandbox**

Run project-standard Supabase migration apply process.

Expected: migration succeeds.

- [ ] **Step 3: Sandbox SQL smoke**

Run:

```sql
begin;
insert into public.chat_memory_settings (chat_id, enabled, enabled_at)
values ('oc_smoke', true, now())
on conflict (chat_id) do update set enabled=true, enabled_at=now();

insert into public.chat_messages (
  feishu_message_id, chat_id, chat_type, sender_open_id,
  sender_display_name, text_redacted, occurred_at
) values (
  'om_smoke_1', 'oc_smoke', 'group', 'ou_smoke', 'bcc',
  'vibelive dev push 后要总结 diff', now()
);

select feishu_message_id, text_redacted
from public.chat_messages
where chat_id='oc_smoke';
rollback;
```

Expected: one row returned.

- [ ] **Step 4: Deploy bot to Railway staging/prod**

First confirm the linked service:

```bash
railway status
```

Expected: target is the bot service/environment serving
`/feishu/webhook`.

Run:

```bash
railway up --detach
```

Expected: deploy succeeds; health endpoint OK.

- [ ] **Step 5: Feishu e2e**

In a group:

1. `@包工头 开始记录这个群`
2. Send three ordinary messages without @:
   - "vibelive dev push 后要总结 diff"
   - "hellobit 普通 push 不用打扰"
   - "今天先让 bcc 确认通知文案"
3. Verify bot does not reply to ordinary messages.
4. Ask: `@包工头 基于我们刚才聊的信息，有哪些达成一致的 TODO?`
5. Expected: bot uses chat-memory tools and answers from the messages.

- [ ] **Step 6: Commit any verification doc updates**

```bash
git add docs/specs/2026-05-07-proactive-agent-2.0c-spec.md docs/specs/2026-05-07-proactive-agent-2.0c-plan.md
git commit -m "docs: record 2.0c passive memory validation"
```

---

## 12. Task 10 — Phase 2 Schema: Observer Candidates

Do this only after Phase 1 is stable.

**Files:**
- Create: `backend/supabase/migrations/0025_observer_candidates.sql`
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_observer.py`

- [ ] **Step 1: Write failing migration tests**

Assert table contains:

- `observer_candidates`
- `observer_candidates_target_check`
- `observer_feedback`
- `evidence_message_ids text[]`
- `evidence_event_ids bigint[]`
- `suggested_people text[]`
- status check including `open`, `notified`, `suppressed`, `expired`
- feedback kind check including `not_useful`, `suppress_candidate_type`,
  `suppress_topic`, `disable_observer`

- [ ] **Step 2: Add migration**

Create table per spec §6.3 plus indexes:

- `observer_candidates_status_idx`
- `observer_candidates_chat_open_idx`
- `observer_candidates_expires_idx`
- `observer_feedback_chat_kind_idx`
- `observer_feedback_topic_idx`

- [ ] **Step 3: Add DB helpers**

```python
def open_observer_candidate(row: dict) -> int: ...
def claim_observer_candidates(limit: int, claim_id: str) -> list[dict]: ...
def mark_observer_candidate_notified(candidate_id: int, notification_id: int) -> None: ...
def mark_observer_candidate_suppressed(candidate_id: int, reason: str) -> None: ...
def record_observer_feedback(row: dict) -> int: ...
def observer_feedback_for_chat(chat_id: str) -> list[dict]: ...
```

- [ ] **Step 4: Run tests and commit**

```bash
python -m pytest bot/tests/test_observer.py -q
git add backend/supabase/migrations/0025_observer_candidates.sql bot/db/queries.py bot/tests/test_observer.py
git commit -m "feat(db): add observer candidates and feedback"
```

---

## 13. Task 11 — Phase 2 Observer Loop

Do this only after Task 10 and a separate user review.

**Files:**
- Create: `bot/agent/observer.py`
- Create: `bot/agent/observer_loop.py`
- Modify: `bot/app.py`
- Test: `bot/tests/test_observer.py`

- [ ] **Step 1: Write failing tests**

Tests:

- most observer runs produce no candidate
- candidate output requires evidence message/event ids
- low confidence mention candidates are suppressed
- per-chat daily cap blocks second delivery
- feedback table suppresses disabled candidate types/topics
- observer never sends directly; it only opens candidates

- [ ] **Step 2: Implement observer prompt and parser**

Prompt per spec §8.3. JSON output only.

- [ ] **Step 3: Implement snapshot builder**

Input budget:

- max 80 recent chat messages
- max 10 people notes
- max 20 recent repo/turn events
- max 10 recent notifications

- [ ] **Step 4: Implement observer loop**

Default disabled:

```text
OBSERVER_LOOP_ENABLED=false
```

Per-chat `chat_memory_settings.observer_enabled` must also be true.

- [ ] **Step 5: Run tests and commit**

```bash
python -m pytest bot/tests/test_observer.py bot/tests -q
git add bot/agent/observer.py bot/agent/observer_loop.py bot/app.py bot/tests/test_observer.py
git commit -m "feat(bot): add PMO observer candidates"
```

---

## 14. Task 12 — Phase 2 Investigator and Delivery Integration

Do this only after Task 11.

**Files:**
- Modify: `bot/agent/investigator_loop.py`
- Modify: `bot/agent/investigator.py`
- Modify: `bot/agent/renderer.py`
- Modify: `bot/agent/delivery_loop.py` if needed
- Test: `bot/tests/test_observer.py`

- [ ] **Step 1: Write failing tests**

Tests:

- observer candidate is investigated before notification
- investigator can read evidence chat windows
- renderer cannot add new facts outside investigator brief
- target routing follows 2.0b target fields
- feedback "这类提醒没用" suppresses future category

- [ ] **Step 2: Add candidate claim path**

Either:

- extend investigator loop to claim subscription jobs and observer
  candidates in separate batches, or
- create a small observer-investigator loop that reuses investigator
  prompt modules.

Keep code paths separate if merging makes the existing 1.0c
subscription job flow harder to reason about.

- [ ] **Step 3: Add trust budget checks**

Before creating notification:

- chat daily observer cap
- user daily observer DM cap
- same topic in last 24h
- disabled observer flag

- [ ] **Step 4: Run tests and commit**

```bash
python -m pytest bot/tests/test_observer.py bot/tests -q
git add bot/agent/investigator.py bot/agent/investigator_loop.py bot/agent/renderer.py bot/agent/delivery_loop.py bot/tests/test_observer.py
git commit -m "feat(bot): deliver observer-vetted PMO nudges"
```

---

## 14. Deployment Gates

### Phase 1 gate

Required before production:

- [ ] `python -m pytest bot/tests -q` passes
- [ ] migration 0024 applies to sandbox
- [ ] sandbox SQL smoke passes
- [ ] retention cleanup test passes
- [ ] `railway status` confirms the bot service/environment before deploy
- [ ] Railway deploy succeeds
- [ ] Feishu e2e:
  - enable memory
  - ordinary messages stored silently
  - passive TODO query works
  - disabled chat stores nothing

### Phase 2 gate

Required before enabling observer:

- [ ] observer env flag defaults OFF in production
- [ ] per-chat observer flag defaults false
- [ ] observer candidate tests pass
- [ ] trust budget tests pass
- [ ] one internal chat runs observer in shadow mode for at least 3 days
- [ ] shadow-mode report reviewed:
  - candidate count
  - would-send count
  - false positives
  - missed obvious PMO moments
- [ ] only then enable real delivery for one chat

---

## 15. Rollback

### Phase 1

Immediate rollback:

- set `CHAT_MEMORY_CAPTURE_ENABLED=false` if implemented as env guard
- or disable every row in `chat_memory_settings`

Database rollback is not required for incidents; stop capture first.
Historical rows can be deleted with:

```sql
delete from public.chat_messages where chat_id = '<chat_id>';
update public.chat_memory_settings set enabled=false, disabled_at=now()
where chat_id = '<chat_id>';
```

### Phase 2

Immediate rollback:

```sql
update public.chat_memory_settings set observer_enabled=false;
```

Also set:

```text
OBSERVER_LOOP_ENABLED=false
```

Observer must be independently disableable without disabling passive
chat memory.
