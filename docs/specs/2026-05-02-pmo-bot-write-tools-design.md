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
| Sending DMs as the bot (`send_dm`) | High-blast-radius write; deferred until a draft-then-confirm UX pattern exists. Currently the bot only replies in the conversation it was addressed in. |
| Feishu Tasks integration | `action_items` table in the bot's Bitable subsumes it |
| Cross-language fuzzy name matching | `resolve_people` returns matches; agent reconfirms |
| `lark-cli` shell-out from the daemon | CLI is OAuth-based, not headless-friendly |

### 1.4 Trust model: direct execution + reliable undo

This spec deliberately does NOT introduce a confirmation gate (e.g., a
"Are you sure?" Feishu card button) before write tools execute. Two
reasons:

1. The bot already operates in a high-trust environment (private team
   group / 1:1 DM with a teammate). The user has already typed the
   request; an interstitial confirmation feels redundant for the same
   reason booking flights through a chatbot's confirm-button feels
   redundant when the user just typed "book it".
2. The friction would push users toward typing imperative shorthand
   that the LLM then has to parse twice (once for the request, once for
   the confirmation), instead of letting the model focus on doing the
   work right the first time.

**The cost of this choice**: there is no pre-execution safety net. If
the LLM misinterprets intent, the side effect lands on Feishu before
anyone notices. We accept that cost only because of the post-execution
safety net described next.

**Implication — `undo_last_action` is no longer a "nice to have"**:
without a confirmation gate, this tool *is* the safety net. v1
acceptance criteria for `undo_last_action`:

- Must be implemented, callable, and exposed in the agent's allowlist
  in the same release as `schedule_meeting` / `append_action_items` /
  `create_meeting_doc`. It does not ship later.
- Must work for every other write tool's outputs (one compensating
  call per `action_type`, see §3.9).
- Must be tested end-to-end during the §11 step-9 smoke test before
  the bot is exposed to other groups.
- Must remain usable even when the original message is older than
  `_seen_events` LRU window — i.e., scoped by `chat_id` +
  `sender_open_id`, not `message_id`.

If any of those conditions can't be met for a release, that release
defers the corresponding write tool, not the undo.

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

**Tool description directive**: "Always call this before passing any
datetime to `schedule_meeting` or any time-window argument to
`list_my_meetings` / `query_action_items`. The asker's timezone is the
correct frame for interpreting relative phrases like '下周三 3 点'."

### 3.3 `schedule_meeting`

**Inputs**:
- `title: str`
- `start_time: str` — RFC3339 with timezone, no defaults, no ambiguity.
- `duration_minutes: int = 30`
- `attendee_open_ids: list[str]` — must come from `resolve_people`.
- `description: str = ""`
- `reminder_minutes: int = 15`

**Internal sequence** (the three-phase pattern, see §5):

1. **Phase 1 — Idempotency check + pending insert**: look up
   `bot_actions` by `(message_id, "schedule_meeting")`; on hit return
   the cached result. Otherwise insert a `pending` row keyed on
   `(message_id, action_type)` — UNIQUE constraint serializes concurrent
   retries. **All subsequent steps including freebusy run only after
   this row exists**, so a webhook retry sees `pending` and bails out
   without re-issuing any Feishu calls.
2. **Phase 2 — Freebusy pre-check**: call `calendar.v4.freebusy.list`
   for all attendees over the requested window. If any conflict, mark
   the row `failed` (with `error="conflict"` and the conflict payload
   stored in `result`) and return `{conflict: [{open_id,
   busy_event_summary}]}` to the agent. The agent reasks the user; a
   subsequent retry uses a *new* message_id so a fresh row is created.
3. **Phase 2 — Create event**: `calendar.v4.calendar_event.create`
   against the bot's primary calendar with
   `attendee_ability=can_modify_event`.
4. **Phase 2 — Invite attendees**:
   `calendar.v4.calendar_event.attendee.create_batch`.
5. **Phase 3 — Persist terminal state**: update `bot_actions` to
   `status=success`, store `target_id=event_id`, `target_kind=
   "calendar_event"`, `result={event_id, calendar_id, link, attendees}`.
6. Return event details to the agent.

### 3.4 `cancel_meeting`

**Inputs**: `event_id` OR `last:true` (cancels the most recent
bot-scheduled meeting **in the current conversation**).

Resolution rules:
- If `event_id` is given: look up `bot_actions WHERE target_kind=
  'calendar_event' AND target_id=event_id`. If no row → refuse
  ("only cancel meetings I created"). If row exists but
  `status='undone'` → no-op, return idempotent success.
- If `last:true`: look up `bot_actions WHERE chat_id=<current> AND
  sender_open_id=<current> AND action_type='schedule_meeting' AND
  status='success' ORDER BY created_at DESC LIMIT 1`. **The
  `sender_open_id` filter matters in groups**: without it, user A
  could cancel a meeting user B asked the bot to schedule. Requires
  `(chat_id, sender_open_id)` on `bot_actions` (see §6.2).

Internally follows the three-phase pattern: pending insert keyed on
`(message_id, "cancel_meeting")` → `calendar_event.delete` →
mark `success`, and update the original `schedule_meeting` row's
`status` to `undone` for traceability.

### 3.5 `list_my_meetings`

**Inputs**: `user_open_id`, `since`, `until`.

**Visibility model — read carefully, this is subtle**: a tenant_access_token
under `calendar:calendar.event:read` does NOT see every event on a
user's primary calendar. What it sees depends on (a) whether the bot
is on the attendee list of that event, and (b) whether the user has
shared their calendar with the bot's app via Feishu's calendar-sharing
mechanism. There is no "act as user" path in this spec (no OAuth, see
§1.2).

The tool therefore returns **two separate result sets** so the agent
can be honest about provenance:

```json
{
  "since": "...",
  "until": "...",
  "bot_known_events": [
    // Events the bot itself scheduled (joined from bot_actions WHERE
    // action_type='schedule_meeting' AND attendees ⊇ {user_open_id}).
    // These are 100% complete from the bot's POV.
  ],
  "user_calendar_events": [
    // Events from calendar.v4.calendar_event.list against the user's
    // primary calendar. May be empty (no sharing), partial (only
    // events bot is invited to), or complete (calendar shared with
    // bot app).
    {"event_id": "...", "title": "...", "source": "user_primary"}
  ],
  "visibility_note": "string the agent should pass to the user when
  the union looks suspiciously sparse — e.g., 'I can only see meetings
  I scheduled or that you've shared your calendar with me on. If you
  expect more, share your Feishu calendar with the @包工头 app.'"
}
```

**Why two sets, not a merged dedup**: when the user asks "我下午有啥
会"，the agent needs to reason about *what it can know* before
committing to "你下午没会" — which would be a confidently wrong answer
if visibility is partial. By keeping the sources separate, the agent
can reply truthfully ("我看到的有 A, B；如果还有别的，可能是我没权限
看到") instead of pretending omniscience.

**No write side effects**; not subject to the three-phase pattern.

### 3.6 `append_action_items`

**Inputs**:
- `items: [{title: str, owner_open_id: str, due_date?: str, project?: str}]`
- `meeting_event_id?: str` (optional link to an event in the same conversation)

Writes one record per item to the `action_items` table in the bot's
Bitable base. Three-phase pattern.

**Default-project resolution** (per item with no `project` provided):

1. Look at the asker's `turns` rows in the **last 7 days**, group by
   `project_root`, take the one with highest count.
2. **Tie-break**: prefer the project whose latest turn is most recent.
3. **Threshold**: if the top project has fewer than 3 turns in the
   window, treat as no signal — the **whole tool call** halts before
   writing anything (see "ambiguous flow" below).

**Ambiguous flow** (no auto-write, no orphan records):

When **any** item lacks a project AND auto-resolution can't pick one
confidently, the tool returns **without writing anything**:

```json
{
  "needs_project": true,
  "items_pending": [
    {"title": "review API design", "owner_open_id": "ou_xxx"},
    {"title": "send timeline to client", "owner_open_id": "ou_yyy"}
  ],
  "auto_suggestion": "/Users/a/Desktop/pmo_agent",
  "auto_suggestion_confidence": "low",
  "agent_directive": "Ask the user which project these belong to. Do not call append_action_items again until you have an explicit project string."
}
```

The agent reasks the user. When the user answers, the agent calls
`append_action_items` *again* with `project` populated explicitly on
each item. Because the call has the same `(chat_id, sender_open_id,
action_type, canonical_args)` shape as the first call **only if the
user's intent is unchanged** (and `project` is part of canonical_args,
so a different project produces a different `logical_key`), there is
no logical_key collision — both calls execute cleanly.

**Why not write-then-update**: an earlier draft proposed writing rows
with `project=null` and asking the agent to update them later. That
required either a new `update_action_items` tool (more surface) or
LLM-driven `undo + re-append` (fragile, two side effects per
correction). Halting at the boundary is simpler: one ask, one write,
no orphan rows.

**Return shape on success** (project resolved, items written):

```json
{
  "records": [
    {
      "record_id": "rec_xxx",
      "title": "...",
      "project_used": "/Users/a/Desktop/vibelive",
      "project_source": "user_explicit" | "auto_recent_turns"
    }
  ]
}
```

`project_source: "ambiguous"` is no longer a possible value — the
ambiguous case never reaches the write path.

### 3.7 `query_action_items`

**Inputs**: any combination of `owner_open_id`, `project`, `status`,
`since`, `until`.

Reads from the `action_items` table; no write side effects.

### 3.8 `create_meeting_doc`

**Inputs**:
- `title: str`
- `markdown_body: str` — markdown source the agent produced.
- `meeting_event_id?: str`

Three-phase pattern. Creates a Docx in the bot's "文档柜" folder, returns
`{doc_token, url}`. The agent embeds the link in its reply.

**Implementation note — Markdown to Docx**: the standard
`docx.v1.document.create` endpoint creates an **empty** document and
does NOT accept Markdown directly. Two viable paths, to confirm during
implementation:

- **Path A (preferred)**: `drive.v1.import_tasks.create` with
  `type="docx"`, `file_extension="md"`, body=Markdown bytes. This is
  Feishu's documented Markdown import flow, async — you poll
  `import_tasks.get` until done, then receive the `doc_token`.
- **Path B (fallback)**: `docx.v1.document.create` (empty doc), then
  parse Markdown into Docx blocks ourselves and call
  `docx.v1.document.block.children.create` to append. More code, no
  async polling.

Pick A first; fall back to B only if import permissions can't be
granted in production. The choice does not affect this tool's
**interface** — only its body.

### 3.9 `undo_last_action`

**Inputs**:
- `target` (one of):
  - `last_for_me: true` — undo the most recent `success` row in the
    current conversation **that the current asker created**. Scoped by
    `(chat_id, sender_open_id)`, see §6.2. The earlier `last_in_chat`
    name is renamed to make the per-asker scope explicit; in groups,
    user A cannot undo user B's actions through this tool.
  - `action_id: str` — explicit `bot_actions.id` to undo (used when the
    agent has just shown the user a list and they pointed to one).
    Allowed even when the asker isn't the original creator, IF the row
    is in the same `chat_id` — useful when the team explicitly wants
    "anyone in the room can undo the bot's last move" via direct
    reference. The agent surfaces the original sender's name in its
    confirmation reply.
  - `target_id: str` + `target_kind: str` — undo by Feishu-side ID
    (e.g. user pasted an event link). Same chat_id constraint applies.

The "lookup most recent for the same message_id" semantic from earlier
drafts is removed: `UNIQUE (message_id, action_type)` means a single
message_id has at most one row per action_type, so "most recent for
message" is ambiguous when an utterance triggered multiple action
types. Conversation scope (`chat_id`) is the right unit.

Dispatches on `action_type`:
- `schedule_meeting` → `calendar_event.delete`
- `append_action_items` → `bitable.app.table.record.batch_delete`
  (one record_id per appended item, all stored in `result.record_ids`)
- `create_meeting_doc` → `drive.v1.file.delete`

Marks the source row `status=undone`. Records its own `undo_last_action`
row with `target_id=<original action_id>` for traceability.

Idempotent: calling it on an already-`undone` row is a no-op success.

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

**Concurrency**: re-bootstrap is serialized through the same
`bot_actions` UNIQUE-row mechanism the rest of the spec uses for
idempotency, **not** a Postgres advisory lock. Reason: the bot's DB
layer (`bot/db/client.py`) only uses Supabase REST clients; it does
not hold a direct `psycopg` connection, so we can't issue
`pg_advisory_xact_lock` from app code without adding a new dependency.

Mechanism: two distinct row roles — **lock** and **audit** — never
the same row.

**Acquire the lock** (transient, deleted on release):

```sql
-- Lock row: exists at most once, lifecycle = "a bootstrap is in
-- flight". Released by DELETE, NOT by transitioning status — see
-- "Why DELETE, not UPDATE" below.
INSERT INTO bot_actions
  (message_id, chat_id, sender_open_id, logical_key,
   action_type, status, args)
VALUES
  ('__bootstrap_lock__', '__system__', '__system__', '__bootstrap_lock__',
   'bootstrap_workspace_lock', 'pending', '{}'::jsonb)
ON CONFLICT (message_id, action_type) DO NOTHING
RETURNING id;
```

**Audit the work** (permanent, separate row per call):

```sql
-- Each bootstrap attempt also writes its own audit row with a unique
-- message_id (e.g. timestamp + random) so the history accumulates
-- and stays queryable. logical_key here is just the same unique
-- message_id (it's never used for dedup — bootstrap is intentionally
-- not idempotent across re-runs; we want every attempt logged).
INSERT INTO bot_actions
  (message_id, chat_id, sender_open_id, logical_key,
   action_type, status, args, target_kind)
VALUES
  ('bootstrap-' || $timestamp || '-' || $random_suffix,
   '__system__', '__system__',
   'bootstrap-' || $timestamp || '-' || $random_suffix,
   'bootstrap_workspace', 'pending', $args, 'workspace_bootstrap')
RETURNING id;
```

**Flow**:

- If the lock-INSERT returned a row → we own it. Write the audit row
  (`bootstrap_workspace`, `pending`). Run the bootstrap substeps.
  On success: mark the audit row `success` AND **DELETE the lock row**.
  On failure: mark the audit row `failed`, DELETE the lock row anyway
  (so the next caller can retry).
- If the lock-INSERT returned 0 rows → someone else owns it. Poll
  `get_bot_action('__bootstrap_lock__', 'bootstrap_workspace_lock')`
  every 500ms until the row is GONE (i.e. the holder finished and
  released). Then re-read `bot_workspace` and proceed.

**Why DELETE, not UPDATE-to-`success`** (the key v3-era bug, caught
in iteration 4): the lock row uses
`UNIQUE (message_id='__bootstrap_lock__', action_type='bootstrap_workspace_lock')`.
If we left a `success` row sitting there forever, the **next**
re-bootstrap (when a human deletes the workspace again) would hit
`ON CONFLICT DO NOTHING` and **silently no-op** — the conflicting
row already exists. Then it would see `status='success'` and assume
"someone else just rebuilt it", read stale `bot_workspace`, and break.
Deleting the lock on release ensures every fresh bootstrap can
acquire afresh. The audit row remains intact in a separate row, so
no history is lost.

**Stuck-lock recovery**: if the lock row stays `pending` >5min, the
holder process likely crashed. Any waiter that observes `created_at >
5 min ago` proactively `DELETE`s the row (with a `WHERE
created_at < now() - interval '5 minutes' AND status='pending'` guard
to avoid TOCTOU) and retries the acquire. Recovery time is bounded by
the polling interval, not by an external GC pass.

This shape (separate lock row + separate audit row, lock released by
DELETE) is the standard "advisory lock via UNIQUE row" pattern in
Postgres. It reuses the table the spec already requires, doesn't add
a `psycopg` dependency, and is debuggable — `SELECT * FROM
bot_actions WHERE action_type='bootstrap_workspace_lock'` shows the
current holder, if any.

**Data loss is real**: when a resource is deleted by a human, the
records inside it (e.g. previously-written `action_items` rows in the
old base) are **gone**. Self-healing rebuilds the container, not the
contents. The post-message warning to the asker exists precisely so
users can decide whether to recreate critical entries by hand. We
intentionally do not attempt to back up Bitable contents to Postgres
in MVP — that is a future-work item if this becomes a real problem.

---

## 5. The three-phase write pattern

This is the single most important pattern in this spec. **Every write
tool's body follows this shape**, not a generic post-hoc listener.

### 5.0 Per-run context propagation (per-`_PooledClient` closure, NOT contextvars)

Existing code uses a module-level `_current_conversation_key_var`
(`bot/agent/tools.py:29-31`) set via `set_current_conversation` before
each agent run. That works today because agent runs are serialized
per-conversation (`bot/agent/runner.py` slot lock), and there is only
one mutable field. It does **not** generalize to multiple fields
(`message_id`, `chat_id`, `sender_open_id`) and multiple concurrent
conversations.

#### Why `contextvars.ContextVar` does NOT work here

An earlier draft of this spec used `contextvars.ContextVar`, on the
assumption that each `asyncio.create_task(_handle_message(...))` carries
its own context. **That assumption is wrong for this SDK**. Inspection
of the installed `claude-agent-sdk`:

- `ClaudeSDKClient.connect()` starts a long-lived reader task once and
  reuses it for every subsequent `query()`
  (`claude_agent_sdk/client.py:171`, `_query.start()` /
  `_query.initialize()`).
- Tool calls arrive as MCP `control_request` messages on that reader
  task; the reader dispatches each one via
  `self._tg.start_soon(self._handle_control_request, request)`
  (`_internal/query.py:196`).
- `start_soon` (anyio's task-spawn primitive) does **not** propagate
  the caller's `contextvars.Context` automatically the way
  `asyncio.create_task` does. Even if it did, the caller here is the
  reader task — not the original `_handle_message` task — so the
  message_id we set on the webhook handler's frame would never reach
  the tool body.
- `tools/call` is dispatched by the MCP server's request handler
  (`_internal/query.py:475`). At that point we're several `start_soon`
  hops away from the webhook task that wanted to inject context.

Result: any ContextVar set by `_handle_message` would be either
**invisible** (never copied to the reader-spawned task) or **stale**
(carrying values from whichever conversation last set them), and the
pooled-client architecture makes the staleness case dominant.

#### The actual mechanism: a mutable struct owned by `_PooledClient`

Each pooled client gets one `RequestContext` instance. `build_pmo_mcp`
becomes a factory that takes the context and returns an MCP server
whose tool implementations close over it. `_handle_message` mutates
the context's fields **before** calling `client.query()`, while
holding `slot.lock` so no other request can interleave.

```python
# bot/agent/tools.py
from dataclasses import dataclass

@dataclass
class RequestContext:
    """Mutable per-pooled-client request scope.

    Mutated by app.py before every client.query() call, while holding
    the pooled client's lock. Read by the tool closures via the same
    object reference. No global state, no contextvars; the dataclass
    instance lives as long as the _PooledClient that owns it.
    """
    message_id: str = ""
    chat_id: str = ""
    sender_open_id: str = ""
    conversation_key: str = ""


def build_pmo_mcp(ctx: RequestContext):
    """Factory: returns an MCP server whose tools see `ctx` by closure.

    Called once per _PooledClient, at client construction time, and
    passed in via ClaudeAgentOptions(mcp_servers=...).
    """

    @tool("schedule_meeting", "...", {...})
    async def schedule_meeting(args: dict) -> dict:
        # Closure captures `ctx` itself, not its current value — so
        # every invocation sees whatever app.py last wrote.
        message_id = ctx.message_id
        chat_id = ctx.chat_id
        sender_open_id = ctx.sender_open_id
        ...

    # ... other tools ...

    return create_sdk_mcp_server(
        name="pmo", version="0.1.0",
        tools=[schedule_meeting, ..., resolve_people, append_action_items, ...],
    )
```

```python
# bot/agent/runner.py
@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    ctx: RequestContext = field(default_factory=RequestContext)
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

async def _get_client(conversation_key: str) -> _PooledClient:
    async with _pool_lock:
        slot = _pool.get(conversation_key)
        if slot is None:
            ctx = RequestContext()  # one ctx for this pooled client's lifetime
            options = ClaudeAgentOptions(
                ...,
                mcp_servers={"pmo": build_pmo_mcp(ctx)},  # closure over ctx
                ...,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            slot = _PooledClient(client=client, ctx=ctx)
            _pool[conversation_key] = slot
        ...
        return slot
```

```python
# bot/app.py — inside _handle_message, after acquiring slot.lock
slot = await agent_runner._get_client(conversation_key)
async with slot.lock:
    slot.ctx.message_id = ev.message_id
    slot.ctx.chat_id = ev.chat_id
    slot.ctx.sender_open_id = ev.sender_open_id
    slot.ctx.conversation_key = conversation_key
    # ... then call into the agent ...
```

#### Why this is safe

- **Per-conversation isolation**: each conversation has its own
  `_PooledClient`, hence its own `ctx`. Two conversations running in
  parallel never share state.
- **Within a conversation, requests are FIFO** (`slot.lock` already
  enforces this). One agent run completes before the next begins, so
  the next mutation of `ctx` only happens after all of the current
  run's tool calls have finished.
- **No reliance on async-task context propagation**. The closure binds
  to the `RequestContext` *object*, not to any task-local state. Tools
  read it by attribute access; whatever value was set under the lock
  is exactly what the tool sees.

The existing call site at `tools.py:29-31` (`_current_conversation_key_var`
+ `set_current_conversation`) is removed and replaced. `agent/imaging.py`
itself does **not** change — it already takes `conversation_key` as a
kwarg. The change is at the caller side: the `generate_image` tool body
inside `build_pmo_mcp(ctx)` now passes `ctx.conversation_key` to that
existing kwarg instead of reading the old module global.

### 5.1 Tool body skeleton

The skeleton runs inside the `build_pmo_mcp(ctx)` factory's closure (§5.0),
so `ctx` is captured by reference — no parameter passing, no global state.

```python
async def schedule_meeting(args: dict) -> dict[str, Any]:
    # ctx is captured by closure from build_pmo_mcp(ctx); see §5.0.
    message_id = ctx.message_id
    chat_id = ctx.chat_id
    sender_open_id = ctx.sender_open_id
    action_type = "schedule_meeting"

    # Compute logical_key for repeat-utterance dedup (see §5.2 / §6.2).
    logical_key = queries.compute_logical_key(
        chat_id=chat_id,
        sender_open_id=sender_open_id,
        action_type=action_type,
        canonical_args=args,
    )

    # Phase 0: short-window logical dedup
    # Runs *before* Phase 1a (message_id check) deliberately: if a
    # prior success exists for this logical_key, the side effect
    # already happened. Whether the *current* message_id has a row
    # (failed, pending, none) is moot — re-firing the API call would
    # produce a duplicate.
    recent = queries.get_recent_success_by_logical_key(logical_key, window_seconds=60)
    if recent:
        return _ok({**recent["result"], "deduplicated_from_logical_key": True})

    # Phase 1a: idempotency check
    existing = queries.get_bot_action(message_id, action_type)
    if existing:
        if existing["status"] == "success":
            return _ok(existing["result"])
        if existing["status"] == "pending":
            return _err("a previous identical call is in flight")
        if existing["status"] == "reconciled_unknown":
            # GC'd row — we don't know if Feishu side succeeded.
            return _err(
                "an earlier identical call was orphaned; please verify "
                "in your Feishu calendar and ask me again with a fresh "
                "instruction if it didn't happen"
            )
        if existing["status"] == "failed":
            # Retry: claim the existing row by transitioning it back to
            # pending. update_for_retry uses an atomic UPDATE ... WHERE
            # status='failed' RETURNING id; if it returns 0 rows another
            # caller already claimed it, fall through to read again.
            action_id = queries.update_for_retry(
                existing["id"], new_args=args,
            )
            if action_id is None:
                return _err("a concurrent retry won the race; try again in a moment")
        elif existing["status"] == "undone":
            return _err("this action has been undone; submit as a fresh request")
    else:
        # Phase 1b: pending insert (first-time path)
        try:
            action_id = queries.insert_bot_action_pending(
                message_id=message_id,
                chat_id=chat_id,
                sender_open_id=sender_open_id,
                action_type=action_type,
                args=args,
                logical_key=logical_key,
            )
        except queries.UniqueViolation:
            # Concurrent insert beat us — re-read and dispatch on its status.
            existing = queries.get_bot_action(message_id, action_type)
            if existing and existing["status"] == "success":
                return _ok(existing["result"])
            return _err("a concurrent call is in flight; try again in a moment")

    # Phase 2: do the actual side effect (freebusy pre-check, create
    # event, invite attendees — see §3.3 for schedule_meeting specifics)
    try:
        result = await feishu_client.create_calendar_event(...)
    except Exception as e:
        queries.mark_bot_action_failed(action_id, str(e))
        return _err(f"飞书订会失败: {e}")

    # Phase 3: persist terminal state
    queries.mark_bot_action_success(
        action_id,
        target_id=result["event_id"],
        target_kind="calendar_event",
        result=result,
    )
    return _ok(result)
```

**Why update-in-place instead of inserting a fresh retry row**: the
`UNIQUE (message_id, action_type)` constraint deliberately prevents
two rows for the same logical action — that's the whole basis of
idempotency. Retrying a `failed` row therefore must be an UPDATE, not
an INSERT. The `attempt_count` column (§6.2) is bumped on each retry
so the audit trail still shows how many tries it took.

`update_for_retry`'s SQL:

```sql
UPDATE bot_actions
   SET status='pending',
       attempt_count = attempt_count + 1,
       args = $new_args,
       error = NULL,
       updated_at = now()
 WHERE id = $id AND status = 'failed'
 RETURNING id;
```

The `WHERE status='failed'` clause is the lock: only one concurrent
caller's UPDATE returns a row, others get 0 and bail.

### 5.1bis Why this and not a post-hoc listener

| Naive approach | Failure mode |
| --- | --- |
| Write log after agent run finishes | Agent makes 3 tool calls; only 1 logged. |
| Write log only on success | Webhook retry between API success and log write → duplicate side effect. |
| Use Agent SDK's in-context memory | Memory is single-run; webhook retries are different runs entirely. |
| Use existing `_seen_events` LRU only | Process-local, cleared on restart; doesn't cover business-level dedup. |

The three-phase pattern gives:

- **Cross-process idempotency**: `UNIQUE (message_id, action_type)`
  is enforced by Postgres regardless of what process or run inserts.
- **Crash safety**: if the process dies between phase 2 and phase 3,
  the row stays `pending`; on retry, we have to reconcile (see §5.3).
- **Audit trail for free**: the same row that locks the action also
  describes what was done and what the result was.

### 5.2 Three-layer dedup

| Layer | Mechanism | Covers | Does NOT cover |
| --- | --- | --- | --- |
| **Transport** | `bot/feishu/events.py:_seen_events` LRU (`event_id`) | Feishu webhook redeliveries within the same process | Process restarts; user retypes the same instruction |
| **Per-message idempotency** | `bot_actions UNIQUE(message_id, action_type)` | The same Feishu `message_id` reaching us twice (cross-process, cross-restart) | A *new* user message that says the same thing — different `message_id`, no constraint hit |
| **Logical short-window dedup** | `bot_actions.logical_key` + 60-second look-back (§6.2) | "User typed the request again 3 seconds later because nothing visibly happened" — same chat, same sender, same canonical args | Anything older than the window; intentional re-issuance later |

`logical_key` is a stable hash over `(chat_id, sender_open_id,
action_type, canonical_args)` — the same parameters that uniquely
identify a logical request from a human's POV. The tool body's Phase
0 (§5.1) checks for a `success` row with the same `logical_key`
within 60s and returns its result instead of firing the side effect
again.

**Why a window, not another UNIQUE constraint**: the user might
*legitimately* want to schedule "another 30-minute meeting with
albert about the same topic" later in the day. Hard-blocking forever
would surprise them. 60 seconds catches the "I pressed enter twice"
case without trapping legitimate repeat scheduling.

**Honesty about coverage**: in the no-confirmation-gate trust model
(§1.4), **truly intentional re-issuance more than 60 seconds apart
will execute twice**. We accept that — the cost of intercepting it is
asking the user "did you mean to schedule again?" on every legitimate
follow-up, which would erode the very fluidity §1.4 is paying for.

### 5.3 Reconciling stuck `pending` rows

A row stuck in `pending` for >5 minutes is almost certainly orphaned
(process died mid-call). A simple GC pass marks them
`reconciled_unknown` (a distinct status, **not** `failed`) with
`error="reconciled: pending too long"`.

The distinction matters: `failed` means "we know the Feishu call
errored, retry is safe". `reconciled_unknown` means "we don't know
if the Feishu side succeeded — retrying could create a duplicate".

The tool skeleton (§5.1) treats `reconciled_unknown` as a hard stop:
return an error to the agent that explains the ambiguity and asks the
user to verify on the Feishu side before issuing a fresh request.
This deliberately surfaces a rare case to the user rather than silently
risking a duplicate meeting.

A separate cron/loop is **not** added in MVP; the GC happens lazily
in `get_bot_action` itself (if it returns a row >5min old in pending,
mark it `reconciled_unknown` before returning). YAGNI.

The `status` CHECK constraint in §6.2 includes `reconciled_unknown` for
this reason.

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

-- gen_random_uuid() lives in pgcrypto. Supabase usually has it
-- pre-installed, but be explicit so this migration is portable.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE bot_actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      text NOT NULL,                 -- Feishu message that triggered the action
    chat_id         text NOT NULL,                 -- Feishu chat id (group / p2p) — scope for "last in conversation"
    sender_open_id  text NOT NULL,                 -- Who asked the bot to do this — for per-user undo scoping
    logical_key     text NOT NULL,                 -- hash(chat_id|sender_open_id|action_type|canonical_args), see §5.2
    attempt_count   int  NOT NULL DEFAULT 1,       -- bumped on retry-after-failure (see §5.1)
    action_type     text NOT NULL,                 -- 'schedule_meeting' | 'append_action_items' | ...
    status          text NOT NULL CHECK (
                      status IN ('pending','success','failed','undone','reconciled_unknown')
                    ),
    args            jsonb NOT NULL,                -- tool inputs (sanitized)
    target_id       text,                          -- Feishu side ID (event_id, record_id, doc_token)
    target_kind     text,                          -- 'calendar_event' | 'bitable_record' | 'docx' | 'workspace_bootstrap'
    result          jsonb,                         -- Feishu response keys we'll need later
    error           text,                          -- failure detail
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (message_id, action_type)
);
CREATE INDEX bot_actions_target_idx ON bot_actions (target_kind, target_id);
CREATE INDEX bot_actions_pending_idx ON bot_actions (status, created_at)
  WHERE status = 'pending';
-- "last bot action this user requested in this chat" — used by
-- cancel_meeting(last:true) and undo_last_action(last_for_me:true).
-- Per-sender scoping prevents user A from undoing user B's action.
CREATE INDEX bot_actions_chat_sender_recent_idx
  ON bot_actions (chat_id, sender_open_id, created_at DESC);
-- Logical-key dedup: "did this user just say the same thing?"
-- Partial index because we only ever look up succeeded rows here.
CREATE INDEX bot_actions_logical_recent_idx
  ON bot_actions (logical_key, created_at DESC)
  WHERE status = 'success';
ALTER TABLE bot_actions ENABLE ROW LEVEL SECURITY;
-- Service role only; no end-user policy.
```

`logical_key` canonicalization: `args` is canonicalized to a sorted-
keys JSON before hashing, so `{a:1,b:2}` and `{b:2,a:1}` produce the
same key. For `schedule_meeting`, `start_time` is normalized to UTC
before hashing so equivalent `+08:00` / `+00:00` representations
collide. Implementation in `db/queries.py:compute_logical_key`.

`UNIQUE (message_id, action_type)` is the hard idempotency guarantee.
Two concurrent inserts race; one wins, the other gets a violation and
falls through to "read existing row, return its result".

`chat_id` is **required** (NOT NULL) because both `cancel_meeting`
(§3.4) and `undo_last_action` (§3.9) need to scope "last action" to
the conversation that issued the request. Without it the tools would
either operate globally (wrong) or fall back to message_id-only
(ambiguous when one message triggered multiple action types).

`sender_open_id` is **required** for the same reason but on a
different axis: in a group chat, two different users might both ask
the bot to schedule meetings. Without per-user scoping, user A typing
"取消刚才那个会" would silently cancel user B's meeting if it was
created more recently. This matches the per-conversation FIFO behavior
already in `bot/app.py:128` where `conversation_key` is
`{chat_id}:{sender_open_id}`.

---

## 7. Code file ownership

Every line of code introduced by this spec belongs to exactly one of
these places:

| Concern | Lives in | Existing or new |
| --- | --- | --- |
| Feishu webhook handling | `bot/feishu/events.py`, `bot/app.py` | unchanged |
| Per-run context (`message_id`, `chat_id`, `sender_open_id`, `conversation_key`) via `RequestContext` dataclass owned by each `_PooledClient`, captured by closure in `build_pmo_mcp(ctx)` (see §5.0) | `bot/agent/tools.py` (`RequestContext` definition + factory), `bot/agent/runner.py` (one ctx per `_PooledClient`), `bot/app.py` (mutate `slot.ctx.*` under `slot.lock`) | new dataclass, factory pattern in `build_pmo_mcp`, existing `set_current_conversation` removed entirely |
| Tool schema + LLM-visible behavior | `bot/agent/tools.py` | new tools, three-phase pattern in each |
| **Agent SDK `allowed_tools` whitelist** (`bot/agent/runner.py:179`) | `bot/agent/runner.py` | **must add** `mcp__pmo__resolve_people`, `mcp__pmo__schedule_meeting`, `mcp__pmo__cancel_meeting`, `mcp__pmo__list_my_meetings`, `mcp__pmo__append_action_items`, `mcp__pmo__query_action_items`, `mcp__pmo__create_meeting_doc`, `mcp__pmo__undo_last_action` to the existing list. Without this, the SDK filters the new tools out and the LLM never sees them. **Discovered during review iteration 3** — a previous draft incorrectly claimed runner.py was unchanged. |
| Agent SDK system prompt | `bot/agent/runner.py` (`SYSTEM_PROMPT` constant) | append §9 directives |
| Feishu API wrappers (calendar, bitable, docx, contact) | `bot/feishu/client.py` | new methods |
| `bot_actions` / `bot_workspace` SQL | `bot/db/queries.py` | new functions, all via `sb_admin()` |
| Workspace bootstrap script | `bot/scripts/bootstrap_bot_workspace.py` | new file (and the `bot/scripts/` directory itself, created in step 4 of §11) |
| Schema | `backend/supabase/migrations/0010_*.sql`, `0011_*.sql` | new |

Things explicitly NOT changed: `bot/feishu/cards.py`, `bot/db/client.py`,
the existing read tools.

No imaging.py signature change is needed: `imaging.generate_and_upload`
already takes `conversation_key` as a kwarg. The actual change is in
the `generate_image` tool body inside `build_pmo_mcp(ctx)`, which now
passes `ctx.conversation_key` to that existing kwarg instead of
reading the removed module global.

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
- When list_my_meetings returns a non-empty `visibility_note` or its
  `user_calendar_events` set looks suspiciously sparse, surface that
  caveat to the user. Never assert "你没有会" without acknowledging
  the visibility limitation; the bot can only see meetings it
  scheduled or events on calendars the user has shared with the
  @包工头 app.
```

---

## 10. Identified omissions and how this spec handles them

A checklist run during brainstorming surfaced 12 issues a write-tool
agent commonly mishandles. For traceability:

| # | Risk | Mitigation in this spec |
| --- | --- | --- |
| 1 | Timezone ambiguity | `today_iso` returns `user_timezone` (§3.2); system prompt enforces RFC3339+offset (§9) |
| 2 | Webhook retry double-action | `bot_actions UNIQUE(message_id, action_type)` (§6.2) |
| 3 | Booking on top of existing meetings | `freebusy.list` pre-check **after** pending insert (§3.3 phase 2) |
| 4 | Orphaned half-completed multi-step actions | `bot_actions` audit log + `undo_last_action` (§3.9) |
| 5 | Silent name-resolution failures | `resolve_people` returns `resolved/ambiguous/unresolved` separately (§3.1) |
| 6 | Missing defaults for meeting duration / reminder | 30 min / 15 min, set in tool description (§3.3) |
| 7 | "Which project" missing context | When auto-resolution can't pick confidently, `append_action_items` returns `needs_project: true` and writes **nothing** — agent reasks user, user answers, agent retries with explicit project (§3.6). No orphan rows, no update tool needed. |
| 8 | Bot's workspace resources deleted by humans | Self-healing re-bootstrap behind sentinel-row lock (§4); orphan acknowledgement |
| 9 | Recurring meetings | Out of scope (§1.3) |
| 10 | Meeting rooms / VC links | Out of scope (§1.3) |
| 11 | Cross-language fuzzy matching | Out of scope; agent re-asks (§3.1) |
| 12 | Doc attachments / images | Out of scope; markdown-only Docx body (§3.8) |
| 13 | Per-task isolation of `message_id`/`chat_id` for concurrent runs | Per-`_PooledClient` `RequestContext` dataclass captured by closure in `build_pmo_mcp(ctx)` (§5.0). `contextvars.ContextVar` was tried in v3 and rejected: claude-agent-sdk dispatches tool calls from a long-lived reader task via `start_soon`, breaking ContextVar inheritance. |
| 14 | Stuck `pending` rows after process crash | Lazy GC marks them `reconciled_unknown`, surfaced to user, never silently retried (§5.3) |
| 15 | Concurrent re-bootstrap creating duplicate workspace resources | Lock row + audit row are now **separate** in `bot_actions` (§4); lock released by **DELETE** so subsequent rebuilds can reacquire. (v3-era "release by UPDATE-to-success" was a real bug — the row would persist forever and block all future bootstraps.) |
| 16 | "Last meeting in conversation" undefined without conversation scope | `chat_id` column on `bot_actions` + `(chat_id, sender_open_id, created_at DESC)` index (§6.2) |
| 17 | Markdown-to-Docx assumption unverified | Two-path implementation note, A preferred (§3.8) |
| 18 | Cross-user undo leak in groups (user A undoes user B's action) | `sender_open_id` column on `bot_actions`; `undo_last_action(last_for_me)` and `cancel_meeting(last)` filter on `(chat_id, sender_open_id)` (§3.4, §3.9, §6.2) |
| 19 | `failed`-row retry collides with `UNIQUE(message_id, action_type)` | UPDATE-in-place via `update_for_retry` with `attempt_count`; never INSERT a duplicate row (§5.1) |
| 20 | New MCP tools invisible to LLM because of SDK whitelist (`bot/agent/runner.py:179`) | §7 explicitly requires editing `allowed_tools`; §11 step 6 blocks step 8 (smoke test) on this edit |
| 21 | `list_my_meetings` cannot truthfully claim full visibility under tenant token | Tool returns `bot_known_events` and `user_calendar_events` separately + `visibility_note` so the agent never falsely asserts "you have no meetings" (§3.5) |
| 22 | Scope name typos / drift between Feishu API versions | §11 step 0 runs `lark-cli` schema check before applying scopes in admin console |
| 23 | No pre-execution confirmation gate for write actions | Accepted explicitly (§1.4); `undo_last_action` is elevated to safety-critical with v1 acceptance criteria |
| 24 | `send_dm` (DM-as-bot) was raised by Codex review as missing | Marked out-of-scope in §1.3 with stated reason; deferred until draft-then-confirm UX is designed |
| 25 | User retypes the same instruction → bot fires duplicate side effect (UNIQUE on `message_id` does NOT cover this) | `logical_key` column on `bot_actions` + 60-second look-back in tool body Phase 0 (§5.1, §5.2, §6.2). Re-issuance more than 60s apart still fires twice — explicitly accepted in §5.2. |
| 26 | `contextvars.ContextVar` does not survive claude-agent-sdk's tool-call dispatch path | Per-`_PooledClient` `RequestContext` dataclass + closure-based `build_pmo_mcp(ctx)` factory (§5.0). Verified by inspecting `claude_agent_sdk/_internal/query.py:196` `start_soon` semantics. |
| 27 | Bootstrap lock row left in `success` state forever, blocking all future rebuilds (v3 bug) | Release the lock by `DELETE`, audit by separate row (§4). Caught in iter-4 review. |
| 28 | `append_action_items` ambiguous-project flow wrote orphan rows in v3 | Refactored to halt-and-ask: returns `needs_project` without writing (§3.6). Caught in iter-4 review. |

---

## 11. Build sequence

Suggested order. Each step is independently testable; later steps
depend on earlier ones, but no earlier step depends on a later one's
internals.

0. **Verify Feishu scope names with `lark-cli`** — install
   `@larksuite/cli` locally, run `lark-cli auth scopes --help` (or
   inspect skill manifests like
   `https://raw.githubusercontent.com/larksuite/cli/main/skills/lark-calendar/SKILL.md`)
   to confirm the exact scope strings the API expects (e.g.
   `calendar:calendar.free_busy:read` vs `calendar:calendar.freebusy:read`).
   Then add the verified scope set in the Feishu open-platform admin
   console and publish a new app version. **All later steps assume
   scopes are live**; without them every Feishu API call 401s.
1. **Migrations** `0010` + `0011` — schema first, no app changes.
2. **`db/queries.py`** — add the new functions for `bot_actions`
   (`get_bot_action`, `insert_bot_action_pending`, `update_for_retry`,
   `mark_bot_action_success`, `mark_bot_action_failed`,
   `mark_bot_action_undone`, `last_bot_action_for_sender_in_chat`,
   `compute_logical_key`, `get_recent_success_by_logical_key`,
   `acquire_bootstrap_lock`, `release_bootstrap_lock`) and
   `bot_workspace` (`get_bot_workspace`, `update_bot_workspace`).
3. **`feishu/client.py`** — wrap calendar/bitable/docx/contact endpoints.
4. **Create `bot/scripts/` directory + `bootstrap_bot_workspace.py`** —
   run once against dev env, verify the calendar/base/folder appear
   correctly. Re-runnable: detects existing workspace row and exits.
5. **Per-pooled-client `RequestContext` refactor** — touches three
   files; ship and bake **before** any new write tools land:
   - `bot/agent/tools.py`: define `RequestContext` dataclass; convert
     `build_pmo_mcp` from a no-arg helper to a factory `build_pmo_mcp(ctx)`
     whose tool implementations close over `ctx`. Remove the old
     `_current_conversation_key_var` global and `set_current_conversation`.
   - `bot/agent/runner.py`: each `_PooledClient` gets a fresh
     `RequestContext` at construction and passes it into
     `build_pmo_mcp(ctx)`. Add the new tools' names to `allowed_tools`
     (combine with step 6 if you want to ship in one PR).
   - `bot/app.py`: in `_handle_message`, after `_get_client` and
     before `client.query()`, while holding `slot.lock`, mutate
     `slot.ctx.message_id`, `slot.ctx.chat_id`,
     `slot.ctx.sender_open_id`, `slot.ctx.conversation_key`.
   - `bot/agent/imaging.py`: take `ctx` (or `conversation_key`) as an
     argument from its caller (`generate_image` tool body) instead of
     reading the old module global.

   This is a **pure refactor** — no new tool, no new behavior. Existing
   read tools should continue working unchanged. Verify with the
   existing test set (or a manual round-trip in dev) before moving on.

6. **`bot/agent/runner.py` — `allowed_tools` whitelist**: add the 8
   new `mcp__pmo__*` entries to the existing `allowed_tools` list at
   `runner.py:179`. **This step blocks step 9** (smoke test): without
   it the LLM never sees the new tools.
7. **`agent/tools.py`** — add the 8 new tools (`resolve_people`,
   `schedule_meeting`, `cancel_meeting`, `list_my_meetings`,
   `append_action_items`, `query_action_items`, `create_meeting_doc`,
   `undo_last_action`) plus the `today_iso` extension as inner
   functions inside `build_pmo_mcp(ctx)`; each follows the §5.1
   skeleton and reads context from `ctx`.
8. **System prompt** — append §9 directives in `bot/agent/runner.py`
   (`SYSTEM_PROMPT` constant).
9. **End-to-end smoke test** in a private Feishu group, mandatory
   coverage of these scenarios:
    - Schedule a meeting with two attendees → confirm event in Feishu
      Calendar UI + `bot_actions` row with `status='success'` +
      meeting visible to both attendees with `attendee_ability=
      can_modify_event`.
    - Append 3 action items linked to the event above → confirm rows
      in `action_items` table, owners populated, project resolved.
      Then test the ambiguous flow: ask "记一下要发邮件" with no project
      hint and confirm `needs_project: true` is returned and **no rows
      were written**. Provide a project, retry, confirm rows appear.
    - Create a meeting-notes doc → confirm Docx in 文档柜 + link works.
    - **Undo each of the above in turn** via `undo_last_action` →
      confirm Feishu side artifacts deleted + the original
      `bot_actions` rows transitioned to `undone` + a fresh
      `undo_last_action` row exists pointing at each original
      `action_id` (the audit trail per §3.9). (This is the §1.4
      safety-net check; do NOT skip it.)
    - Logical_key dedup: ask the bot the same scheduling request
      twice within 60s; confirm the second call returns the prior
      result with `deduplicated_from_logical_key: true` and **no
      second meeting** appears in Feishu.
    - Group chat: user A schedules a meeting, user B says "取消刚才那个会"
      → bot must refuse / say it can only undo user B's own actions.
    - Bootstrap recovery: manually delete the bot's Bitable base in
      Feishu, then issue a write request; confirm the bot self-heals
      (re-creates the base, posts the warning message, completes the
      original request). Run two such requests concurrently and
      confirm only **one** new base is created.

Each step touches at most one or two files. Step 5 is the largest
single touch (4 files), and is intentionally separated from new-
behavior steps so a regression there is easier to bisect.

---

## 12. Open questions to revisit after MVP usage

- Should `bot_actions` rows be exposed via a "what did the bot do for
  me lately" Feishu card? (Probably yes; trivial extension.)
- Should `action_items` get a Feishu webhook back into the bot when
  someone marks an item done in the Bitable UI? (Two-way sync; later.)
- Do we need per-conversation rate limiting on write tools, the way
  we limit `generate_image`? (Likely yes once a few teams use it.)
