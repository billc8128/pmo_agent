# PMO Bot Write Tools — Design Spec

- **Status**: Draft, pending implementation
- **Date**: 2026-05-02
- **Author**: brainstormed with Claude (Opus 4.7)
- **Supersedes / extends**: `2026-04-29-mvp-design.md`§"agent tools" — the
  bot was originally read-only; this spec turns it into a real PMO
  assistant that can also act on Feishu.

This spec is the source of truth for the "包工头" bot's first set of
**write-side** capabilities. Read-only tools (`list_users`,
`get_recent_turns`, etc.) defined in `bot/agent/tools.py` are unaffected.

---

## 1. Product framing

### 1.1 What changes

The PMO bot today answers questions about a team's AI-coding activity.
After this spec ships, it can also do the things a human PMO would do:

- Schedule and cancel meetings
- Look up someone's calendar
- Record action items into a structured table
- Produce a meeting-notes document
- Resolve names ("albert", "@bcc", "李四") to Feishu users

### 1.2 Operating principle: the bot is an employee

Throughout this spec, "包工头" is treated as a junior employee of the
team, not as a faceless service:

- It has its own Feishu identity (open_id, name, avatar — already exists).
- It owns its own primary calendar, its own Bitable base ("包工头的工作台"),
  and its own Docx folder ("包工头的文档柜"). Everything it produces
  lives there by default.
- It only does work that is explicitly asked for. Default response mode
  is plain text; tools fire only when the user's intent maps clearly to
  a write action ("订个会", "记一下", "写成文档").

This framing collapses several authorization questions: meetings are
owned by the bot (so all attendees get `attendee_ability=can_modify_event`
to share editing), records are authored by the bot, docs are owned by
the bot. No OAuth, no user_access_token, no per-user token refresh.
`tenant_access_token` everywhere.

### 1.3 What this spec does NOT cover (intentional YAGNI)

| Out of scope | Why |
| --- | --- |
| OAuth / user_access_token | Bot-as-employee makes it unnecessary |
| Recurring meetings (rrule) | One-off meetings cover 95% of MVP requests |
| Meeting rooms / VC links auto-attach | Adds Feishu permission scope; defer |
| Pulling people into chats, creating groups | Different trust profile; later |
| Editing user-owned (non-bot) Bitables / Docs | Different trust profile; later |
| Feishu Tasks integration | `action_items` table in the bot's Bitable subsumes it |
| Cross-language fuzzy name matching | `resolve_people` returns matches; agent reconfirms |
| `lark-cli` shell-out from the daemon | CLI is OAuth-based, not headless-friendly |

---

## 2. Architecture additions

```
┌────────────── Feishu ──────────────┐
│ • Calendar API (events, freebusy)   │
│ • Bitable API   (base/tables/records)│
│ • Docx API      (create/append)      │
│ • Contact API   (search org)         │
└────────────────────────────────────┘
              ▲
              │ tenant_access_token only
              │
   ┌──────────┴────────────┐
   │ bot/feishu/client.py  │   thin lark-oapi wrappers
   └──────────┬────────────┘
              │
   ┌──────────┴────────────┐
   │ bot/agent/tools.py    │   8 write tools, three-phase pattern
   └──────────┬────────────┘
              │
   ┌──────────┴────────────┐
   │ bot/db/queries.py     │   bot_actions + bot_workspace queries
   └──────────┬────────────┘
              │
   ┌──────────┴────────────┐
   │ Supabase (Postgres)   │
   │  • bot_actions        │   (idempotency lock + side-effect log)
   │  • bot_workspace      │   (single row: bot's calendar/base/folder IDs)
   └───────────────────────┘
```

No new services, no new processes. The existing FastAPI app gains
write tools; existing event handling, dedup, and cards are unchanged.

---

## 3. Tool catalog

8 new write tools + 1 modification to an existing tool.

| # | Tool | Domain | Read or Write |
| --- | --- | --- | --- |
| 1 | `resolve_people` | Contact | Read |
| 2 | `today_iso` (modified) | Time | Read |
| 3 | `schedule_meeting` | Calendar | **Write** |
| 4 | `cancel_meeting` | Calendar | **Write** |
| 5 | `list_my_meetings` | Calendar | Read |
| 6 | `append_action_items` | Bitable | **Write** |
| 7 | `query_action_items` | Bitable | Read |
| 8 | `create_meeting_doc` | Docx | **Write** |
| 9 | `undo_last_action` | Audit | **Write** (compensating) |

System prompt receives a paragraph telling the model: default to plain
text; call write tools only when the user's intent is unambiguous; for
person references, always go through `resolve_people` first.

### 3.1 `resolve_people`

**Purpose**: Map free-form names ("albert", "@bcc", "李四", "研发的小王")
to Feishu `open_id`s before any write tool runs.

**Resolution order** (per input string):

1. **`profiles` + `feishu_links` join** in Supabase — handles people who
   already use pmo_agent. Highest confidence.
2. **Feishu `contact.v3.user.batch_get_id`** by name — fast exact match
   against the directory.
3. **Feishu `contact.v3.user.search`** — fuzzy fallback, scoped to the
   whole org (we chose B in brainstorming over the narrower "only
   visible to bot" option).

**Return shape** (split explicitly to make agent reasoning clean):

```json
{
  "resolved": [
    {
      "input": "albert",
      "open_id": "ou_xxxx",
      "display_name": "Albert Wang",
      "department": "研发",
      "email": "albert@example.com",
      "source": "profiles" | "directory_exact" | "directory_search"
    }
  ],
  "ambiguous": [
    {
      "input": "albert",
      "candidates": [
        {"open_id": "ou_a", "display_name": "Albert Wang", "department": "研发"},
        {"open_id": "ou_b", "display_name": "Albert Lee", "department": "产品"}
      ]
    }
  ],
  "unresolved": ["小王"]
}
```

**Tool description directive**: "If `unresolved` is non-empty or
`ambiguous` is non-empty, you MUST reply to the user asking to clarify
before invoking any other tool that takes person inputs."

### 3.2 `today_iso` (extended)

Existing tool gains one field: `user_timezone`, fetched once via
`contact.v3.user.get` for the asker's open_id (cached in-process for
the run). Without timezone the model has no safe way to interpret
"下周三 3 点".

```json
{
  "now": "2026-05-02T07:14:55+00:00",
  "user_timezone": "Asia/Shanghai",
  "user_today_local": "2026-05-02",
  "today_start": "...",
  "yesterday_start": "...",
  ...
}
```

### 3.3 `schedule_meeting`

**Inputs**:
- `title: str`
- `start_time: str` — RFC3339 with timezone, no defaults, no ambiguity.
- `duration_minutes: int = 30`
- `attendee_open_ids: list[str]` — must come from `resolve_people`.
- `description: str = ""`
- `reminder_minutes: int = 15`

**Internal sequence** (the three-phase pattern, see §5):

1. **Pre-check**: call `calendar.v4.freebusy.list` for all attendees over
   the requested window. If any conflict, return
   `{conflict: [{open_id, busy_event_summary}]}` and DO NOT create the
   event. The agent reasks the user.
2. **Idempotency**: insert `bot_actions(message_id, action_type=
   "schedule_meeting", status="pending", args=...)`. UNIQUE
   `(message_id, action_type)` constraint surfaces duplicates.
3. **Create event**: `calendar.v4.calendar_event.create` against the
   bot's primary calendar with `attendee_ability=can_modify_event`.
4. **Invite attendees**: `calendar.v4.calendar_event.attendee.create_batch`.
5. **Persist**: update `bot_actions` to `status=success`, store
   `target_id=event_id`, `result={event_id, calendar_id, link, attendees}`.
6. Return event details to the agent.

### 3.4 `cancel_meeting`

**Inputs**: `event_id` OR `last:true` (cancels most recent
bot-scheduled meeting in the conversation).

Looks up the event in `bot_actions` by `target_id` to confirm it was
bot-scheduled; refuses to delete events the bot did not create.

### 3.5 `list_my_meetings`

**Inputs**: `user_open_id`, `since`, `until`.
Calls `calendar.v4.calendar_event.list` against the user's primary
calendar (read-only scope, no write). Returns title, start/end,
attendees, location.

### 3.6 `append_action_items`

**Inputs**:
- `items: [{title: str, owner_open_id: str, due_date?: str, project?: str}]`
- `meeting_event_id?: str` (optional link to an event in the same conversation)

Writes one record per item to the `action_items` table in the bot's
Bitable base. Three-phase pattern. `project` defaults to the
project most active in the asker's recent turns (see §6).

### 3.7 `query_action_items`

**Inputs**: any combination of `owner_open_id`, `project`, `status`,
`since`, `until`.

Reads from the `action_items` table; no write side effects.

### 3.8 `create_meeting_doc`

**Inputs**:
- `title: str`
- `markdown_body: str` — Feishu Docx accepts a Markdown import; we use it.
- `meeting_event_id?: str`

Three-phase pattern. Creates a Docx in the bot's "文档柜" folder, returns
`{doc_token, url}`. The agent embeds the link in its reply.

### 3.9 `undo_last_action`

**Inputs**: `message_id?: str` (defaults to current).

Looks up the most recent `bot_actions` row for the message, dispatches
on `action_type` to the right "compensating" Feishu call:
- `schedule_meeting` → `calendar_event.delete`
- `append_action_items` → `bitable.app.table.record.delete` (one per row)
- `create_meeting_doc` → `drive.v1.file.delete`

Marks the row `status=undone`. Idempotent (calling it again is a no-op).

---

## 4. Bot workspace bootstrap

A new one-shot script: `bot/scripts/bootstrap_bot_workspace.py`.

**On first run** (per environment, dev / staging / prod):

1. `calendar.v4.calendar.create` → primary calendar, store
   `calendar_id`.
2. `bitable.v1.app.create` (folder=root) → "包工头的工作台" base, store
   `app_token`.
3. Inside that base, `bitable.v1.app.table.create` for two tables:
   - `action_items` (fields: title, owner [Person field],
     project [Single Select], due_date [Date], status [Single Select:
     todo/doing/done], created_by_meeting [URL], created_at)
   - `meetings` (fields: title, event_id, attendees [Person], project,
     doc_link, created_at)
4. `drive.v1.file.create_folder` → "包工头的文档柜", store
   `folder_token`.
5. `INSERT INTO bot_workspace (id, calendar_id, base_app_token,
   action_items_table_id, meetings_table_id, docs_folder_token,
   bootstrapped_at) VALUES (1, ...)` — single row, primary key always 1.

**Self-healing at runtime**: every write tool, before doing work,
verifies its target resource still exists (`bitable.v1.app.get` etc.,
cached 60s). If a resource has been deleted by a human, re-run the
bootstrap path for the missing piece, update `bot_workspace`, and
post-message the asker: "我的工作台被删了，刚重建了一份在这里：[link]".

---

## 5. The three-phase write pattern

This is the single most important pattern in this spec. **Every write
tool's body follows this shape**, not a generic post-hoc listener.

```python
async def schedule_meeting(args: dict) -> dict[str, Any]:
    message_id = _current_message_id_var          # injected by app.py
    action_type = "schedule_meeting"

    # Phase 1: idempotency check + pending insert
    existing = queries.get_bot_action(message_id, action_type)
    if existing:
        if existing["status"] == "success":
            return _ok(existing["result"])
        if existing["status"] == "pending":
            return _err("a previous identical call is in flight")
        # 'failed' → fall through, retry permitted

    action_id = queries.insert_bot_action_pending(
        message_id=message_id, action_type=action_type, args=args,
    )  # raises UniqueViolation if a concurrent insert beat us

    # Phase 2: do the actual side effect
    try:
        result = await feishu_client.create_calendar_event(...)
    except Exception as e:
        queries.mark_bot_action_failed(action_id, str(e))
        return _err(f"飞书订会失败: {e}")

    # Phase 3: persist terminal state
    queries.mark_bot_action_success(
        action_id, target_id=result["event_id"], result=result,
    )
    return _ok(result)
```

### 5.1 Why this and not a post-hoc listener

| Naive approach | Failure mode |
| --- | --- |
| Write log after agent run finishes | Agent makes 3 tool calls; only 1 logged. |
| Write log only on success | Webhook retry between API success and log write → duplicate side effect. |
| Use Agent SDK's in-context memory | Memory is single-run; webhook retries are different runs entirely. |
| Use existing `_seen_events` LRU only | Process-local, cleared on restart; doesn't cover business-level dedup ("user manually retried the same request"). |

The three-phase pattern gives:

- **Cross-process idempotency**: `UNIQUE (message_id, action_type)`
  is enforced by Postgres regardless of what process or run inserts.
- **Crash safety**: if the process dies between phase 2 and phase 3,
  the row stays `pending`; on retry, we have to reconcile (see §5.3).
- **Audit trail for free**: the same row that locks the action also
  describes what was done and what the result was.

### 5.2 Two-layer dedup, not redundant

`bot/feishu/events.py:_seen_events` is the **transport** dedup: it
stops the same Feishu webhook delivery from being processed twice
within the same process. Cheap, in-memory, ~5 minute window.

`bot_actions(UNIQUE message_id, action_type)` is the **business**
dedup: it stops the same logical action from being executed twice,
including across process restarts and across "user pressed enter
twice on the same prompt" cases.

They cover different failure modes. Both stay.

### 5.3 Reconciling stuck `pending` rows

A row stuck in `pending` for >5 minutes is almost certainly orphaned
(process died mid-call). A simple GC pass marks them `failed` with
`error="reconciled: pending too long"`. We do not auto-retry — the
side effect may or may not have happened, and re-attempting could
create duplicates on the Feishu side. The agent surfaces these to
the user the next time `undo_last_action` or `query_action_items`
runs.

A separate cron/loop is **not** added in MVP; the GC happens lazily
in `get_bot_action` itself (if it returns a row >5min old in pending,
mark it failed before returning). YAGNI.

---

## 6. Schema changes

### 6.1 `bot_workspace` (single-row config)

```sql
-- backend/supabase/migrations/0010_bot_workspace.sql
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
-- No public policy. Service role only.
```

### 6.2 `bot_actions` (idempotency + audit log)

```sql
-- backend/supabase/migrations/0011_bot_actions.sql
CREATE TABLE bot_actions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id   text NOT NULL,                    -- Feishu message that triggered the action
    action_type  text NOT NULL,                    -- 'schedule_meeting' | 'append_action_items' | ...
    status       text NOT NULL CHECK (status IN ('pending','success','failed','undone')),
    args         jsonb NOT NULL,                   -- tool inputs (sanitized)
    target_id    text,                             -- Feishu side ID (event_id, record_id, doc_token)
    target_kind  text,                             -- 'calendar_event' | 'bitable_record' | 'docx'
    result       jsonb,                            -- Feishu response keys we'll need later
    error        text,                             -- failure detail
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, action_type)
);
CREATE INDEX bot_actions_target_idx ON bot_actions (target_kind, target_id);
CREATE INDEX bot_actions_pending_idx ON bot_actions (status, created_at)
  WHERE status = 'pending';
ALTER TABLE bot_actions ENABLE ROW LEVEL SECURITY;
-- Service role only; no end-user policy.
```

`UNIQUE (message_id, action_type)` is the hard idempotency guarantee.
Two concurrent inserts race; one wins, the other gets a violation and
falls through to "read existing row, return its result".

---

## 7. Code file ownership

Every line of code introduced by this spec belongs to exactly one of
these places:

| Concern | Lives in | Existing or new |
| --- | --- | --- |
| Feishu webhook handling | `bot/feishu/events.py`, `bot/app.py` | unchanged |
| `set_current_message_id` injection | `bot/app.py` (call site), `bot/agent/tools.py` (storage) | one new line each |
| Tool schema + LLM-visible behavior | `bot/agent/tools.py` | new tools, three-phase pattern in each |
| Feishu API wrappers (calendar, bitable, docx, contact) | `bot/feishu/client.py` | new methods |
| `bot_actions` / `bot_workspace` SQL | `bot/db/queries.py` | new functions, all via `sb_admin()` |
| Workspace bootstrap script | `bot/scripts/bootstrap_bot_workspace.py` | new file |
| Schema | `backend/supabase/migrations/0010_*.sql`, `0011_*.sql` | new |

Things explicitly NOT changed: `bot/feishu/cards.py`, `bot/agent/runner.py`,
`bot/agent/imaging.py`, `bot/db/client.py`, the existing read tools.

---

## 8. Permission scopes (Feishu Open Platform)

The bot's app needs these scopes added (one-time admin task; without
them everything 401s and no code change matters):

- `im:*` (existing)
- `calendar:calendar` — own calendar mgmt
- `calendar:calendar.event:*` — create/update/delete events
- `calendar:calendar.event.attendee:*` — invite/remove attendees
- `calendar:calendar.freebusy:read` — conflict detection
- `bitable:app` — full read/write on bot's own bases
- `docx:document` — create/edit docs
- `drive:drive` — manage bot's folder
- `contact:user.base:readonly` — resolve names by user
- `contact:contact:readonly` — search organizational directory

A short README section in `bot/README.md` will list these.

---

## 9. System-prompt directives

A new paragraph appended to the agent's system prompt (location:
`bot/agent/runner.py` where the system message is composed):

```
You can now act on Feishu, not just answer questions.

Default behavior: reply with text. Only invoke a write tool when the
user's intent unambiguously matches it: 订会 / 取消会议 / 看日程 →
calendar tools; 记一下 / 写到表里 → action_items tools; 写成文档 /
整理纪要 → create_meeting_doc.

Hard rules:
- Before calling any tool that takes a person, call resolve_people first.
  If it returns ambiguous or unresolved entries, ASK THE USER to
  disambiguate. Never guess.
- All times you pass to schedule_meeting must be RFC3339 with timezone.
  Call today_iso first to learn the asker's timezone.
- If schedule_meeting returns a `conflict`, surface it to the user and
  propose alternative times. Do not retry blindly.
- Never modify Feishu resources you did not create. Cancel/edit only
  things tied to a bot_actions row owned by the bot.
```

---

## 10. Identified omissions and how this spec handles them

A checklist run during brainstorming surfaced 12 issues a write-tool
agent commonly mishandles. For traceability:

| # | Risk | Mitigation in this spec |
| --- | --- | --- |
| 1 | Timezone ambiguity | `today_iso` returns `user_timezone`; system prompt enforces RFC3339+offset |
| 2 | Webhook retry double-action | `bot_actions UNIQUE(message_id, action_type)` |
| 3 | Booking on top of existing meetings | `freebusy.list` pre-check inside `schedule_meeting` |
| 4 | Orphaned half-completed multi-step actions | `bot_actions` audit log + `undo_last_action` |
| 5 | Silent name-resolution failures | `resolve_people` returns `resolved/ambiguous/unresolved` separately |
| 6 | Missing defaults for meeting duration / reminder | 30 min / 15 min, set in tool description |
| 7 | "Which project" missing context | `append_action_items` defaults `project` to most active project in asker's recent turns |
| 8 | Bot's workspace resources deleted by humans | Self-healing re-bootstrap on missing resource |
| 9 | Recurring meetings | Out of scope (§1.3) |
| 10 | Meeting rooms / VC links | Out of scope (§1.3) |
| 11 | Cross-language fuzzy matching | Out of scope; agent re-asks |
| 12 | Doc attachments / images | Out of scope; markdown-only Docx body |

---

## 11. Build sequence

Suggested order (each step independently testable):

1. **Migrations** `0010` + `0011` — schema first, no app changes.
2. **`db/queries.py`** — add the 6 new functions for bot_actions/workspace.
3. **`feishu/client.py`** — wrap calendar/bitable/docx/contact endpoints.
4. **`bootstrap_bot_workspace.py`** — run once against dev env, verify
   the calendar/base/folder appear correctly.
5. **`agent/tools.py`** — add the 9 tools, register on the MCP server.
6. **`app.py`** — inject `message_id` into tools at the same time as
   `conversation_key`.
7. **System prompt** — append §9 directives.
8. **End-to-end smoke test** in a private Feishu group.

Each step touches at most one file. No step depends on a later step's
internals.

---

## 12. Open questions to revisit after MVP usage

- Should `bot_actions` rows be exposed via a "what did the bot do for
  me lately" Feishu card? (Probably yes; trivial extension.)
- Should `action_items` get a Feishu webhook back into the bot when
  someone marks an item done in the Bitable UI? (Two-way sync; later.)
- Do we need per-conversation rate limiting on write tools, the way
  we limit `generate_image`? (Likely yes once a few teams use it.)
