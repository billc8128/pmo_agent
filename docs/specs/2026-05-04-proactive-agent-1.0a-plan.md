# Proactive PMO Agent 1.0a — Implementation Plan

- **Status**: Draft, branch `proactive-agent`
- **Date**: 2026-05-04
- **Spec**: [proactive-agent-1.0a-spec.md](2026-05-04-proactive-agent-1.0a-spec.md)
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)

This is the practical "how to land 1.0a" plan. It enumerates files
to add or change, in the order I'd commit them, with exit criteria
for each chunk. Time estimates are end-to-end including local
verification but not including waiting on Railway/Vercel deploys.

The spec is the source of truth for behaviour. This document is the
source of truth for **build order**.

---

## 0. Pre-flight (~10 min)

- [x] Branch created: `proactive-agent`
- [ ] Confirm Supabase migrations are at 0012 (latest is
      `0012_feishu_links_mobile.sql`)
- [ ] Confirm `bot/agent/runner.py` accepts `RequestContext` (already
      true — see existing code)
- [ ] Confirm `feishu_client` has `send_message` capability (it has
      reply_text/reply_card; we'll add `send_to_user` /
      `send_to_chat`)

---

## 1. Migrations (~30 min)

**Files**:
- `backend/supabase/migrations/0013_active_notifications.sql` —
  events, subscriptions, notifications, decision_logs, turn trigger,
  RLS
- `backend/supabase/migrations/0014_feishu_links_timezone.sql` —
  add `timezone` column

**Apply path**: via Supabase Management API (the same pattern used
for 0005-0006). Confirm both apply cleanly. Verify the trigger by
manually inserting a fake turn row in a transaction and rolling
back, observing the events row.

**Exit criterion**: tables exist, trigger fires, RLS denies anon
selects on the new tables.

---

## 1.5 RequestContext extension (~15 min)

Required before any subscription tool can be implemented (§5.0 of
spec).

**Files**:
- `bot/agent/request_context.py` — add `chat_type`, `asker_user_id`,
  `asker_handle` fields
- `bot/app.py::_handle_message` — populate them from
  `ParsedMessageEvent.chat_type` and the existing
  `db_queries.lookup_by_feishu_open_id` call (already present for
  the `[asker]` framing). Pass into agent runner's context.
- `bot/agent/runner.py` — accept the new fields in
  `answer_streaming(...)` kwargs, push to `slot.ctx`.

**Exit criterion**: a tool calling
`ctx.chat_type / ctx.asker_user_id` returns the right values for
both p2p and group messages.

## 2. Bot config + DB layer (~30 min)

**Files**:
- `bot/config.py` — add three settings:
  - `decider_loop_interval_seconds: int = 30`
  - `delivery_loop_interval_seconds: int = 15`
  - `notification_render_max_seconds: int = 60`
- `bot/db/queries.py` — new functions:
  - `fetch_events_needing_decision(limit)` — picks rows where
    `processed_at IS NULL` OR `processed_version < payload_version`
  - `mark_event_processed(event_id, payload_version)`
  - `fetch_enabled_subscriptions_for_scope(scope_kind, scope_id)` —
    returns ALL enabled rows for that scope (for sibling-rule
    decision context)
  - `get_notification(event_id, sub_id)` — for upsert-aware decider
  - `write_decision_log(... + input_tokens, output_tokens)`
  - `upsert_notification_row(...)` — overwrites pending decisions
    when payload_version increases; never overwrites `sent`
  - `fetch_pending_notifications(limit)`
  - `mark_notification_sent(id, msg_id, text)`
  - `mark_notification_failed(id, error)`
  - `recent_notifications_for_scope(scope_kind, scope_id,
    since_minutes)` — returns rows with `decided_at` so judge can
    do real timestamp math
  - `daily_sent_count_for_scope(scope_kind, scope_id,
    since_local_midnight)`
  - `lookup_notification_by_feishu_msg_id(msg_id)`
  - `add_subscription(scope_kind, scope_id, description, created_by, chat_id)`
  - `list_subscriptions(scope_kind, scope_id)`
  - `update_subscription(id, scope_kind, scope_id, **fields)`
  - `remove_subscription(id, scope_kind, scope_id)`
  - `feishu_link_for_user_id(user_id)` — returns open_id, name,
    timezone for renderer's mention/timezone logic
  - `resolve_subject_open_id(user_id)` — used by the new renderer
    tool `resolve_subject_mention`

All writes use `sb_admin()`.

**Exit criterion**: a unit-style smoke test from the Python REPL
inserts a fake event row, fetches it as unprocessed, decides
something, writes a notification, marks it sent — all paths exercise
without error.

---

## 3. Decider module (~45 min)

**File**: `bot/agent/decider.py` (new)

Responsibilities:
- Build the judge prompt (§4.1 of spec) from event + subscription +
  context
- Call the LLM (re-uses ARK Coding Plan via existing
  `ANTHROPIC_*` env), expecting JSON output
- Parse the JSON robustly (model may wrap in code fences)
- Return `Decision` dataclass

Public API:

```python
@dataclass
class Decision:
    send: bool
    matched_aspect: str
    preview_hint: str | None
    suppressed_by: str | None
    reason: str
    raw_input: dict
    raw_output: dict
    latency_ms: int
    model: str

async def decide(
    event: dict,
    subscription: dict,
    context: dict,
) -> Decision
```

Where `context` is built by the caller from
`recent_notifications_for_subscription` and `daily_count_for_owner`.

**Exit criterion**: synthetic event + synthetic subscription, judge
returns a coherent Decision JSON. Run 5 hand-picked cases that
should clearly send and 5 that should clearly suppress; eyeball
agreement.

---

## 4. Decider loop (~30 min)

**File**: `bot/agent/decider_loop.py` (new), wired into `app.py`'s
`lifespan`.

Wires §3.1 of the spec. Important details:

- Runs serially per iteration; uses `asyncio.create_task` only at
  the top level
- On any uncaught exception, log and `await asyncio.sleep(60)`
  before retrying — don't let one bad iteration spin the loop
- Sets `processed_at` only after every (event, sub) pair was either
  written or already-existed (idempotent skip). If any decision call
  threw, leave `processed_at` null so the next iteration retries
  the missing pairs.

**Exit criterion**: with one fake event in the DB and one
subscription matching it, the loop picks the event up within 30
seconds and writes a notification row.

---

## 5. Renderer module (~45 min)

**File**: `bot/agent/renderer.py` (new), and a tiny additional MCP
tool registration for `resolve_subject_mention`.

Public API:

```python
async def render_notification(
    notif_row: dict,
    event_payload: dict,
    subscription: dict,
) -> str  # markdown text
```

Implementation:
- Register a new read-only MCP tool `resolve_subject_mention` that
  wraps `queries.resolve_subject_open_id(user_id)` and returns
  `{ open_id, display_name }` or `{ open_id: null }`. Group renders
  need this for `<at user_id="...">` mentions; user-scope renders
  may also use it.
- Build a one-shot `ClaudeAgentOptions` with the renderer prompt
  (§4.2 of spec) and a curated tool subset:
    `list_users, lookup_user, get_recent_turns,
     get_project_overview, get_activity_stats, today_iso,
     resolve_subject_mention`
  Image generation, write tools, external readers, resolve_people
  are all explicitly NOT in this subset.
- No SDK client pooling — each render is independent and
  short-lived
- Hard timeout via `asyncio.wait_for` at
  `notification_render_max_seconds`

**Exit criterion**: given a real recent turn event payload, returns
a markdown string that reads like a coherent 200-400 char brief.

---

## 6. Feishu send-message client (~20 min)

**File**: `bot/feishu/client.py` — add two methods:

```python
async def send_to_user(self, open_id: str, post_content: dict) -> Optional[str]
async def send_to_chat(self, chat_id: str, post_content: dict) -> Optional[str]
```

Both call `/open-apis/im/v1/messages?receive_id_type=...` with
`msg_type=post`. Returns the new `message_id` on success.

Required scope: `im:message:send_as_bot` or
`im:message` — verify which one is already granted. If missing,
this is the third Feishu permission we need to apply for; surface
that to the user clearly.

**Exit criterion**: with a hardcoded test open_id (yours), a
hand-crafted post payload arrives in your DM.

---

## 7. Delivery loop (~30 min)

**File**: `bot/agent/delivery_loop.py` (new), wired into
`app.py`'s `lifespan`.

Wires §3.2 of spec. Each iteration:
- Fetch up to 20 pending notifications oldest-first
- For each: build event/subscription bundle, call renderer, call
  appropriate send method, mark sent
- On render error → mark failed (no retry yet)
- On send transient error → leave pending, will retry next
  iteration; on send permanent error → mark failed

**Exit criterion**: a notification row appearing in `pending` is
delivered to the right Feishu chat within ~30 seconds.

---

## 8. Subscription management tools (~30 min)

**File**: `bot/agent/tools_meta.py` (or wherever the meta MCP lives
today — confirm which file holds `today_iso` etc) — add four tools.

Each tool reads `RequestContext` to determine scope. Validations:
- `add_subscription` rejects if asker has no `feishu_links` row and
  scope_kind would be `'user'`
- `update_subscription` and `remove_subscription` verify scope
  ownership before acting

**Exit criterion**: from a private chat, "vibelive 进展告诉我" gets
a row written and "我都订了什么" lists it. From a group,
"@bot 订阅 vibelive 进展" creates a chat-scoped row.

---

## 9. Why-no-notification tool (~30 min)

**File**: same as §8.

Implementation:
- Fetch recent (default 24h) decision_logs for the asker's
  subscriptions
- Fuzzy-match `query` (Chinese substring search on
  `judge_input.event.payload.user_message` and `agent_summary`)
- Return up to 5 matched (event, decision) pairs with
  `suppressed_by` and `reason`

**Exit criterion**: ask "为什么没告诉我 albert 的播放器修改" — agent
calls the tool, gets a structured answer, surfaces it in human
language.

---

## 10. Reply-as-followup wiring (~30 min)

**File**: `bot/app.py` and `bot/feishu/events.py`

Steps:
- `events.py` — extend `ParsedMessageEvent` to expose
  `parent_message_id` if Feishu's payload includes it (it does, via
  `event.message.parent_id`)
- `app.py` — before framing the question, if `parent_message_id` is
  set, look up `notifications` by `feishu_msg_id`. If found, append
  a `[parent_notification]` block to `framed_question`.

**Exit criterion**: reply to a sent notification with "这次改动大不
大" — the agent's answer references the right turn / event without
needing a second tool round trip.

---

## 11. System prompt update (~15 min)

**File**: `bot/agent/runner.py`

Add a section to `_SYSTEM_PROMPT_TEMPLATE`:

- Document the four subscription tools and `why_no_notification`
- Document the `[parent_notification]` block (when present, the
  user is following up on that notification)
- Add scope inference rules ("在群聊里 add_subscription 默认绑到这个
  群；私聊里默认绑到自己")

**Exit criterion**: end-to-end conversational flows work: subscribe
in DM, subscribe in group, list, update, remove, ask "why no
notification".

---

## 12. End-to-end validation (~30 min)

Concrete script:

1. From your DM with the bot: "vibelive 项目有进展告诉我"
2. Verify a `subscriptions` row exists
3. Have albert run two turns in vibelive (real or simulated by
   inserting `turns` rows + agent_summary update)
4. Wait ≤ 90s; observe a notification arrive in your DM
5. Reply to that notification with "这次改动大不大?" — verify the
   bot answers coherently using the parent notification's context
6. Ask "为什么 albert 上一条 push 没告诉我" — verify either a real
   answer or a graceful "I don't have that event" response
7. Say "项目 C 不要发了" — verify a second subscriptions row
8. Pull a fake turn into project C — verify the decider writes a
   `suppressed: explicit_exclude` notification row, no Feishu push
9. Say "今晚别打扰我" — verify a third subscription row, and any
   subsequent events are suppressed with `quiet_hours`
10. From a group `@bot 订阅 vibelive 进展` — verify chat-scoped
    subscription, and a subsequent matching event reaches the group
    with a real `<at user_id="ou_...">` mention of the turn author
    (i.e. that `resolve_subject_mention` actually got called and
    the renderer used the real open_id, not just `@handle`).
11. **Sibling-rule veto regression**: with both "vibelive 进展告诉我"
    and "今晚别打扰" subscribed, fast-forward owner local clock into
    "tonight" range (or insert a turn during real night), verify
    the matching event is suppressed with
    `suppressed_by='quiet_hours'` even though the candidate
    subscription positively matches.
12. **Late-summary race regression**: insert a turn row with
    `agent_summary IS NULL`, observe one decision (likely
    suppressed `mismatch`); 30s later, UPDATE the turn to fill
    `agent_summary`; observe `events.payload_version` bumped to 2,
    decider re-considers, second decision sends.
13. **5-min dedup**: after step 4 succeeds, immediately insert
    another similar vibelive turn within 5 minutes; verify the
    second event is suppressed `duplicate_in_window` and references
    the first notification's `decided_at` in its `reason`.

If any step fails: triage in the order of decider prompt → judge
JSON parsing → renderer prompt → delivery wiring → tool
implementation. Update the spec where reality differs.

---

## 13. Commit + push

Single commit on `proactive-agent` branch with the message:

```
add proactive notifications: events / subscriptions / decider /
renderer / delivery (1.0a)

See docs/specs/2026-05-04-proactive-agent-1.0a-spec.md for the full
behaviour contract; this commit implements §2-§6 end to end. Group
+ user-scoped subscriptions managed via four new MCP tools.
```

Push to remote so deployment can pull from this branch (we don't
merge to main until 1.0a is validated against real usage and 1.0b
has at least the basic web visibility, otherwise users have no way
to see what they subscribed to other than asking).

---

## Cut points

If time pressure arrives:

- **Skip §9 (why_no_notification)**: the architecture supports it,
  decision logs are written, but the user-facing tool can wait.
- **Skip §10 (reply-as-followup)**: still works without it, just
  loses the "this" pronoun resolution.
- **Hard-code timezone to `Asia/Shanghai`**: skip the
  `feishu_links_timezone` migration, ignore user-decision #4 for
  now. Easy to add later.

Don't cut: migrations, decider, delivery, four subscription tools.
That's the irreducible 1.0a.
