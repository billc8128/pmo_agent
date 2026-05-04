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
  RLS, **and the six PL/pgSQL RPC functions from spec §2.8**
  (`claim_pending_notifications`, `mark_sent_if_claimed`,
  `mark_failed_if_claimed`, `release_claim`, `reap_stale_claims`,
  `upsert_notification_row`). These are how Python expresses
  `for update skip locked` and lease-conditional UPDATEs through
  PostgREST — Supabase's table API alone can't do that.
- `backend/supabase/migrations/0014_feishu_links_timezone.sql` —
  add `timezone` column

**Apply path**: via Supabase Management API (the same pattern used
for 0005-0006). Confirm both apply cleanly.

**Trigger-level smoke tests** (run before believing the migration
is good — this is the only place we touch a hot table on the daemon
upload path):

1. INSERT a fake turn row; verify a fresh `events` row appears with
   `payload_version = 1`. INSERT must not reference OLD; if the
   trigger's branch logic is wrong this is where it explodes.
2. UPDATE the same row to fill `agent_summary`; verify the events
   row's `payload` is updated AND `payload_version` is now 2 AND
   `ingested_at` is bumped.
3. UPDATE only an unrelated column (e.g. `device_label`); verify
   `payload_version` stays at 2 and `ingested_at` is unchanged.
4. DELETE the fake turn row; verify `events` is unaffected (we
   don't cascade — events are append-only as described in the
   roadmap invariant, only `payload` and version are mutable).

All four tests inside a single transaction, rolled back at the end,
so production data is untouched. Run via Supabase Management API
or psql.

**RPC ACL smoke tests** (run after the trigger tests, against the
deployed database — these verify that the SECURITY DEFINER
functions can't be invoked from the browser):

5. With the **anon** key: call `claim_pending_notifications` →
   expect `permission denied for function claim_pending_notifications`.
6. With the **anon** key: call `upsert_notification_row(...)` with
   any args → expect permission denied.
7. With the **service-role** key: call `reap_stale_claims(5)` →
   expect a successful return (likely `0` if no stale rows).
8. Same for `mark_sent_if_claimed`, `mark_failed_if_claimed`,
   `release_claim` — service-role can call (returns NULL on
   non-existent row), anon gets permission denied.

**claim_pending_notifications shape test** — the most complex new
RPC, directly feeds the renderer, easy to subtly break. Run as a
single transaction and roll back at the end so production data is
untouched:

9. Insert a fake `events` row (source='turn', payload={...},
    payload_version=1).
10. Insert a fake `subscriptions` row (scope_kind='user', some
    fake user_id with a profile, description='test').
11. Call `upsert_notification_row(event_id, sub_id, status='pending',
    decided_payload_version=1, payload_snapshot=<fake payload>)`.
12. Call `claim_pending_notifications(uuid_v4(), 5)` and assert
    the returned row contains:
    - a non-null `notification` jsonb whose `id` matches the
      pending row from step 11
    - a non-null `notif_payload_snapshot` jsonb that EQUALS the
      payload passed to upsert in step 11. To prove this is NOT a
      join from current `events.payload`, update only the `events`
      row's `payload` JSON between steps 11 and 12 **without
      bumping `events.payload_version`** (direct SQL against the
      `events` row, not a `turns` update that would fire the trigger),
      then confirm the snapshot is still the original.
    - `notif_payload_version` = 1
    - a non-null `subscription` jsonb whose `id` matches step 10

**Exit criterion**: all four trigger smoke tests pass; all four
RPC ACL smoke tests pass; the claim shape test passes (in
particular the snapshot-decoupled-from-events.payload assertion);
RLS denies anon SELECTs on events/subscriptions/notifications/
decision_logs.

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

## 1.6 OAuth callback: pull timezone (~10 min)

Spec §2.1 requires `feishu_links.timezone` to be populated from the
Feishu user_info response. The current callback ignores this field.

**File**: `web/app/api/feishu/oauth/callback/route.ts`

Steps:
- Add `let timezone: string | null = null;` alongside the other
  parsed fields
- Read `userJson.data?.timezone ?? null` after the userinfo fetch
- Add `timezone: timezone` to the upsert payload — the column name
  in spec §2.1 is just `timezone`, not `feishu_timezone`, even
  though the other Feishu-derived columns use the `feishu_` prefix.
  We accept the small naming inconsistency to keep the schema
  clean.
- Existing rows: `default 'Asia/Shanghai'` from the migration covers
  them; users who re-bind (or whose row is upserted by a future
  re-OAuth) get the real timezone.

**Exit criterion**: re-bind your own Feishu account and verify the
new column is set to a non-default value (assuming your Feishu
profile has a timezone configured).

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
  - `fetch_all_enabled_subscriptions()` — every `enabled = true`
    row across the whole DB; the decider groups by `(scope_kind,
    scope_id)` in memory and fans out per group. **Not** filtered
    by event scope — an event must reach every relevant subscriber
    regardless of where the event came from.
  - `fetch_subscriptions_for_scope(scope_kind, scope_id)` — used by
    the subscription tools (`list_subscriptions`) and renderer; not
    used in the decider fan-out path.
  - `get_notification(event_id, sub_id)` — for read-side checks
    (the upsert itself happens via RPC, so this is just for the
    decider's "skip if version already covered / sent / claimed"
    short-circuit before paying for a judge call)
  - `write_decision_log(... + payload_version, input_tokens, output_tokens)`
  - `upsert_notification_row(event_id, sub_id, decision,
                             decided_payload_version,
                             payload_snapshot)` — thin wrapper
    around the `upsert_notification_row` SQL RPC defined in spec
    §2.8. The RPC is a single-statement
    `INSERT … ON CONFLICT (event_id, subscription_id) DO UPDATE
    WHERE …` with the §2.4 rewrite predicate in the WHERE clause
    and a CTE that returns 'inserted' / 'updated' / 'noop'. No
    SELECT FOR UPDATE — concurrent decider workers safely race
    through the unique constraint. The decider passes
    `payload_snapshot=ev.payload` so the renderer (running later in
    the delivery loop) reads the SAME bytes the judge decided on,
    not whatever events.payload has mutated to in the meantime.
    Python helper just calls
    `sb_admin().rpc("upsert_notification_row", {...})`.
  - `claim_pending_notifications(claim_id, limit)` — RPC wrapper
    around the `claim_pending_notifications` SQL function (§2.8);
    that's where `for update of n2 skip locked` AND the version-match
    guard live. Returns each claimed row joined with the **frozen
    payload snapshot** from decision time (NOT current
    events.payload), the version that snapshot represents, and the
    full `subscription` row, so the delivery loop has everything
    the renderer needs without a second roundtrip AND with
    rendering decoupled from any subsequent mutations to events.

    The Python wrapper deserialises each RPC row into a
    `ClaimedBundle` dataclass:

    ```python
    @dataclass
    class Notification:
        id: int
        event_id: int
        subscription_id: str
        status: str
        decided_payload_version: int
        delivery_kind: str
        delivery_target: str
        suppressed_by: str | None
        # ... (all columns of the notifications table)

    @dataclass
    class Subscription:
        id: str
        scope_kind: str
        scope_id: str
        description: str
        enabled: bool
        # ... (all columns of the subscriptions table)

    @dataclass
    class ClaimedBundle:
        notification: Notification
        notif_payload_snapshot: dict      # jsonb → dict passthrough
        notif_payload_version: int
        subscription: Subscription
    ```

    The wrapper parses each RPC row's `notification` and
    `subscription` jsonb columns through the dataclass constructors
    so call sites use `b.notification.id`,
    `b.notification.decided_payload_version`, etc. — never raw
    `["id"]` lookups — keeping the delivery-loop code readable.
  - `release_claim(id, claim_id)` — RPC wrapper
  - `mark_sent_if_claimed(id, claim_id, msg_id, text)` — RPC
    wrapper; the SQL function returns NULL on lost lease so the
    Python helper can detect and warn.
  - `mark_failed_if_claimed(id, claim_id, error)` — RPC wrapper
  - `reap_stale_claims(stale_after_minutes=5)` — RPC wrapper,
    returns count reaped
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
- Cap repeated JSON parse failures: after 3 parse failures for the
  same `(event_id, subscription_id, payload_version)`, write
  `suppressed_by='judge_failure'` and settle that pair rather than
  retrying indefinitely.
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

@dataclass
class ScopeContext:
    """Owner state shared across every candidate in one scope."""
    owner_local_time: str             # e.g. "2026-05-04T23:42+08:00"
    owner_today_sent_count: int
    recent_notifications: list[dict]  # see spec §3.1 for shape

async def decide(
    event: dict,
    candidate: dict,
    siblings: list[dict],
    scope_ctx: ScopeContext,
) -> Decision
```

Per spec §3.1, every judge call must see ALL of the owner's
preferences (candidate + siblings) so exclusion / quiet-hours rules
written in a separate `subscriptions` row can veto a positive
match. The caller (decider loop) builds:
- `siblings` from the same `scope_subs` group, minus the candidate
- `scope_ctx.recent_notifications` from
  `recent_notifications_for_scope(scope_kind, scope_id,
  since_minutes=30)` — note: scoped to the OWNER, not to one
  subscription, since dedup is owner-level not subscription-level.
- `owner_today_sent_count` from `daily_sent_count_for_scope(...)`
- `owner_local_time` from the owner's timezone (user
  scope: feishu_links.timezone; chat scope: hardcoded
  Asia/Shanghai for 1.0a — chat-level timezone is a future feature)

`Decision.raw_input` must serialise the full bundle (candidate +
siblings + scope_ctx + event) so decision_logs can replay any
judgement without further DB lookups.

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
- On any uncaught exception at the *outer* level (DB connection
  loss, etc.), log and `await asyncio.sleep(60)` before retrying —
  don't let one bad iteration spin the loop
- Per-(event, candidate) errors are caught locally inside the
  inner loop. They set a per-event `had_unhandled_error` flag.
  Other pairs in the same iteration keep being processed.
- A second per-event flag, `had_blocking_claim`, is set whenever
  the decider sees an `existing.status == 'claimed'` whose
  `decided_payload_version < decided_version`. That row may
  release back to `pending` later (transient delivery failure) at
  the OLD version — so we must keep the event in the
  needs-decision set to rewrite that pending up to the current
  version.
- Subscriptions are forward-from-creation: before judging an event
  against a scope's subscriptions, filter out subscriptions whose
  `created_at` is later than `ev.ingested_at`. If that leaves no
  applicable candidates, the event can still be marked processed.
  This prevents a new subscription from cold-start fanout over all
  historical events and keeps the ≤90s E2E notification target
  meaningful.
- `mark_event_processed(ev.id, decided_version)` is called **only
  if both flags are false**. Pairs that already wrote a
  notification row at the current version are protected from
  re-judgement by the `decided_payload_version >= decided_version`
  guard, so retries only re-judge what actually needs it.

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
async def send_to_user(self, open_id: str, post_content: dict,
                        idempotency_uuid: str | None = None) -> Optional[str]
async def send_to_chat(self, chat_id: str, post_content: dict,
                        idempotency_uuid: str | None = None) -> Optional[str]
```

Both call `/open-apis/im/v1/messages?receive_id_type=...` with
`msg_type=post`. When `idempotency_uuid` is provided, also include
`uuid=<idempotency_uuid>` as a query parameter — Feishu uses it for
~1h server-side dedup of (app, receive_id, uuid) tuples. The
delivery loop (§7) sets this via
`stable_uuid_from_notif(notification.id, decided_payload_version)`,
so:
- a process crash between send and DB mark on the **same**
  payload_version doesn't double-send (same uuid → Feishu returns
  the original message_id);
- a v2 rewrite of the same notification row gets a **different**
  uuid and is delivered as a fresh message rather than getting
  silently dedupe-d into the v1 cached message.

Add a unit test that asserts
`stable_uuid_from_notif(42, 1) != stable_uuid_from_notif(42, 2)`
and that both are stable across calls.

Returns the new `message_id` on success. Note the Feishu API may
return the *previously-sent* message_id when an idempotent retry
hits the dedup cache; the delivery loop treats that as success and
the DB row gets the right msg_id either way.

Required scope: `im:message:send_as_bot` or
`im:message` — verify which one is already granted. If missing,
this is the third Feishu permission we need to apply for; surface
that to the user clearly.

**Exit criterion**:
- With a hardcoded test open_id (yours), a hand-crafted post
  payload arrives in your DM.
- Calling `send_to_user(...)` twice with the same
  `idempotency_uuid` results in only one message in your DM (and
  both calls return the same `message_id`).

---

## 7. Delivery loop (~30 min)

**File**: `bot/agent/delivery_loop.py` (new), wired into
`app.py`'s `lifespan`.

Wires §3.2 of spec. Each iteration:
1. Reap stale claims: any row in `claimed` for > 5 min flips back
   to `pending` (delivery worker probably crashed).
2. Atomically claim up to 20 `pending` rows: pending → claimed,
   stamping `claim_id` and `claimed_at`. Use
   `for update of n2 skip locked` so future multi-worker setups
   cooperate without unnecessarily locking joined `events` rows.
3. For each claimed row: render with the renderer (§5), send via
   `send_to_user` / `send_to_chat`, then `mark_sent_if_claimed`
   (UPDATE WHERE claim_id = ours AND status = 'claimed') so the
   commit fails cleanly if the lease was somehow lost.
4. On transient errors (5xx, network, 429) → release the lease
   (status back to pending, claim_id null) so next iteration retries.
   Do not perform inline sleep/backoff retries inside the row loop;
   otherwise a batch of transient failures can block the whole
   delivery loop for minutes.
5. On permanent errors (4xx non-rate-limit, renderer empty output,
   renderer timeout) → `mark_failed_if_claimed` (same lease guard).
6. On unexpected row-level exceptions (SDK parse error, malformed
   payload) → log with `notif.id`, release the lease immediately, and
   keep processing the remaining claimed rows.
7. Wrap the whole iteration in an outer `try/except Exception`; on
   DB/RPC/shape failures log, sleep 60s, and continue so the
   background `create_task` never dies silently.

The lease is what stops a stale `pending` (one that the decider
has since rewritten on a new payload version) from being delivered:
the rewrite rules in spec §2.4 say the decider **never** mutates a
`claimed` row, so once delivery has begun, the row's content for
this delivery is frozen. If the decider had already moved the row
to `claimed` before its rewrite would have happened, the rewrite is
a no-op; once delivery finishes (`sent` or `pending`-after-fail),
the next decider iteration sees the higher payload_version and
either freezes (sent) or rewrites (back to pending).

DB helpers needed in queries.py:
- `claim_pending_notifications(claim_id, limit)` — wraps the §2.8
  RPC that does the atomic pending → claimed transition AND returns
  the frozen `payload_snapshot` + subscription so the delivery loop
  has everything for the renderer in one trip without reading current
  `events.payload`. Returns
  `list[ClaimedBundle]`.
- `release_claim(notification_id, claim_id)` — flips `claimed`
  back to `pending` and clears the lease columns
- `mark_sent_if_claimed(notification_id, claim_id, msg_id, text)`
- `mark_failed_if_claimed(notification_id, claim_id, error)`
- `reap_stale_claims(stale_after_minutes=5)`

**Exit criterion**: a notification row appearing in `pending` is
delivered to the right Feishu chat within ~30 seconds. A second
test: insert a fake stale `claimed` row with `claimed_at` 10
minutes old; observe it reaped back to `pending` on the next
iteration. A third test: make `render_notification` raise an
unexpected `RuntimeError` for one claimed row; verify the delivery
loop logs, releases that row back to `pending`, continues processing
other rows, and is still alive for the next iteration.

---

## 8. Subscription management tools (~30 min)

**File**: `bot/agent/tools_meta.py` (or wherever the meta MCP lives
today — confirm which file holds `today_iso` etc) — add four tools.

Each tool reads `RequestContext` to determine scope. Validations:
- `add_subscription` rejects if `ctx.asker_user_id` is None or the
  asker has no `feishu_links` row, **regardless of scope_kind**.
  Per roadmap invariant + spec §5.0, you must be a bound pmo_agent
  user to subscribe at all — even when creating a chat-scoped
  subscription on behalf of a group, you (the creator) need an
  identity for `created_by`. If asker is unbound: tool returns an
  error message pointing them at `/me` to bind.
- `update_subscription` and `remove_subscription` verify the
  subscription's `(scope_kind, scope_id)` matches the current
  conversation scope before acting (you can't edit your DM subs
  from a group, or vice versa). Asker still must be bound.

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
- For each matched event, group decision_logs by
  `(event_id, subscription_id)` and order by `created_at` ascending
  — surfacing the version timeline so a (v1 mismatch, v2 send)
  sequence shows up as one timeline rather than two unrelated
  results
- Return up to 5 (event, subscription) groups, each with:
    - the event's payload summary
    - the subscription's description
    - the timeline:
      `[{payload_version, created_at, send, suppressed_by, reason,
         judge_output}, …]`
    - the *current* notifications row status (sent / suppressed /
      claimed / pending / failed) so the agent knows whether the
      eventual outcome was delivery
- Do NOT return token-heavy fields (`judge_input.event.payload`,
  `judge_input.candidate.full_text`) — the timeline is for the
  agent to summarise, not for the user to see verbatim.

**Exit criterion**: ask "为什么没告诉我 albert 的播放器修改" — agent
calls the tool, gets a structured timeline, summarises it in
human language: "v1 时 summary 还没生成，被判 mismatch；v2 时 summary
到了但你的'凌晨别打扰'触发了 quiet_hours，所以最终没发。"

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

12b. **Stale-pending-not-claimed regression** (covers spec §2.8
    version-match guard in claim_pending_notifications): write a
    `pending` notification at v1, immediately mutate the underlying
    turn so `events.payload_version` becomes 2 BEFORE the delivery
    loop runs. Run delivery once and assert that `claim_pending_…`
    returns zero rows (the v1 pending was filtered by the version
    guard). Then run decider once; assert the v1 pending is
    rewritten in place to v2 (or to suppressed/v2 depending on
    judge verdict). Run delivery again; assert it now claims the
    v2 row and sends. Without the version guard, delivery would
    have claimed v1 in step one and sent stale content.
13. **5-min dedup**: after step 4 succeeds, immediately insert
    another similar vibelive turn within 5 minutes; verify the
    second event is suppressed `duplicate_in_window` and references
    the first notification's `decided_at` in its `reason`.
14. **had_blocking_claim regression** (covers spec §3.1's stale
    claimed handling): walk the system through this exact sequence
    and assert each waypoint:
    a. Insert a vibelive turn with `agent_summary` already filled
       in. Wait for the decider to write a `pending` notification
       at `decided_payload_version=1` and for the delivery loop to
       claim it (`status='claimed'`). Pause delivery before it
       calls Feishu (e.g. set a breakpoint, or run delivery with
       a stub renderer that hangs).
    b. While the row is `claimed` at v1, UPDATE the turn to change
       its `agent_summary` to materially different content. Verify
       `events.payload_version` advanced to 2 (per the trigger's
       fingerprint logic).
    c. Run one decider iteration. Assert:
       - `had_blocking_claim` was set true for this event
         (instrument the decider log, OR verify by checking
         `events.processed_version` is still 1, NOT 2)
       - The `claimed` notification row was NOT touched
       - `events.processed_at` is NOT updated to a fresh timestamp
         tied to v2
    d. Release the renderer stub so delivery completes. Two
       sub-cases:
       - Delivery succeeds → notification becomes `sent`,
         frozen at v1 forever (we can't unsend). Run decider again,
         assert `events.processed_version` advances to 2 with the
         frozen-sent row left untouched.
       - Delivery transient-fails → notification falls back to
         `pending` at v1. Run decider again, assert
         `had_blocking_claim` is now FALSE (no claim), the v1
         pending row gets rewritten via `upsert_notification_row`
         to v2, and `events.processed_version` advances to 2.
    Without the `had_blocking_claim` flag, sub-case (d.transient)
    would deadlock — the row stays at v1 pending forever because
    the event already got marked processed at v2 in step (c).

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
