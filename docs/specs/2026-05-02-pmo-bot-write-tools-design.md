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
  in the same release as `schedule_meeting` / `cancel_meeting` /
  `append_action_items` / `create_meeting_doc`. It does not ship later.
- Must work for every other write tool's outputs **including
  `cancel_meeting`** — undo of an accidental cancel restores the
  event from the pre-cancel snapshot (§3.4, §3.9). Without this case,
  cancellation would be a one-way door, violating the trust model.
- Must be tested end-to-end during the §11 step-9 smoke test before
  the bot is exposed to other groups, with explicit coverage of:
  schedule → undo, cancel → undo (restore), append → undo, create_doc
  → undo.
- Must remain usable even when the original message is older than
  `_seen_events` LRU window — i.e., scoped by `chat_id` +
  `sender_open_id`, not `message_id`.

If any of those conditions can't be met for a release, that release
defers the corresponding write tool, not the undo.

**Cancel/restore is best-effort, not perfect** (§3.4 caveats): the
restored event has a different `event_id`, and any post-cancel edits
others made are lost. This is documented so the agent can warn the
user when the restore happens. It is still strictly better than
"sorry, no undo for cancel".

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

**Resolution order** (per input string — choose path based on the
input's *shape*, not just sequence):

1. **`profiles` + `feishu_links` join** in Supabase — handles people
   who already use pmo_agent. Highest confidence. Matches against
   `handle` (with or without leading `@`), `display_name`, and the
   linked `feishu_email`.
2. **Email or phone shape** → `contact.v3.user.batch_get_id`. This
   endpoint accepts ONLY `emails[]` or `mobiles[]` per Feishu's docs;
   it does NOT take names. Use it when the input string looks like
   `name@host.tld` or matches a phone-number regex.
3. **Otherwise (free-form name / handle / Chinese name)** →
   raw HTTP call to `/open-apis/search/v1/user`. **The lark-oapi
   Python SDK does NOT expose this endpoint** — `contact.v3.user`
   has `batch`, `batch_get_id`, `get`, `list`, `find_by_department`,
   etc., but no `search` method (verified against
   `lark_oapi/api/contact/v3/resource/user.py`). The
   `/open-apis/search/v1/user` endpoint is what `lark-cli contact
   +search-user --query "<name>"` calls under the hood.

   Implementation in `bot/feishu/client.py`: use `httpx` (already a
   dependency, see `feishu/client.py:67`) to call the endpoint with
   `Authorization: Bearer <tenant_access_token>`. Reuse the
   existing tenant_access_token issuer flow (the same pattern as
   `fetch_self_info` already does for `/open-apis/bot/v3/info`).
   Request body: `{"query": "<name>", "page_size": 20}`. Response
   contains `users: [{open_id, name, en_name, email, department_ids,
   ...}]` ranked by relevance.

   If exactly one match → `resolved`; if 2+ → `ambiguous`; if 0 →
   `unresolved`.

   **Error handling for the raw HTTP path** (no PostgREST conflict
   semantics here — just standard HTTP):
   - Non-2xx → mark the input string as `unresolved` with an
     `error_tag: "directory_search_failed"`. Let the agent reask the
     user. Do not raise — fail soft per input string so a single
     bad name doesn't kill resolution for siblings.
   - HTTP 401 (token expired) → invalidate the cached
     tenant_access_token, refetch via the existing issuer (same
     pattern as `feishu/client.py:67`), retry once. If the second
     attempt also 401s, surface a generic
     `directory_search_unavailable` and tell the agent to reply
     "我现在查不到通讯录，请稍后再试".
   - HTTP 429 (rate limit) → one retry with 500 ms backoff; if still
     429, treat as `unresolved` for that input.
   - HTTP 5xx → one retry with 500 ms backoff; if still 5xx, treat
     as `unresolved`.

**Spec-vs-reality note**: an earlier draft of this spec described
`batch_get_id` as a "by name" lookup. That was wrong — Feishu's docs
explicitly list only `emails` and `mobiles` as inputs. Caught in
review iteration 5; the fixed order above is what the implementation
must follow.

The org-wide search scope (chosen during brainstorming over the
narrower "visible to bot only" option) means Step 3 can return any
employee in the directory, not just people the bot has been added
to chats with.

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
      "source": "profiles" | "directory_email_or_phone" | "directory_search"
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
2. **Phase 2.0 — Read bot's calendar_id**: load
   `bot_workspace.calendar_id` (cached in process for the request).
   Every later Calendar SDK call needs **both** `calendar_id` AND
   `event_id` as path params (`/calendars/:calendar_id/events/:event_id/...`)
   — the SDK request builders all require both, see
   `lark_oapi/api/calendar/v4/model/{create_calendar_event_attendee_request,
   get_calendar_event_request, delete_calendar_event_request}.py`. We
   thread the bot's own `calendar_id` through every step here, then
   record it in `result.calendar_id` so cancel/undo can re-read it
   later without going back to `bot_workspace`.
3. **Phase 2.1 — Freebusy pre-check**: call
   `client.calendar.v4.freebusy.batch(BatchFreebusyRequest)` (SDK
   method is `freebusy.batch`, NOT `freebusy.list` — the latter is a
   single-user variant) with body built via
   `BatchFreebusyRequestBody.builder()`. Body fields per
   `lark_oapi/api/calendar/v4/model/batch_freebusy_request_body.py`:
   `user_ids: List[str]` (NOT `user_id_list`), `time_min: str`,
   `time_max: str`, `include_external_calendar: bool` (we pass
   `false`), `only_busy: bool` (we pass `true`). Use
   `user_id_type="open_id"` on the request.

   If any returned slot overlaps the requested window, **the action
   is complete with a "conflict" outcome — NOT a failure**. Mark the
   row `status='success'` with
   `result.outcome="conflict"`, `result.conflicts=[{open_id,
   busy_event_summary}, ...]`, and `target_id=NULL`. Return that
   result to the agent (it surfaces "albert 那个时间已经有会了，要换
   时间吗？" to the user). A subsequent retry of the *same*
   utterance hits Phase 1a's idempotency check, sees the cached
   conflict, and returns it without re-calling Feishu. A new
   utterance with a different time produces a different
   `logical_key`, so a fresh row is created cleanly.

   **Why not `failed`** (the v9 design): `failed` is the
   "retryable technical error" status — Phase 1a's failed-status
   branch automatically reclaims and re-executes via
   `update_for_retry`. A conflict isn't a technical error; it's a
   business outcome the user has to act on. Treating it as `failed`
   meant a duplicate webhook would re-issue the freebusy call (and
   if the conflicting meeting got cancelled in between, would
   silently succeed in scheduling — a behavior the user never
   asked for). Storing it as `success+outcome=conflict` makes the
   idempotency contract clean: the user got the answer, repeating
   the question gives the same answer.

   The `outcome` discriminator on `result` is the same pattern used
   by `reconciliation_kind` in §5.3 — it lets `status` retain pure
   semantics ("did the side-effect-or-decision land?") while
   richer semantics live in `result`.
4. **Phase 2.2 — Create event**:
   `client.calendar.v4.calendar_event.create(CreateCalendarEventRequest
   .builder().calendar_id(bot_calendar_id).request_body(...).build())`
   with `attendee_ability=can_modify_event`. Returns
   `event_id`.
5. **Phase 2.2.5 — Intermediate persist (atomicity)**: as soon as
   `event_id` is in hand, run an UPDATE that records
   `target_id=event_id`, `target_kind='calendar_event'`,
   `result.calendar_id=<bot_calendar_id>`, `result.event_id=<event_id>`
   on the still-`pending` row. **This update is NOT a status
   change — the row stays `pending`** — but if the process crashes
   or the next sub-step fails between here and Phase 2.3, we still
   have a record that "we created event X". This protects against
   the v8-and-earlier silent-duplicate bug where a failed attendee
   step would mark the row `failed` while the event sat orphaned in
   Feishu, and a retry would gleefully create event Y.

   **Residual crash window — pre-2.2.5**: if the process dies
   between Phase 2.2 (event created in Feishu) and Phase 2.2.5 (DB
   UPDATE that records `target_id`), the row is `pending` with no
   `target_id` and the event is orphaned in Feishu. The §5.3 GC will
   later transition this row to `reconciled_unknown` after 5 min,
   but undo cannot target the orphan because `target_id` is NULL.
   This window is small (one DB roundtrip) but real. **Mitigation**:
   log the new `event_id` to stderr at INFO level the moment it
   comes back from Feishu, before issuing the UPDATE, so an operator
   can manually delete the orphan from logs after a crash. We do not
   try to recover automatically — that would require an out-of-band
   reconciler that the §1.4 trust model doesn't currently warrant.
6. **Phase 2.3 — Invite attendees**:
   `client.calendar.v4.calendar_event_attendee.create(
   CreateCalendarEventAttendeeRequest.builder().calendar_id(
   bot_calendar_id).event_id(event_id).request_body(
   CreateCalendarEventAttendeeRequestBody.builder().attendees([
   CalendarEventAttendee(type="user", user_id=open_id), ...]).
   need_notification(true).build()).user_id_type("open_id").build())`.
   The SDK exposes this as `calendar.v4.calendar_event_attendee`
   (flat attribute, with underscore — see
   `lark_oapi/api/calendar/v4/version.py`), NOT as a nested
   `calendar_event.attendee` path. The method is `.create` (not
   `.create_batch`); the body accepts a list in the `attendees`
   field. `batch_delete` exists for the inverse, but there's no
   `batch_create`.

   **If this step fails after Phase 2.2.5 has persisted the
   event_id**: do NOT mark the row `failed` (which would invite a
   retry that creates a duplicate event). Instead transition to
   `reconciled_unknown` with `error="attendee_invite_failed:
   <details>"` and `result.reconciliation_kind = "partial_success"`
   (the discriminator that §5.3 documents — see the "Two flavors of
   `reconciled_unknown`" table). The agent surfaces this to the user
   with a message like "I created the event but couldn't invite
   everyone — please check the calendar and reinvite manually, or
   ask me to undo". `last_for_me` (§3.9) finds the row because it
   filters `status IN ('success', 'reconciled_unknown') AND
   target_id IS NOT NULL`; undo deletes the orphan event using the
   persisted `target_id` and `result.calendar_id`.

   **Why not auto-compensate** (delete the event we just created)?
   Doing so would silently destroy a successful side effect that
   *is* visible to whoever's already on the calendar, which could
   surprise someone who saw the invite arrive on the bot's own
   calendar. `reconciled_unknown` keeps the human in the loop. v2
   may add an explicit "rollback on partial failure" mode once we
   see how often this fires in practice.
7. **Phase 3 — Persist terminal success**: update `bot_actions` to
   `status=success`, augment `result` with `link` and `attendees`
   (target_id and calendar_id already in place from Phase 2.2.5).
8. Return event details to the agent.

#### 3.3bis API endpoint vs lark-oapi SDK attribute path

This spec uses two different naming styles for Feishu APIs and they
mean different things. Implementers must use the **SDK attribute
path** in code, not the URL-style name:

| Concept | API endpoint URL | lark-oapi Python SDK path |
|---|---|---|
| Create calendar | `/open-apis/calendar/v4/calendars` | `client.calendar.v4.calendar.create(...)` |
| Resolve user's primary calendar | `/open-apis/calendar/v4/calendars/primarys` (note plural — verified in `lark_oapi/api/calendar/v4/model/primarys_calendar_request.py:25`) | `client.calendar.v4.calendar.primarys(...)` (plural method name; takes `user_ids: List[str]`) |
| Get calendar event | `/open-apis/calendar/v4/calendars/{...}/events/{...}` | `client.calendar.v4.calendar_event.get(...)` (requires both `calendar_id` and `event_id`) |
| Create calendar event | `/open-apis/calendar/v4/calendars/{...}/events` | `client.calendar.v4.calendar_event.create(...)` (requires `calendar_id`) |
| Delete calendar event | `/open-apis/calendar/v4/calendars/{...}/events/{...}` | `client.calendar.v4.calendar_event.delete(...)` (requires both `calendar_id` and `event_id`) |
| List events on calendar | `/open-apis/calendar/v4/calendars/{...}/events` | `client.calendar.v4.calendar_event.list(...)` (requires `calendar_id`) |
| Add attendees | `/open-apis/calendar/v4/calendars/{...}/events/{...}/attendees` | `client.calendar.v4.calendar_event_attendee.create(...)` (flat, NOT `.calendar_event.attendee`; requires both `calendar_id` and `event_id`) |
| Batch freebusy | `/open-apis/calendar/v4/freebusy/batch` | `client.calendar.v4.freebusy.batch(...)` (the URL path is `/batch`, NOT `/batch_query` — verified in `lark_oapi/api/calendar/v4/model/batch_freebusy_request.py`) |
| Create Drive folder | `/open-apis/drive/v1/files/create_folder` | `client.drive.v1.file.create_folder(...)` |
| Upload Drive file | `/open-apis/drive/v1/files/upload_all` | `client.drive.v1.file.upload_all(...)` (singular `file`, NOT plural `files`) |
| Create import task | `/open-apis/drive/v1/import_tasks` | `client.drive.v1.import_task.create(...)` (singular `import_task`, NOT plural `import_tasks`) |
| Poll import task | `/open-apis/drive/v1/import_tasks/{ticket}` | `client.drive.v1.import_task.get(...)` |
| Append doc blocks | `/open-apis/docx/v1/documents/{...}/blocks/{...}/children` | `client.docx.v1.document_block_children.create(...)` |
| Bitable: create base | `/open-apis/bitable/v1/apps` | `client.bitable.v1.app.create(...)` |
| Bitable: get base | `/open-apis/bitable/v1/apps/{...}` | `client.bitable.v1.app.get(...)` |
| Bitable: create table | `/open-apis/bitable/v1/apps/{...}/tables` | `client.bitable.v1.app_table.create(...)` (NOT `app.table.create`) |
| Bitable: append record | `/open-apis/bitable/v1/apps/{...}/tables/{...}/records` | `client.bitable.v1.app_table_record.create(...)` |
| Bitable: batch records | (same path with `/batch_create`) | `client.bitable.v1.app_table_record.batch_create(...)` |
| Bitable: batch delete | (same path with `/batch_delete`) | `client.bitable.v1.app_table_record.batch_delete(...)` |

**Why the gap**: lark-oapi's Python SDK flattens nested REST paths
into a single attribute on the version object (see
`lark_oapi/api/calendar/v4/version.py`,
`lark_oapi/api/drive/v1/version.py`,
`lark_oapi/api/bitable/v1/version.py`,
`lark_oapi/api/docx/v1/version.py`). When in doubt, open the
`version.py` for the relevant API surface and grep for the resource;
the `self.<name>` attributes are the legal SDK paths.

The rest of this spec uses SDK-style paths (`calendar_event_attendee`,
`drive.v1.file`, `import_task`, `app_table_record`, etc.). If you
spot one that looks like a URL path with extra dots, it's probably
a typo this callout missed — flag it and fix it before implementation.

### 3.4 `cancel_meeting`

**Inputs**: `event_id` OR `last:true` (cancels the most recent
bot-scheduled meeting **in the current conversation**).

Resolution rules:
- If `event_id` is given: look up `bot_actions WHERE target_kind=
  'calendar_event' AND target_id=event_id`. If no row → refuse
  ("only cancel meetings I created"). If row exists but
  `status='undone'` → no-op, return idempotent success. **Cross-chat
  guard**: if the row's `chat_id ≠ ctx.chat_id`, refuse with a
  message explaining "this meeting was scheduled in <other chat>;
  please ask me there to cancel it." The `event_id` knowing-it-is-
  bot-owned check alone is not enough — anyone in any chat who has
  the link could otherwise cancel a meeting scheduled by a different
  team. v1 keeps this strict (no override flag); a future "I'm sure,
  cancel anyway" path can be added once UX supports cross-chat
  confirmations.
- If `last:true`: look up `bot_actions WHERE chat_id=<current> AND
  sender_open_id=<current> AND action_type='schedule_meeting' AND
  status='success' AND target_id IS NOT NULL ORDER BY created_at
  DESC LIMIT 1`. **The `sender_open_id` filter matters in groups**:
  without it, user A could cancel a meeting user B asked the bot
  to schedule. **The `target_id IS NOT NULL` filter matters
  post-iter-10**: §3.3 Phase 2.1 now stores freebusy conflicts as
  `success` rows with `target_id=NULL` (they're "I checked and
  there was a conflict" no-ops, not actual scheduled meetings).
  Without this filter, `last:true` would resolve to a conflict row
  that has no event to cancel. Both filters together match the
  `last_bot_action_for_sender_in_chat` helper's `require_target=True`
  default (§11 step 2). Requires `(chat_id, sender_open_id)` on
  `bot_actions` (see §6.2).

**Internal sequence**:

1. **Phase -1 — pre-flight**: validate that `event_id` (or the
   resolved-from-`last`) exists and is bot-owned (`bot_actions` row
   present). Refuse if not. Also extract `calendar_id` from the
   original `schedule_meeting` row's `result.calendar_id` (saved
   during §3.3 Phase 2.2.5). Both `calendar_id` and `event_id` are
   required by every Calendar SDK call.
2. **Phase 1 — pending insert** keyed on `(message_id, "cancel_meeting")`.
3. **Phase 2a — Read full event before delete**:
   `client.calendar.v4.calendar_event.get(GetCalendarEventRequest
   .builder().calendar_id(calendar_id).event_id(event_id).build())`
   → `pre_cancel_event_snapshot`. **Critical for undo**: without
   this snapshot, undo cannot restore the event; once Feishu
   deletes, the event is gone server-side. This call must succeed
   before we delete.
4. **Phase 2b — Delete**:
   `client.calendar.v4.calendar_event.delete(DeleteCalendarEventRequest
   .builder().calendar_id(calendar_id).event_id(event_id).build())`.
5. **Phase 3 — Persist terminal state**: mark this `cancel_meeting`
   row `success` with `result={original_event_id, calendar_id,
   pre_cancel_event_snapshot}`. Mark the original `schedule_meeting`
   row `status='undone'` for traceability (and so it stops appearing
   in `last_for_me` lookups).

**Why snapshot, not just rely on Feishu trash/recovery**: Feishu
calendar events do not have a "recently deleted" recovery window
exposed through tenant_access_token APIs. Once deleted, restoration
must come from data we saved before the delete. The snapshot is the
data.

**Restore behavior** (`undo_last_action` on a `cancel_meeting` row):
re-creates the event via `calendar_event.create` from the snapshot,
then re-invites the original attendees. **Caveats** (must surface to
user):
- The restored event has a **new** `event_id`. Anyone who had a link
  to the old one needs the new link.
- Any modifications other attendees made *after* cancellation but
  before restore are lost.
- If 5+ minutes elapsed between cancel and restore, attendees may
  already have removed it from their own UIs / accepted other
  bookings for the slot.

These caveats are encoded in the agent's reply when undo runs (the
tool's return value includes `restore_caveats: [...]`).

### 3.5 `list_my_meetings`

**Inputs**:
- `target?: "self" | str` — defaults to `"self"`, which the tool body
  resolves to `ctx.sender_open_id`. The agent does NOT need to look
  up its own open_id; the asker's identity is already in the
  `RequestContext`. If the agent passes a Feishu `open_id` (resolved
  via `resolve_people` first), the tool checks "is this someone other
  than the asker" — see "scope check" below.
- `since: str` (RFC3339)
- `until: str` (RFC3339)

**Why default to self**: the prompt-injected `[asker]` line gives the
LLM only a pmo `user_id` / `handle` / `display_name` — never a Feishu
`open_id`. Without the `"self"` default, the natural request "我下午
有啥会" would force the agent into an awkward dance: call
`resolve_people` on the asker's own handle just to get their own
open_id back. The default short-circuits that.

**Scope check** (when `target ≠ "self"`): looking up someone else's
calendar is **not** privileged in Feishu's data model — the bot can
ask the API regardless — but the spec treats it as a soft permission
boundary. The tool description directs the LLM: "Only pass an
explicit `target` open_id when the user clearly asked about another
person's calendar (e.g. 'albert 下周三有空吗?'). For any first-person
question, leave `target` unset."

**Resolving the user's primary calendar_id**: `calendar_event.list`
requires a `calendar_id` path param (see
`lark_oapi/api/calendar/v4/model/list_calendar_event_request.py`),
which must be the user's *primary* calendar — not the bot's. The
SDK exposes a dedicated lookup for this:

```python
client.calendar.v4.calendar.primarys(
    PrimarysCalendarRequest.builder()
      .user_id_type("open_id")
      .request_body(
          PrimarysCalendarRequestBody.builder()
            .user_ids([resolved_target_open_id])
            .build()
      )
      .build()
)
```

Returns a list of `{user_id, calendar: {calendar_id, ...}}`; pick
the entry whose `user_id` matches and pull `calendar.calendar_id`.
Cache per-request (one resolution per `list_my_meetings` call).

**Failure modes for primarys**:
- Empty list returned (e.g., user is external to the org / has
  never logged in / hasn't provisioned a Feishu calendar) → return
  `{user_calendar_events: [], visibility_note: "我没找到这个人的
  飞书主日历——可能他还没用过日历功能。我只能列出我自己安排的会。",
  bot_known_events: <still queryable from bot_actions>}`. Do NOT
  raise; the bot's own `bot_known_events` data path is independent
  and still works.
- HTTP 4xx/5xx → log and treat the same as empty list. The bot
  remains useful for the bot_known_events portion.
- Multiple entries (shouldn't happen for a single user_id but
  defensive) → use the first match by `user_id`.

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
    // action_type='schedule_meeting' AND status='success' AND
    // attendees ⊇ {resolved_target_open_id}). These are 100% complete
    // from the bot's POV.
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

**Ambiguous flow** (no auto-write, no orphan records, no `bot_actions` row):

When **any** item lacks a project AND auto-resolution can't pick one
confidently, the tool returns **without writing anything** — including
no row in `bot_actions`. This decision happens in **Phase -1** (§5.1),
which is the spec-mandated location for "early reject before any
side effect or bookkeeping".

Return shape:

```json
{
  "needs_input": "project",
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
each item. Since the new message has a different `message_id` AND the
canonical_args are different (project is set), neither the
`(message_id, action_type)` UNIQUE nor the `logical_key` partial
UNIQUE collides — the second call executes cleanly through Phase 0
and Phase 1, lands a single `success` row.

**Critical sequencing**: if Phase -1 weren't a thing — i.e. if the
tool followed a naïve "always insert pending first" pattern — then
returning `needs_input` after the insert would leave a `pending` row
with no terminal state. That row would either drift into
`reconciled_unknown` after 5 min (false alarm to the user about an
"orphaned call") or, worse, block re-attempts via the logical_key
UNIQUE constraint. The Phase -1 / Phase 0 separation in §5.1 exists
specifically to avoid this.

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

Three-phase pattern. On success the row's `target_kind="docx"` and
`target_id=<doc_token>` so undo can call
`client.drive.v1.file.delete(file_token=target_id, type="docx")` —
both fields are mandatory in `DeleteFileRequest` (see §3.9 +
`lark_oapi/api/drive/v1/model/delete_file_request.py`). Creates a
Docx in the bot's "文档柜" folder, returns
`{doc_token, url}`. The agent embeds the link in its reply.

**Implementation note — Markdown to Docx**: the standard
`docx.v1.document.create` endpoint creates an **empty** document and
does NOT accept Markdown directly. Two viable paths, to confirm during
implementation:

- **Path A (preferred)**: a **3-step async flow**, NOT a single
  call. The v6 spec described this as a one-shot
  `import_tasks.create` with body=Markdown bytes — that was wrong.
  The actual `ImportTask` model
  (`lark_oapi/api/drive/v1/model/import_task.py`) takes
  `file_token`, `file_extension`, `type`, `file_name`, `point` —
  i.e. the markdown source has to live in Drive first as a file
  whose token we then hand to the importer.

  Each step that produces a Feishu artifact must persist its handle
  to `bot_actions` immediately — same multi-step atomicity rule as
  §3.3 schedule_meeting (Codex iter9 #1 / iter10 #2).

  1. **Upload the markdown source as a `.md` file**:
     `client.drive.v1.file.upload_all(file_name="meeting-notes.md",
     parent_type="explorer", parent_node=<bot_workspace.docs_folder_token>,
     size=<bytes>, file=<bytes_io>)` → returns `source_file_token`.
     (SDK attribute is singular `file`, not `files` — see §3.3bis.)
  2. **Phase 2.1.5 persist** (intermediate state): UPDATE the
     `pending` row with `result.source_file_token=<from step 1>`.
     The `.md` is now a real artifact in Drive; if any later step
     fails, we need to know its token to clean it up.
  3. **Create the import task**:
     `client.drive.v1.import_task.create(import_task=ImportTask(
     file_token=<source_file_token>, file_extension="md",
     type="docx", file_name="<title>.docx",
     point=ImportTaskMountPoint(mount_type=1,
     mount_key=<docs_folder_token>)))` → returns `ticket` (the async
     task id). (SDK attribute is singular `import_task`.)
  4. **Phase 2.2.5 persist**: UPDATE with
     `result.import_ticket=<from step 3>`. The async import is now
     in flight on Feishu's side; even if our process dies, we have
     a record that there's an in-flight import to reconcile.
  5. **Poll for completion**:
     `client.drive.v1.import_task.get(ticket=<...>)` every ~500 ms
     until `job_status == 0` (success) or terminal failure
     (`job_status` is a known error code — consult the SDK docs at
     poll-result time). Set a **5-minute total timeout** on this
     poll loop. (The number is a defensive guess: typical
     meeting-notes-sized markdown imports complete in seconds, but
     Feishu has documented multi-minute waits under load. If
     real-world timeouts hit the 5-minute cap with non-trivial
     frequency, raise it; if 99p completes in < 10s, lower it
     and surface "import is unusually slow" earlier as a
     reconciled_unknown signal.)
  6. **Phase 2.3.5 persist (on success)**: UPDATE with
     `target_id=<doc_token from poll>`, `target_kind="docx"`,
     `result.url=<...>`. Now undo can find the doc.
  7. **Phase 3 — Persist terminal status**: UPDATE `status='success'`.

  **Failure / uncertainty handling**:
  - **Step 1 (upload) fails**: no Feishu artifact yet; mark `failed`
    with `error="upload_failed: ..."`. Retry is safe.
  - **Step 3 (import_task.create) fails after step 1 succeeded**:
    `.md` is in Drive but no import has started. Best-effort delete
    the orphan `.md` (`file.delete(file_token=source_file_token,
    type="file")`); on cleanup success mark `failed`. On cleanup
    failure mark `reconciled_unknown(partial_success)` with
    `target_id=source_file_token` and `target_kind="file"` so undo
    can clean it later.
  - **Step 5 (poll) returns terminal failure** (Feishu reports the
    import errored): no docx was produced. Best-effort delete the
    `.md`; mark the row `failed`.
  - **Step 5 (poll) times out after 5 min OR network error during
    poll**: we **do not know** whether Feishu finished the import.
    Mark `reconciled_unknown` with `kind=partial_success`,
    `target_id=NULL` (we have no doc_token to point at),
    `result.source_file_token=...`,
    `result.import_ticket=...`. Surface to the user: "I started
    importing your notes but lost track of whether it finished —
    please check 文档柜 and let me know if you want me to retry or
    delete what's there." Undo on this row can re-poll the ticket
    and either delete the resulting docx (if found) or just delete
    the `.md`.
  - **Step 6 persist (DB UPDATE) fails after step 5 success**: same
    pre-2.2.5-style residual crash window as in §3.3 — log the
    `doc_token` to stderr at INFO level so an operator can recover.

- **Path B (fallback)**: `client.docx.v1.document.create` (empty
  doc), then parse Markdown into Docx blocks ourselves and call
  `client.docx.v1.document_block_children.create` (flat attribute
  path, see §3.3bis) to append. More code, no async polling, no
  intermediate file in Drive.

Pick A first; fall back to B only if `drive:drive` import permissions
can't be granted in production. The choice does not affect this
tool's **interface** — only its body.

### 3.9 `undo_last_action`

**Inputs**:
- `target` (one of):
  - `last_for_me: true` — undo the most recent **terminal** row in
    the current conversation **that the current asker created**.
    Scoped by `(chat_id, sender_open_id)`, see §6.2. The lookup
    filter is `status IN ('success', 'reconciled_unknown') AND
    target_id IS NOT NULL`, **not** just `status='success'`: the
    `reconciled_unknown` arm catches partial-Phase-2 failures from
    §3.3 (event was created in Feishu but attendees couldn't be
    invited). Without this, the user has no way to clean up the
    orphan event via the safety-net path. Rows with `target_id IS
    NULL` are skipped — they represent the residual pre-2.2.5 crash
    window where we don't know what to delete. The earlier
    `last_in_chat` name is renamed to make the per-asker scope
    explicit; in groups, user A cannot undo user B's actions through
    this tool.
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
- `schedule_meeting` →
  `client.calendar.v4.calendar_event.delete(DeleteCalendarEventRequest
  .builder().calendar_id(<from result.calendar_id>).event_id(
  <from target_id>).build())`. Both path params are mandatory (see
  `lark_oapi/api/calendar/v4/model/delete_calendar_event_request.py`).
- `cancel_meeting` → **restore-from-snapshot**:
  `client.calendar.v4.calendar_event.create(...calendar_id(<from
  result.calendar_id>)...)` populated from
  `result.pre_cancel_event_snapshot` (saved by §3.4 Phase 2a),
  then `client.calendar.v4.calendar_event_attendee.create(...
  calendar_id(<same>).event_id(new_event_id)...)` for the original
  attendees. The new event has a different `event_id` than the
  original; the response includes `restore_caveats` (see §3.4) so
  the agent can warn the user. Audit linkage: we create a fresh
  `schedule_meeting` audit row with `target_id=new_event_id` and
  `result.predecessor_action_id=<original schedule action_id>`; the
  cancel row stays `undone`.
- `append_action_items` →
  `client.bitable.v1.app_table_record.batch_delete(...)` (flat SDK
  attribute path; see §3.3bis), passing `record_ids` from
  `result.record_ids` along with the `app_token` and `table_id`
  recorded in the original `result`.
- `create_meeting_doc` →
  `client.drive.v1.file.delete(DeleteFileRequest.builder().file_token(
  <from target_id>).type("docx").build())`. Both `file_token` and
  `type` are required by the SDK (see
  `lark_oapi/api/drive/v1/model/delete_file_request.py`); the
  `type` is always `"docx"` for this tool's outputs.

Without a `cancel_meeting` case, undoing an accidental cancel was
impossible — see §1.4: the no-confirmation-gate trust model requires
*every* destructive write tool to have a compensating action. A
`cancel_meeting` followed by `undo_last_action` is now a closed loop
even though the underlying Feishu API has no "restore" endpoint, by
virtue of the snapshot saved before deletion.

Marks the source row `status=undone`. Records its own `undo_last_action`
row with `target_id=<original action_id>` for traceability.

Idempotent: calling it on an already-`undone` row is a no-op success.

---

## 4. Bot workspace bootstrap

A new one-shot script: `bot/scripts/bootstrap_bot_workspace.py`.

**On first run** (per environment, dev / staging / prod):

1. `calendar.v4.calendar.create` → primary calendar, store
   `calendar_id`.
2. `client.bitable.v1.app.create` (folder=root) → "包工头的工作台" base, store
   `app_token`.
3. Inside that base, `client.bitable.v1.app_table.create` (flat
   attribute path, NOT `app.table.create`; see §3.3bis) for two tables:
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
--
-- logical_key_locked = false on this row: the lock semantics here
-- come from the (message_id, action_type) UNIQUE, NOT from the
-- logical_key partial UNIQUE. Setting it false prevents bootstrap
-- attempts from inadvertently competing for the dedup index.
INSERT INTO bot_actions
  (message_id, chat_id, sender_open_id, logical_key,
   action_type, status, args, logical_key_locked)
VALUES
  ('__bootstrap_lock__', '__system__', '__system__', '__bootstrap_lock__',
   'bootstrap_workspace_lock', 'pending', '{}'::jsonb, false)
ON CONFLICT (message_id, action_type) DO NOTHING
RETURNING id;
```

**Audit the work** (permanent, separate row per call):

```sql
-- Each bootstrap attempt also writes its own audit row with a unique
-- message_id (e.g. timestamp + random) so the history accumulates
-- and stays queryable. logical_key is set to the same unique message_id
-- AND logical_key_locked = false: bootstrap is intentionally not
-- idempotent across re-runs; we want every attempt logged and we
-- don't want bootstrap to take up slots in the dedup index.
INSERT INTO bot_actions
  (message_id, chat_id, sender_open_id, logical_key,
   action_type, status, args, target_kind, logical_key_locked)
VALUES
  ('bootstrap-' || $timestamp || '-' || $random_suffix,
   '__system__', '__system__',
   'bootstrap-' || $timestamp || '-' || $random_suffix,
   'bootstrap_workspace', 'pending', $args, 'workspace_bootstrap', false)
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
whose tool implementations close over it. The runner mutates the
context's fields **inside its existing `slot.lock` acquisition**
before calling `client.query()`, so no other request can interleave.

**Encapsulation matters here**: `app.py` and other callers do NOT
reach into `_get_client`, `slot.ctx`, or `slot.lock` directly. Those
are private to `bot/agent/runner.py`. The public surface of the
runner gains the new request fields as parameters:

```python
# bot/agent/runner.py — public surface
async def answer(
    conversation_key: str,
    question: str,
    *,
    message_id: str,
    chat_id: str,
    sender_open_id: str,
) -> str: ...

async def answer_streaming(
    conversation_key: str,
    question: str,
    *,
    message_id: str,
    chat_id: str,
    sender_open_id: str,
):  # AsyncIterator
    ...
```

Inside `answer_streaming`, *after* acquiring `slot.lock` (the same
acquisition that already exists at `runner.py:240`), the runner sets
`slot.ctx.*` from the new parameters. `app.py` never imports
`_get_client` or touches `slot.*`; it just calls `answer_streaming`
with the four request parameters and consumes the async iterator as
it does today. This preserves the pooling protocol as private and
also covers the fallback `answer()` path at `app.py:158` automatically.

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
# bot/agent/runner.py — private internals (unchanged shape, plus ctx)
@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    ctx: RequestContext = field(default_factory=RequestContext)
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

async def _get_client(conversation_key: str) -> _PooledClient:
    # ...creates ctx + ClaudeSDKClient with mcp_servers={"pmo": build_pmo_mcp(ctx)}...
    ...

async def answer_streaming(
    conversation_key: str,
    question: str,
    *,
    message_id: str,
    chat_id: str,
    sender_open_id: str,
):
    slot = await _get_client(conversation_key)
    async with slot.lock:                       # already exists today
        slot.ctx.message_id = message_id
        slot.ctx.chat_id = chat_id
        slot.ctx.sender_open_id = sender_open_id
        slot.ctx.conversation_key = conversation_key
        # ... existing body: slot.client.query(question), receive_response loop ...
```

```python
# bot/app.py — _handle_message stays at the public surface only
async for event in agent_runner.answer_streaming(
    conversation_key,
    framed_question,
    message_id=ev.message_id,
    chat_id=ev.chat_id,
    sender_open_id=ev.sender_open_id,
):
    # ... existing card patching loop ...
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

    # ── Phase -1: pre-flight validation. ────────────────────────────
    # Runs BEFORE any bot_actions row is created or any external API
    # is called. If the tool can already tell — from args alone or
    # from quick local lookups — that it cannot proceed, return now,
    # leaving NO trace in bot_actions. This is what
    # append_action_items uses to surface needs_project without
    # writing orphan rows.
    #
    # Allowed pre-flight checks:
    #   - Schema validation of args (types, required fields).
    #   - "needs_project" / "needs_open_ids" / "needs_<X>" returns
    #     where the tool requires extra info from the user.
    #   - Local-DB lookups that don't mutate (e.g.
    #     find_default_project_for_user). Reads of bot_actions are OK.
    #
    # NOT allowed in pre-flight:
    #   - Any Feishu API call. Those go in Phase 2.
    #   - Any DB write. Pre-flight must be free of side effects so
    #     that returning early leaves zero footprint.
    pre = await _preflight_schedule_meeting(args, ctx)
    if pre.kind == "needs_input":
        return _ok({"needs_input": pre.field, "agent_directive": pre.directive})

    # Compute logical_key for repeat-utterance dedup (see §5.2 / §6.2).
    logical_key = queries.compute_logical_key(
        chat_id=chat_id,
        sender_open_id=sender_open_id,
        action_type=action_type,
        canonical_args=pre.canonical_args,  # canonicalized in pre-flight
    )

    # ── Phase 0: short-window logical dedup. ────────────────────────
    # Backed by a partial UNIQUE index on (logical_key) WHERE
    # logical_key_locked = true (see §5.2 / §6.2). Two parallel
    # inserts (across two processes or a single process under
    # concurrent webhooks) race on the UNIQUE; the loser falls
    # through to read the existing row.
    #
    # The 60s "window" is implemented via the dedicated
    # `logical_key_locked` BOOL column, NOT by mutating `status`. A
    # success row stays status='success' forever (so all "find
    # successful action" queries — last_for_me, bot_known_events,
    # cancel_meeting target lookup — keep working). After 60 s, lazy
    # GC just flips logical_key_locked to false, freeing the key.
    recent = queries.get_locked_by_logical_key(logical_key)
    if recent:
        if recent["status"] == "success":
            return _ok({**recent["result"], "deduplicated_from_logical_key": True})
        if recent["status"] == "reconciled_unknown":
            # partial_success orphan — the artifact exists in Feishu
            # but the action couldn't fully complete (e.g. attendees
            # didn't get invited, or doc import polling timed out).
            # We deliberately block retry to avoid duplicate
            # artifacts; the user must run undo first or accept the
            # orphan and ask again with a fresh utterance.
            return _err(
                "a previous identical call left a partial result "
                "in Feishu (target_id={}); please ask me to undo "
                "before re-issuing".format(recent.get("target_id"))
            )
        # status == 'pending' — another caller is in flight.
        return _err("a previous identical call is in flight")

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
            # pending AND re-acquire the logical_key lock. This UPDATE
            # can ALSO raise LogicalKeyConflict if a different message
            # acquired the slot in the meantime (since the failed row
            # released its lock per §6.2). Same handling as Phase 1b's
            # logical-conflict path.
            try:
                action_id = queries.update_for_retry(
                    existing["id"], new_args=args, logical_key=logical_key,
                )
            except queries.LogicalKeyConflict as e:
                winner = e.existing_row
                if winner["status"] == "success":
                    return _ok({**winner["result"], "deduplicated_from_logical_key": True})
                return _err("a previous identical call is in flight")
            if action_id is None:
                return _err("a concurrent retry won the race; try again in a moment")
        elif existing["status"] == "undone":
            return _err("this action has been undone; submit as a fresh request")
    else:
        # Phase 1b: pending insert (first-time path).
        # insert_bot_action_pending may raise two distinct UniqueViolations:
        #   - on bot_actions_message_action_uniq → exact-message retry
        #   - on bot_actions_logical_locked_uniq → another caller holds
        #     the logical_key dedup slot (different message_id, same
        #     logical request)
        # The helper inspects e.diag.constraint_name and dispatches:
        #   - message conflict → returns the existing row keyed by
        #     (message_id, action_type)
        #   - logical conflict → returns the active row keyed by
        #     logical_key (its message_id is different from ours)
        # See §6.2 for the constraint-name contract.
        try:
            action_id = queries.insert_bot_action_pending(
                message_id=message_id,
                chat_id=chat_id,
                sender_open_id=sender_open_id,
                action_type=action_type,
                args=args,
                logical_key=logical_key,
            )
        except queries.MessageActionConflict as e:
            # Same message_id retry that we somehow missed in Phase 1a
            # (rare: race between Phase 1a SELECT and Phase 1b INSERT).
            existing = e.existing_row
            if existing["status"] == "success":
                return _ok(existing["result"])
            return _err("a concurrent call is in flight; try again in a moment")
        except queries.LogicalKeyConflict as e:
            # A different message with the same logical_key won. Surface
            # the winner's outcome.
            existing = e.existing_row  # found by SELECT ... WHERE logical_key=$1 AND logical_key_locked=true
            if existing["status"] == "success":
                return _ok({**existing["result"], "deduplicated_from_logical_key": True})
            # status=='pending' on the winner — it's still in flight
            return _err("a previous identical call is in flight")

    # Phase 2: do the actual side effect.
    # IMPORTANT for tools with multiple sequential side effects (e.g.
    # schedule_meeting = freebusy → create event → invite attendees):
    # the moment any sub-step PRODUCES a Feishu artifact (event,
    # record, doc), persist its identifier to bot_actions BEFORE
    # attempting the next sub-step. If a later sub-step fails, the
    # row goes to `reconciled_unknown` (not `failed`), so retry is
    # blocked and undo can target the persisted artifact.
    #
    # See §3.3 phases 2.0–2.3 for the canonical sequence; the
    # skeleton shows just the single-call shape for tools that don't
    # have multi-step Phase 2.
    try:
        result = await feishu_client.create_calendar_event(...)
    except Exception as e:
        # Pre-creation failure (no Feishu artifact yet) → safe to
        # mark failed; retry can re-issue.
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

`update_for_retry`'s SQL — note the `logical_key_locked = true`
re-acquisition: a `failed` row had its lock cleared by `mark_bot_action_failed`
(§6.2 "lock clearing on terminal status"); transitioning back to
`pending` re-claims the logical_key slot. If another caller has won
the slot in the meantime, the UPDATE will then conflict on the
partial UNIQUE — wrap it in a transaction and surface that as a
retry-too-late condition.

```sql
UPDATE bot_actions
   SET status='pending',
       attempt_count = attempt_count + 1,
       args = $new_args,
       error = NULL,
       logical_key_locked = true,
       updated_at = now()
 WHERE id = $id AND status = 'failed'
 RETURNING id;
```

Two locks layer here:
- `WHERE status='failed'` — only one concurrent caller's UPDATE
  returns a row, others get 0 and bail.
- The partial UNIQUE on `logical_key` — if a different message
  acquired the slot since this row failed, the UPDATE raises a
  PostgREST 409 the helper turns into `LogicalKeyConflict` (same
  mechanism as the first-time-INSERT path in §5.1 Phase 1b).
  **`update_for_retry` is structurally outside Phase 1b's `try`
  block**, so the caller wraps `update_for_retry` in its own
  `try/except queries.LogicalKeyConflict` (see the §5.1 skeleton's
  failed-status branch). The dispatch logic is identical to Phase
  1b: return the winner's success result or the "in flight" error
  depending on the winner's status.

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
| **Logical short-window exclusion** | `bot_actions_logical_locked_uniq` partial UNIQUE on `(logical_key) WHERE logical_key_locked = true AND status IN ('pending','success','reconciled_unknown')` (§6.2) | "User typed the request again 3 seconds later because nothing visibly happened" — same chat, same sender, same canonical args. **Cross-process and cross-instance**: enforced by Postgres, not by app-layer reads. **Also blocks duplicate side effects when a partial_success orphan exists** — see §6.2 lock-behavior table. | After 60s for `success` rows (lazy GC clears the lock); for `partial_success` orphans, blocked until `undo_last_action` runs and clears the lock. Intentional re-issuance worked into a fresh utterance still works after that. |

`logical_key` is a stable hash over `(chat_id, sender_open_id,
action_type, canonical_args)` — the same parameters that uniquely
identify a logical request from a human's POV.

**Why a partial UNIQUE, not a "read then act"** (the v4 approach,
caught as racy in iter-5 review): a Phase 0 read that does
`SELECT ... WHERE logical_key=... AND status='success' AND created_at
> now() - 60s` followed by an INSERT only gives mutual exclusion
*within the same process and only when the Postgres reader sees the
prior commit*. Two webhook tasks racing in different processes (or
in the same process, at the same instant before the first INSERT
commits) both see "no recent success" and both insert. The DB layer
must enforce the exclusion or it isn't enforced.

The fix: the tool body **always tries to INSERT** the `pending` row
with `logical_key_locked = true`. If the partial UNIQUE constraint
fires (because another caller is in flight on the same logical key,
or another caller succeeded within the last 60 s), the loser's
INSERT raises `UniqueViolation`. The loser then `SELECT`s the
existing row and dispatches:
- `status='success'` → return the prior result (`deduplicated_from_logical_key`).
- `status='pending'` → return `"a previous identical call is in flight"`.

**The 60-second window is in a separate column, NOT in `status`**
(this is iter-6 Codex review's correction to v5):

`logical_key_locked: bool` is set to `true` at INSERT time and
flipped to `false` by lazy GC after 60 s. The partial UNIQUE
predicate is `WHERE logical_key_locked = true`. `status` retains
its pure semantic meaning — `success` rows stay `success` forever,
so every "find successful action" query (`last_for_me` (§3.4 / §3.9),
`bot_known_events` (§3.5), `target_id` lookups (§3.4)) keeps working
without needing to also accept some new status value.

An earlier draft used a `status='archived'` value to express
"dedupe-period expired" but that conflated two orthogonal axes:
**did the action succeed** (audit/restore/undo concern) vs **does
this row currently hold the logical_key lock** (dedup concern).
Mixing them required every "successful action" query to start
filtering `status IN ('success','archived')`, with future status
values causing combinatorial drift. Splitting the axes into two
columns (`status` for outcome, `logical_key_locked` for lock
ownership) keeps each query clean.

GC runs lazily inside `get_locked_by_logical_key` — when it walks
past a success row whose `logical_key_locked = true AND created_at
< now() - interval '60 seconds'`, it runs an atomic
`UPDATE ... SET logical_key_locked = false WHERE id=$id AND
logical_key_locked = true` (the predicate prevents double-flip
under concurrent readers) and treats the row as not-locked
(returning NULL) so the caller proceeds with their own INSERT.

**Why a window at all**: the user might *legitimately* want to
schedule "another 30-minute meeting with albert about the same topic"
later in the day. Hard-blocking forever would surprise them. 60
seconds catches the "I pressed enter twice" case without trapping
legitimate repeat scheduling.

**Honesty about coverage**: in the no-confirmation-gate trust model
(§1.4), **truly intentional re-issuance more than 60 seconds apart
will execute twice**. We accept that — the cost of intercepting it is
asking the user "did you mean to schedule again?" on every legitimate
follow-up, which would erode the very fluidity §1.4 is paying for.

### 5.3 Lazy GC: stuck `pending` and aged-out `success`

Two distinct GC actions, both lazy (run inside the read functions
that surface them):

**(a) Stuck pending → `reconciled_unknown`**: a row stuck in
`pending` for >5 minutes is almost certainly orphaned (process died
mid-call). The GC marks it `reconciled_unknown` (a distinct status,
**not** `failed`) with `error="reconciled: pending too long"` and
`result.reconciliation_kind = "stuck_pending"` (see "Two flavors of
reconciled_unknown" below).

The distinction matters: `failed` means "we know the Feishu call
errored, retry is safe". `reconciled_unknown` means "we don't know
if the Feishu side succeeded — retrying could create a duplicate".

The tool skeleton (§5.1) treats `reconciled_unknown` as a hard stop:
return an error to the agent that explains the ambiguity and asks the
user to verify on the Feishu side before issuing a fresh request.
This deliberately surfaces a rare case to the user rather than silently
risking a duplicate meeting.

GC happens in `get_bot_action` itself: if a row matches `status=
'pending' AND created_at < now() - interval '5 minutes'`, the function
runs an atomic `UPDATE ... SET status='reconciled_unknown' WHERE id=
$id AND status='pending' RETURNING *` before returning the row. The
`WHERE status='pending'` predicate avoids races with a still-live
caller about to commit a success.

**(b) Aged success → unlock the logical_key** (§5.2): a `success`
row older than 60 s should leave the partial UNIQUE index on
`logical_key`, freeing the key for a legitimate repeat request. GC
flips `logical_key_locked` from `true` to `false`. The row's `status`
stays `success` — it's still a successful action for audit/restore
purposes; only its claim on the dedup lock has expired.

GC happens in `get_locked_by_logical_key` itself: when the function
finds a candidate row matching the requested `logical_key`, it checks
`created_at`. If `status='success' AND logical_key_locked = true AND
created_at < now() - interval '60 seconds'`, it runs
`UPDATE ... SET logical_key_locked = false WHERE id=$id AND
logical_key_locked = true` (the predicate avoids double-flip races)
and treats the row as not-locked (returning NULL to the caller, which
then proceeds with its INSERT).

**Why both GCs are lazy and not a cron**: the rates are too low to
justify a loop. Stuck-pending events fire only on process crash;
logical-key unlocks only matter to the next caller of the same
logical_key. Doing them inside the read function means we pay
exactly when needed, and we don't introduce a new long-running task
to monitor.

The `status` CHECK constraint in §6.2 includes `reconciled_unknown`
for case (a). Case (b) does NOT touch `status` — it mutates only
`logical_key_locked`, which is a separate column.

**Two flavors of `reconciled_unknown`** (do not collapse them):

| `result.reconciliation_kind` | Source | `target_id` | Undo behavior |
|---|---|---|---|
| `"stuck_pending"` | §5.3 case (a) lazy GC after 5 min | NULL or set | Undo can only act if `target_id IS NOT NULL`. With NULL, surface to the user that we don't know what was created — let them check Feishu manually. |
| `"partial_success"` | §3.3 Phase 2.3 attendee invite failed after Phase 2.2.5 persisted the event_id | always set (the orphan event_id) | Undo deletes the orphan event using the persisted `target_id` and `result.calendar_id`. |

Both kinds are visible to `last_for_me` (§3.9 explicitly filters
`status IN ('success', 'reconciled_unknown') AND target_id IS NOT
NULL`). Code that branches on reconciliation reason should test
`result.reconciliation_kind` rather than parsing the `error` string,
which is meant for human display.

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
    logical_key_locked boolean NOT NULL DEFAULT true,
                                                    -- separate "do I currently hold the
                                                    -- 60s logical_key dedup slot" axis;
                                                    -- decoupled from `status` so audit /
                                                    -- restore queries on success rows
                                                    -- keep working after lock expires
                                                    -- (§5.2 / §5.3 case b).
    args            jsonb NOT NULL,                -- tool inputs (sanitized)
    target_id       text,                          -- Feishu side ID (event_id, record_id, doc_token)
    target_kind     text,                          -- 'calendar_event' | 'bitable_record' | 'docx' | 'workspace_bootstrap'
    result          jsonb,                         -- Feishu response keys we'll need later
    error           text,                          -- failure detail
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Named constraint (NOT a default Postgres-generated name like
    -- bot_actions_message_id_action_type_key) so the regex dispatch
    -- in insert_bot_action_pending can recognize this exact string
    -- in PostgREST's 409 error message. See §6.2 "Constraint names".
    CONSTRAINT bot_actions_message_action_uniq
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

-- Logical-key cross-process exclusion (§5.2): at most ONE row per
-- logical_key may currently hold the dedup lock AND be in an active
-- (pending or success) state. Two concurrent INSERTs race on this
-- UNIQUE; loser gets UniqueViolation. Lazy GC (§5.3 case b) flips
-- logical_key_locked to false on success rows older than 60 s to
-- free the key for legitimate repeat requests.
--
-- The `status IN ('pending','success')` clause matters: it ensures
-- failed / undone / reconciled_unknown rows do NOT continue to hold
-- the dedup slot, even if their logical_key_locked column hasn't
-- been cleared yet. (Defense in depth: the status-changing helpers
-- below ALSO clear the lock, but the predicate makes that redundant
-- rather than load-bearing.)
--
-- Crucially: a row's `status='success'` is preserved by the
-- expiry GC (only logical_key_locked flips), so all "find
-- successful action" queries (last_for_me, bot_known_events,
-- target_id lookups) keep working regardless of lock state.
CREATE UNIQUE INDEX bot_actions_logical_locked_uniq
  ON bot_actions (logical_key)
  WHERE logical_key_locked = true
    AND status IN ('pending', 'success', 'reconciled_unknown');

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

**Constraint names** (relied on by `insert_bot_action_pending` to
dispatch `UniqueViolation`s in §5.1):
- `bot_actions_message_action_uniq` — same `(message_id, action_type)`
  conflict. The conflicting row is the **same row** the caller is
  trying to write again; re-read by `(message_id, action_type)`.
- `bot_actions_logical_locked_uniq` — different message but same
  active logical_key. The conflicting row has a **different**
  `message_id`; re-read by `(logical_key)` filtered to
  `logical_key_locked = true`.

**How constraint names are extracted in this codebase**: `bot/db/
client.py` uses **supabase-py** (PostgREST), not raw `psycopg`.
PostgREST surfaces unique-constraint failures as HTTP 409 responses
whose JSON body looks like:

```json
{
  "code": "23505",
  "details": "Key (logical_key)=(...) already exists.",
  "hint": null,
  "message": "duplicate key value violates unique constraint \"bot_actions_logical_locked_uniq\""
}
```

So `db/queries.py:insert_bot_action_pending` does the dispatch by
**string-matching the constraint name in `error.message`** (or, more
robustly, parsing it out with a regex like
`r'unique constraint "([^"]+)"'`). The helper:

1. Tries the INSERT.
2. On HTTP 409, regex-extracts the constraint name from the message.
3. Looks up the existing row (by `(message_id, action_type)` for
   `bot_actions_message_action_uniq`, by `logical_key` filtered to
   `logical_key_locked = true` for `bot_actions_logical_locked_uniq`).
4. Raises `MessageActionConflict(existing_row)` or
   `LogicalKeyConflict(existing_row)` as appropriate.

**Defensive fallback**: if the message doesn't match either known
constraint name (e.g. PostgREST changes its phrasing in a future
version), the helper raises a generic `BotActionInsertConflict` with
the raw error attached. The skeleton's catch arms are written to
treat that as a hard error rather than silently choosing one branch.

If a future migration moves `bot/db/queries.py` to direct `psycopg`
or `asyncpg`, the same dispatch logic applies but reads
`exception.diag.constraint_name` directly — swap the regex for the
attribute access; nothing else changes.

**Lock behavior on status transitions** — NOT a uniform "always
clear":

| Transition | Final `logical_key_locked` | Reason |
|---|---|---|
| `pending → success` | `true` (until 60s GC) | The action completed; dedup window starts. |
| `pending → failed` | `false` (cleared) | The Feishu call errored cleanly; retry on the same logical_key is safe. |
| `pending → undone` | `false` (cleared) | The action was reversed; the logical request can be re-issued. |
| `pending → reconciled_unknown(stuck_pending)` (case A in §5.3) | `false` (cleared) | We don't know what happened on Feishu; further info needs human verification. Retry with a fresh request is allowed (will create a new row). |
| `pending → reconciled_unknown(partial_success)` (case B in §3.3 / §3.8) | **`true` (kept)** | Feishu artifact exists in a known location (`target_id` is set). A duplicate call would create a second artifact. Lock stays until undo runs (which transitions to `undone` and clears the lock). |

The previous v9 design cleared `logical_key_locked` on every
transition out of `pending`. That works for the three "no
artifact" terminals but **breaks for partial_success**: a duplicate
request would slip past dedup and create a second event/doc while
the first one still sits orphaned in Feishu. Iter-10 reviewer
caught this. Fix: split the helper into two (or pass `kind` to a
single helper) so partial_success preserves the lock.

```sql
-- mark_bot_action_failed
UPDATE bot_actions
   SET status='failed', error=$err,
       logical_key_locked=false, updated_at=now()
 WHERE id=$id;

-- mark_bot_action_undone
UPDATE bot_actions
   SET status='undone', logical_key_locked=false, updated_at=now()
 WHERE id=$id;

-- mark_bot_action_reconciled_unknown(kind)
-- 'stuck_pending' → clear the lock (we don't know if anything was done)
-- 'partial_success' → KEEP the lock (we know an orphan exists)
UPDATE bot_actions
   SET status='reconciled_unknown',
       error=$err,
       result = jsonb_set(
                  COALESCE(result,'{}'::jsonb),
                  '{reconciliation_kind}', to_jsonb($kind::text)),
       logical_key_locked = (CASE WHEN $kind = 'partial_success'
                                  THEN true ELSE false END),
       updated_at=now()
 WHERE id=$id;

-- get_bot_action lazy GC for stuck pending — always clears, since
-- by definition stuck-pending GC is the case-A path
UPDATE bot_actions
   SET status='reconciled_unknown',
       error='reconciled: pending too long',
       result = jsonb_set(
                  COALESCE(result,'{}'::jsonb),
                  '{reconciliation_kind}', '"stuck_pending"'),
       logical_key_locked=false,
       updated_at=now()
 WHERE id=$id AND status='pending'
   AND created_at < now() - interval '5 minutes';
```

**Implication for the partial UNIQUE index**: the predicate now
includes `'reconciled_unknown'` so partial_success rows participate
in the dedup index. This means `get_locked_by_logical_key` will
return them. The §5.1 skeleton's Phase 0 must handle this case —
see updated logic.

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
| Per-run context (`message_id`, `chat_id`, `sender_open_id`, `conversation_key`) via `RequestContext` dataclass owned by each `_PooledClient`, captured by closure in `build_pmo_mcp(ctx)` (see §5.0) | `bot/agent/tools.py` (`RequestContext` definition + factory), `bot/agent/runner.py` (one ctx per `_PooledClient`; `answer` / `answer_streaming` accept `message_id` / `chat_id` / `sender_open_id` and set `slot.ctx.*` inside the existing `slot.lock`), `bot/app.py` (calls `answer_streaming(...)` with the four params; **never** touches `_get_client` / `slot.ctx` / `slot.lock` directly) | new dataclass, factory pattern in `build_pmo_mcp`, runner public surface gains 3 kwargs, existing `set_current_conversation` removed entirely |
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
them everything 401s and no code change matters).

> ⚠️ **Do NOT paste any string marked `TBD` into the Feishu admin
> console without first running §11 step 0** (`lark-cli` verification).
> Feishu has renamed scopes between API versions, and a wrong string
> silently produces a 401 at runtime that's frustrating to track
> down. The `TBD` markers below indicate scope names whose exact
> spelling needs confirmation before paste.

- `im:*` (existing)
- `calendar:calendar` — own calendar mgmt
- `calendar:calendar.event:*` — create/update/delete events
- `calendar:calendar.event.attendee:*` — invite/remove attendees
- **TBD** (likely `calendar:calendar.free_busy:read` or
  `calendar:calendar.freebusy:read`) — conflict detection. Confirm in
  §11 step 0 via lark-cli skill manifest before pasting.
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
- For first-person calendar questions ("我下午有啥会", "我下周三有空吗"),
  call list_my_meetings with **no** `target` argument. The tool
  defaults to the asker via RequestContext. Never call resolve_people
  on the asker's own handle just to derive their open_id.
```

---

## 10. Identified omissions and how this spec handles them

A checklist run during brainstorming surfaced 12 issues a write-tool
agent commonly mishandles. For traceability:

| # | Risk | Mitigation in this spec |
| --- | --- | --- |
| 1 | Timezone ambiguity | `today_iso` returns `user_timezone` (§3.2); system prompt enforces RFC3339+offset (§9) |
| 2 | Webhook retry double-action | `bot_actions UNIQUE(message_id, action_type)` (§6.2) |
| 3 | Booking on top of existing meetings | `freebusy.batch` (NOT `.list`) pre-check **after** pending insert (§3.3 phase 2) |
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
| 29 | `cancel_meeting` had no compensating undo path, breaking the §1.4 trust model | Added `pre_cancel_event_snapshot` capture before delete (§3.4); undo dispatcher restores via `calendar_event.create` (§3.9). Caveats documented. Caught in iter-5 Codex review. |
| 30 | Logical-key dedup was a read-then-act race across processes | Replaced with a partial UNIQUE index `WHERE logical_key_locked = true` (§6.2); concurrent inserts are serialized by Postgres, not by app reads. Lazy GC flips `logical_key_locked` to false on success rows >60 s old (§5.3 case b). Caught in iter-5 Codex review; the column-vs-status decoupling came from iter-6. |
| 31 | Naïve "always insert pending first" leaked rows when validation failed | New explicit Phase -1 in tool body skeleton (§5.1) for pre-flight checks that may return without writing `bot_actions`. `append_action_items` ambiguous flow lives in Phase -1. Caught in iter-5 Codex review. |
| 32 | `list_my_meetings` required `user_open_id` but the LLM never has the asker's own open_id | Default `target` is `"self"`, resolved to `ctx.sender_open_id` (§3.5). Caught in iter-5 Codex review. |
| 33 | Spec mis-stated `contact.v3.user.batch_get_id` as a "by name" lookup; it actually accepts only emails / phones | Resolution chain split by input shape: profiles → emails/phones via `batch_get_id` → names via `contact.v3.user.search` (§3.1). Caught in iter-5 Codex review. |
| 34 | v5's `archived` status conflated "successful action" with "expired dedup lock", forcing every audit/restore/undo query to start filtering `status IN ('success','archived')` and risking combinatorial drift as new statuses get added | Decoupled into two columns: `status` stays pure (success / failed / undone / pending / reconciled_unknown) and a separate `logical_key_locked: bool` controls dedup-index membership. All "find successful action" queries keep `status='success'`, unaffected by the dedup window expiring (§6.2, §5.2, §5.3). Caught in iter-6 Codex review. |
| 35 | v5 leaked runner-pool internals to `app.py` (direct `_get_client` access + manual `slot.lock` + `slot.ctx` mutation) | `runner.answer_streaming(message_id, chat_id, sender_open_id, …)` is the public surface. The runner sets `slot.ctx` inside its existing `slot.lock` acquisition. `app.py` and other callers never touch private pool state (§5.0, §7, §11 step 5). Caught in iter-6 Codex review. |
| 36 | §8 scope table embedded a literal scope string (`calendar:calendar.freebusy:read`) whose exact spelling was deferred to §11 step 0 verification — implementers might copy it before verifying | §8 marks the scope `TBD — see §11 step 0`; a ⚠️ note tells implementers not to paste any TBD entry into the Feishu admin console until lark-cli verification has run. Caught in iter-6 Codex review. |
| 37 | Partial UNIQUE on `(logical_key) WHERE logical_key_locked=true` did not exclude failed/undone/reconciled_unknown rows; a `failed` row would block all retries on the same logical_key | Predicate now `WHERE logical_key_locked=true AND status IN ('pending','success')` (§6.2). Belt-and-suspenders: `mark_failed` / `mark_undone` / GC also clear `logical_key_locked` so non-active rows don't masquerade as locked. `update_for_retry` re-claims the lock by setting `logical_key_locked=true` when transitioning failed→pending (§5.1). Caught in iter-7 Codex review. |
| 38 | `insert_bot_action_pending`'s UniqueViolation handler only re-read by `(message_id, action_type)`, so a logical_key conflict with a different message_id returned a generic "in flight" instead of the winner's result | `insert_bot_action_pending` inspects `e.diag.constraint_name` and raises `MessageActionConflict` or `LogicalKeyConflict` carrying the appropriate existing row (§5.1, §6.2 constraint-name contract). Caught in iter-7 Codex review. |
| 39 | Spec used `freebusy.list` and `attendee.create_batch` — neither matches the lark-oapi Python SDK shape | `freebusy.batch` (§3.3 phase 2) and `attendee.create` with `body={attendees: [...]}` (§3.3 phase 4 / §3.9 cancel-restore). Verified against installed `lark_oapi/api/calendar/v4/resource/{freebusy,calendar_event_attendee}.py`. Caught in iter-7 Codex review. |
| 40 | `cancel_meeting(event_id)` only required bot-ownership; anyone with the link could cancel a meeting scheduled in another chat | Added cross-chat guard: `bot_actions.chat_id` must equal `ctx.chat_id` (§3.4). Refuse with explanation otherwise; v1 has no override. Caught in iter-7 Codex review. |
| 41 | Markdown-to-Docx import was described as a single `import_tasks.create` call with `body=Markdown bytes`; SDK actually requires upload-then-import-then-poll (3 steps) | §3.8 Path A rewritten as 3 steps: `file.upload_all` → `import_task.create(file_token=...)` → `import_task.get` poll. Verified against installed `lark_oapi/api/drive/v1/model/import_task.py`. Caught in iter-7 Codex review. |
| 42 | `UNIQUE (message_id, action_type)` was unnamed in SQL → Postgres auto-generates a name like `bot_actions_message_id_action_type_key`, which the regex dispatch in `insert_bot_action_pending` would not recognize. All same-message conflicts would silently fall through to the generic catch | SQL now uses `CONSTRAINT bot_actions_message_action_uniq UNIQUE (message_id, action_type)` (§6.2) so the name appears literally in PostgREST's 409 error message. Caught in iter-8 Codex review. |
| 43 | freebusy body field was named `user_id_list` in spec; SDK's `BatchFreebusyRequestBody.user_ids` would silently receive empty input, returning a 400 or no-op | Fixed to `user_ids: List[str]` per `lark_oapi/api/calendar/v4/model/batch_freebusy_request_body.py`. §3.3 phase 2 cites the model file directly. Caught in iter-8 Codex review. |
| 44 | Several "API endpoint" path strings in spec didn't match lark-oapi Python SDK attribute paths (`calendar_event.attendee.create` instead of `calendar_event_attendee.create`; `drive.v1.files` instead of `drive.v1.file`; `drive.v1.import_tasks` instead of `drive.v1.import_task`; `bitable.v1.app.table` instead of `bitable.v1.app_table`; `docx.v1.document.block.children` instead of `docx.v1.document_block_children`) | All paths corrected to SDK attribute style; new §3.3bis "API endpoint vs lark-oapi SDK attribute path" callout table maps URL paths to SDK paths so future implementers can spot a mismatch quickly. Caught in iter-8 Codex review. |
| 45 | `contact.v3.user.search` doesn't exist in lark-oapi Python SDK (verified in `lark_oapi/api/contact/v3/resource/user.py` — only `batch`, `batch_get_id`, `get`, `list`, `find_by_department` are exposed) | §3.1 step 3 now specifies a raw HTTP call to `/open-apis/search/v1/user` via `httpx` (already a dependency, see `feishu/client.py:67`) with `Bearer <tenant_access_token>`. Caught in iter-8 Codex review. |
| 46 | `schedule_meeting`'s Phase 2 had multiple sub-steps (create event, invite attendees) but a single coarse "any failure → mark_failed" arm. If event creation succeeded and attendee creation failed, the Feishu event persisted while `bot_actions.target_id` stayed NULL — retry would gleefully create a duplicate event | New Phase 2.2.5 "intermediate persist" step in §3.3: `target_id` written to `bot_actions` immediately after event creation. If a later sub-step fails, transition to `reconciled_unknown` (not `failed`) so retry is blocked and undo can target the orphan. §5.1 skeleton updated with explicit guidance for multi-step Phase 2 tools. Caught in iter-9 Codex review. |
| 47 | Calendar SDK calls (`calendar_event.create/get/delete`, `calendar_event_attendee.create`) all require `calendar_id` AND `event_id` path params (verified in `lark_oapi/api/calendar/v4/model/{create,get,delete}_calendar_event_request.py` and friends), but spec only said `event_id` | `bot_workspace.calendar_id` is read in §3.3 Phase 2.0; threaded through every Calendar call in §3.3 / §3.4 / §3.9. `result.calendar_id` is persisted so cancel/undo can re-read it without going back to `bot_workspace`. Caught in iter-9 Codex review. |
| 48 | `update_for_retry` could raise `LogicalKeyConflict` (when transitioning failed→pending re-acquires the slot but a different message has won it), but the §5.1 skeleton's catch was structurally only around `insert_bot_action_pending` in Phase 1b — Phase 1a's failed-retry branch had no catch | §5.1 skeleton wraps `update_for_retry` in its own `try/except queries.LogicalKeyConflict` with identical dispatch logic. Caught in iter-9 Codex review. |
| 49 | `list_my_meetings` needed the user's primary `calendar_id` to call `calendar_event.list`, but spec didn't say how to get it | §3.5 explicitly invokes `client.calendar.v4.calendar.primarys(user_ids=[target], user_id_type="open_id")` and pulls `calendar.calendar_id` from the response. Added to the §3.3bis SDK callout table. Caught in iter-9 Codex review. |
| 50 | Doc undo said `drive.v1.file.delete` but `DeleteFileRequest` requires both `file_token` and `type` (verified in `lark_oapi/api/drive/v1/model/delete_file_request.py`) | §3.8 records `target_id=<doc_token>` + `target_kind="docx"`; §3.9 calls `client.drive.v1.file.delete(file_token=target_id, type="docx")`. Caught in iter-9 Codex review. |
| 51 | §3.3bis listed freebusy URL as `/open-apis/calendar/v4/freebusy/batch_query`, but installed SDK uses `/freebusy/batch` (`lark_oapi/api/calendar/v4/model/batch_freebusy_request.py:25`) | §3.3bis row updated; verification cite added inline. Caught in iter-9 Codex review. |
| 52 | v9's `mark_bot_action_reconciled_unknown` cleared `logical_key_locked`, AND the partial UNIQUE predicate excluded `reconciled_unknown` — both rules combined meant a `partial_success` orphan did NOT block duplicate calls. A second message with the same logical request would create a second event/doc while the first orphan still sat in Feishu | (a) Predicate now `WHERE logical_key_locked = true AND status IN ('pending','success','reconciled_unknown')`. (b) `mark_bot_action_reconciled_unknown` takes a `kind` parameter; `partial_success` keeps the lock, `stuck_pending` clears it. (c) §5.1 Phase 0 explicitly handles a returned `reconciled_unknown(partial_success)` row by returning "please undo first" rather than re-running. Caught in iter-10 Codex review. |
| 53 | `create_meeting_doc` Path A's async `import_task` flow had a single coarse "any failure → mark_failed" path. After `import_task.create` returns a ticket, the import is still in flight server-side — polling failure / timeout doesn't mean the import didn't complete. Retry could create a duplicate Docx; undo would have no token to point at | §3.8 Path A rewritten with the same Phase 2.X.5 intermediate-persist pattern as §3.3: persist `source_file_token` after upload, persist `import_ticket` after import_task.create, persist `target_id=doc_token` after poll-success. Polling timeout / network error → `reconciled_unknown(partial_success)` so duplicates are blocked; definitive Feishu-side failure → `failed` with cleanup. 5-minute total poll timeout. Caught in iter-10 Codex review. |
| 54 | freebusy conflicts were stored as `status='failed'`, but Phase 1a's failed-status branch automatically reclaims and re-executes via `update_for_retry` — meaning a duplicate webhook would re-issue the freebusy call, and if the conflicting meeting got cancelled in the meantime, would silently schedule the new meeting (a behavior the user never reauthorized) | §3.3 Phase 2.1 now stores conflicts as `status='success'` + `result.outcome='conflict'` + `result.conflicts=[...]`. The idempotency check returns the cached conflict on retry without re-calling Feishu. The `outcome` discriminator on `result` is the same pattern used by `reconciliation_kind` in §5.3 — `status` retains pure semantics ("did the side-effect-or-decision land?") while richer business outcomes live in `result`. Caught in iter-10 Codex review. |
| 55 | §11 step 2's helper list didn't enumerate the new DB primitives v9 introduced (`record_bot_action_target_pending` for intermediate persist, `mark_bot_action_reconciled_unknown(kind=...)` for partial-success vs stuck-pending) — implementers reading the spec would build only the helpers v8 mentioned and discover the gap mid-implementation | §11 step 2 rewritten as a one-line-per-helper checklist covering all v10 primitives, including the iter-10 additions. Caught in iter-10 Codex review. |
| 56 | §3.3bis listed primarys URL as `/open-apis/calendar/v4/calendars/primary` (singular), but installed SDK uses `/calendars/primarys` (plural — `lark_oapi/api/calendar/v4/model/primarys_calendar_request.py:25`) | §3.3bis row corrected with cite. The SDK call snippet (`client.calendar.v4.calendar.primarys`) was already correct; only the URL column was wrong. Caught in iter-10 Codex review. |

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
2. **`db/queries.py`** — add helpers for `bot_actions`. The full
   set, with one-line job descriptions:
   - `get_bot_action(message_id, action_type)` — Phase 1a lookup.
   - `insert_bot_action_pending(...)` — Phase 1b insert. Distinguishes
     PostgREST 409s by constraint name (`bot_actions_message_action_uniq`
     vs `bot_actions_logical_locked_uniq`, both defined in §6.2) and
     raises `MessageActionConflict` / `LogicalKeyConflict` carrying
     the existing row.
   - `record_bot_action_target_pending(action_id, *, target_id,
     target_kind, result_patch)` — **NEW (§3.3 Phase 2.2.5, §3.8
     Phases 2.1.5/2.2.5/2.3.5)**: UPDATE the row with `target_id`,
     `target_kind`, and a JSONB `result` patch (deep-merge), without
     changing `status` (stays `pending`). Used after each successful
     intermediate Feishu call in a multi-step Phase 2 so a later
     failure has something to point at.
   - `update_for_retry(action_id, *, new_args, logical_key)` —
     transitions `failed → pending` and re-claims
     `logical_key_locked=true`; can raise `LogicalKeyConflict` if
     the slot was won by another caller.
   - `mark_bot_action_success(action_id, *, target_id, target_kind,
     result_patch)` — Phase 3 success terminal.
   - `mark_bot_action_failed(action_id, error)` — clears
     `logical_key_locked` (technical-error retry permitted).
   - `mark_bot_action_undone(action_id)` — clears the lock.
   - `mark_bot_action_reconciled_unknown(action_id, *, kind, error,
     target_id?, target_kind?, result_patch?)` — **NEW (§5.3 / §3.3
     Phase 2.3 / §3.8)**: takes `kind ∈ {"stuck_pending",
     "partial_success"}`. `partial_success` keeps
     `logical_key_locked = true` (orphan blocks duplicate);
     `stuck_pending` clears the lock. Writes
     `result.reconciliation_kind` per the §5.3 contract.
   - `last_bot_action_for_sender_in_chat(chat_id, sender_open_id, *,
     statuses=("success","reconciled_unknown"), require_target=True)` —
     used by `cancel_meeting(last:true)` and
     `undo_last_action(last_for_me)`. Must accept both `success` and
     `reconciled_unknown` rows so partial_success orphans can be
     undone (see §3.9).
   - `compute_logical_key(*, chat_id, sender_open_id, action_type,
     canonical_args)` — pure hash function.
   - `get_locked_by_logical_key(logical_key)` — Phase 0 lookup;
     must inline-flip `logical_key_locked=false` on success rows
     >60 s old before returning (lazy GC, §5.3 case b). Returns
     rows with `status IN ('pending','success','reconciled_unknown')`
     so partial_success orphans are visible to dedup.
   - `acquire_bootstrap_lock()` / `release_bootstrap_lock()` — §4
     workspace re-bootstrap mutex.

   Also add `bot_workspace` helpers: `get_bot_workspace()`,
   `update_bot_workspace(...)`.
3. **`feishu/client.py`** — wrap calendar/bitable/docx/contact endpoints.
4. **Create `bot/scripts/` directory + `bootstrap_bot_workspace.py`** —
   run once against dev env, verify the calendar/base/folder appear
   correctly. Re-runnable: detects existing workspace row and exits.
5. **Per-pooled-client `RequestContext` refactor** — touches three
   files; ship and bake **before** any new write tools land:
   - `bot/agent/tools.py`: define `RequestContext` dataclass; convert
     `build_pmo_mcp` from a no-arg helper to a factory
     `build_pmo_mcp(ctx)` whose tool implementations close over
     `ctx`. Remove the old `_current_conversation_key_var` global
     and `set_current_conversation`.
   - `bot/agent/runner.py`: each `_PooledClient` gets a fresh
     `RequestContext` at construction and passes it into
     `build_pmo_mcp(ctx)`. The public `answer` and `answer_streaming`
     functions gain `message_id`, `chat_id`, `sender_open_id`
     keyword parameters. Inside `answer_streaming`, after
     `async with slot.lock:` (the existing line at `runner.py:240`),
     assign all four request fields to `slot.ctx` BEFORE calling
     `slot.client.query(...)`. The `agent_tools.set_current_conversation`
     line at `runner.py:244` is removed in the same diff.
   - `bot/app.py`: update both call sites of `agent_runner.answer*` —
     the streaming path at `app.py:184` and the fallback `answer`
     path at `app.py:158-160` — to pass the three new keyword
     arguments. **Do not** import `_get_client`, `_PooledClient`,
     `slot.lock`, or `slot.ctx` from runner. The pool stays a private
     implementation detail of `runner.py`.
   - `bot/agent/imaging.py` does not change. The
     `generate_image` tool body inside `build_pmo_mcp(ctx)` already
     has `ctx` in scope; it passes `ctx.conversation_key` to
     `imaging.generate_and_upload`'s existing `conversation_key`
     kwarg.

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
    - **Cancel-then-undo restore**: schedule a meeting, then
      `cancel_meeting`, then `undo_last_action` → confirm a NEW
      Feishu event exists with the original title/time/attendees and
      a different `event_id`; agent's reply mentions
      `restore_caveats`. (§3.4 / §3.9 / §1.4.)
    - Logical_key dedup, single-process: ask the same scheduling
      request twice within 60 s; confirm the second call returns the
      prior result with `deduplicated_from_logical_key: true` and
      **no second meeting** appears in Feishu.
    - Logical_key dedup, simulated cross-process: send two webhooks
      with the same `logical_key`-producing content but different
      `message_id`s in rapid succession (use a script that POSTs
      directly to `/feishu/webhook` to bypass Feishu UI throttling).
      Confirm one INSERT succeeds, the other receives a
      `UniqueViolation` and falls through to "deduplicated" — no
      second meeting created.
    - Logical_key window expiry: schedule a meeting, wait >60 s,
      send the same request again. Confirm the second call DOES
      execute and creates a second meeting (the window has
      legitimately expired).
    - Group chat: user A schedules a meeting, user B says "取消刚才那个会"
      → bot must refuse / say it can only undo user B's own actions.
    - Ambiguous append: send "记一下要发邮件" with no project
      hint and no recent project activity. Confirm `needs_input:
      "project"` is returned, **no row in `bot_actions` exists**, and
      no rows in the action_items table appeared. Then provide a
      project; confirm the second call writes the rows cleanly.
    - Bootstrap recovery: manually delete the bot's Bitable base in
      Feishu, then issue a write request; confirm the bot self-heals
      (re-creates the base, posts the warning message, completes the
      original request). Run two such requests concurrently and
      confirm only **one** new base is created (the lock row in
      `bot_actions` mediates).

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
