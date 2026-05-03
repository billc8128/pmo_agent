# Proactive PMO Agent 1.0a — Spec

- **Status**: Draft for implementation
- **Date**: 2026-05-04
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Plan**: [proactive-agent-1.0a-plan.md](2026-05-04-proactive-agent-1.0a-plan.md)

This spec describes the first stage of the proactive PMO bot. It is
the **source of truth** for 1.0a's data model, decision rules, and
tool contracts. When implementation diverges, update this file.

Conventions established in earlier specs (forward-only migrations,
public-by-default, daemon → Supabase REST → bot, etc.) carry forward
unchanged.

---

## 1. Scope

What 1.0a delivers, restated for precision:

- A **subscriptions** layer that records each user's (or chat's)
  natural-language preferences for what they want to be told about.
- An **events** ingest layer that turns every new `turns` row into
  an event consumable by the proactive pipeline. (No GitHub yet.)
- A **decider** background process that, for each new event and
  each enabled subscription, asks an LLM: should this go out?
- A **renderer** that runs an existing-agent-style loop on approved
  decisions to produce the user-facing notification text.
- A **delivery** layer that pushes the rendered text to the right
  Feishu chat, using a new send-message client method (separate from
  the reply path the bot already has).
- Four new agent tools so users can manage subscriptions in chat:
  `add_subscription`, `list_subscriptions`, `update_subscription`,
  `remove_subscription`.
- A **why_no_notification** tool the agent can use to answer
  "why didn't you tell me about X" by reading decision logs.
- A small extension to the agent's existing `[asker]` framing: when
  a user replies to a previous notification, the parent
  notification's payload is appended to the prompt so follow-ups
  are coherent.

What 1.0a does **not** deliver — see the roadmap §2.

---

## 2. Data model

Two existing tables are touched; four new tables are added. All new
tables use `service_role` for bot writes; reads use `service_role`
for cross-user lookups (matching the existing `feishu_links`
pattern).

### 2.1 `feishu_links` — add timezone

```sql
alter table feishu_links
    add column timezone text not null default 'Asia/Shanghai';
```

The OAuth callback already pulls `userJson.data.timezone` if
present; we plumb that through. Default is the most common case for
this team (also matches user decision #4).

### 2.2 `events` — append-only signal stream

```sql
create table events (
    id            bigserial primary key,
    source        text not null,                -- 'turn' for now
    source_id     text not null,                -- (source, source_id) is unique
    user_id       uuid references profiles(id), -- subject of the event, if known
    project_root  text,                         -- canonical project, if applicable
    occurred_at   timestamptz not null,
    ingested_at   timestamptz not null default now(),
    processed_at  timestamptz,                  -- null = not yet decided on
    payload       jsonb not null,
    unique (source, source_id)
);

create index events_unprocessed_idx
    on events (ingested_at)
    where processed_at is null;
```

The `processed_at` column is the watermark for the decider loop.
Once the decider has fanned an event out to all enabled
subscriptions, it sets `processed_at = now()`.

For 1.0a only one source exists: `source = 'turn'`, `source_id =
turns.id::text`. The trigger lives in §2.5.

### 2.3 `subscriptions` — natural-language preferences

```sql
create table subscriptions (
    id           uuid primary key default gen_random_uuid(),
    scope_kind   text not null check (scope_kind in ('user', 'chat')),
    scope_id     text not null,
    description  text not null,
    enabled      boolean not null default true,
    created_by   uuid references profiles(id),  -- profile that created it
    chat_id      text,                          -- where it was created (for audit)
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    -- Soft index: helps the decider's "fetch all enabled subs for owner"
    constraint subs_scope_ck check (
        (scope_kind = 'user'  and scope_id ~ '^[0-9a-f-]{36}$') or
        (scope_kind = 'chat'  and length(scope_id) > 0)
    )
);

create index subs_scope_enabled_idx
    on subscriptions (scope_kind, scope_id)
    where enabled = true;
```

A "subscription" is the user's verbatim phrase. We do not parse it
into rules. The decider re-reads it on every event.

`scope_kind = 'user'` means "deliver to this profile's DM with the
bot". `scope_kind = 'chat'` means "deliver to this Feishu chat".
The corresponding `scope_id` is the profile's UUID or the Feishu
`chat_id`.

`created_by` and `chat_id` exist purely for audit / display and are
NOT used in routing logic.

### 2.4 `notifications` — one row per decision

```sql
create table notifications (
    id              bigserial primary key,
    event_id        bigint not null references events(id) on delete cascade,
    subscription_id uuid   not null references subscriptions(id) on delete cascade,
    status          text   not null check (status in (
                       'pending',          -- decided send, awaiting render+push
                       'sent',             -- delivered to Feishu
                       'suppressed',       -- decider said no, kept for audit
                       'failed'            -- render or push errored permanently
                     )),
    suppressed_by   text,                  -- 'duplicate_in_window' / 'quiet_hours' /
                                           -- 'daily_cap' / 'explicit_exclude' / null
    rendered_text   text,                  -- final user-facing markdown
    feishu_msg_id   text,                  -- set after successful send
    delivery_kind   text,                  -- 'feishu_user' | 'feishu_chat'
    delivery_target text,                  -- open_id or chat_id
    decided_at      timestamptz not null default now(),
    sent_at         timestamptz,
    error           text,
    -- Idempotency guard: at most one notification per (event, subscription)
    constraint notif_event_sub_uniq unique (event_id, subscription_id)
);

create index notif_pending_idx
    on notifications (decided_at)
    where status = 'pending';

create index notif_recent_per_subscription_idx
    on notifications (subscription_id, decided_at desc);
```

`suppressed_by` exists so the "why didn't you tell me" path can
surface a real reason without parsing free text.

`delivery_kind` and `delivery_target` are denormalised from
subscription scope at decision time so the row is self-contained
even if the subscription is later edited or deleted.

### 2.5 `decision_logs` — every judge call

```sql
create table decision_logs (
    id              bigserial primary key,
    event_id        bigint not null references events(id) on delete cascade,
    subscription_id uuid   not null references subscriptions(id) on delete cascade,
    judge_input     jsonb  not null,
    judge_output    jsonb  not null,
    model           text   not null,
    latency_ms      int,
    created_at      timestamptz not null default now()
);

create index decision_logs_event_sub_idx
    on decision_logs (event_id, subscription_id);

create index decision_logs_subscription_recent_idx
    on decision_logs (subscription_id, created_at desc);
```

Always written, even when the decider decides not to send. This is
how prompt iteration becomes data-driven and how the
`why_no_notification` tool works.

### 2.6 Turn → events trigger

```sql
create function on_turn_to_event() returns trigger as $$
begin
    insert into events (source, source_id, user_id, project_root,
                        occurred_at, payload)
    values (
        'turn',
        new.id::text,
        new.user_id,
        new.project_root,
        new.user_message_at,
        jsonb_build_object(
            'turn_id', new.id,
            'agent', new.agent,
            'project_path', new.project_path,
            'project_root', new.project_root,
            'user_message', new.user_message,
            'agent_summary', new.agent_summary,
            'user_message_at', new.user_message_at
        )
    )
    on conflict (source, source_id) do update
        set payload = excluded.payload,
            ingested_at = now();
    return new;
end $$ language plpgsql;

create trigger turns_to_events
    after insert or update on turns
    for each row execute function on_turn_to_event();
```

We trigger on UPDATE too because `agent_summary` is filled in
asynchronously by the summarise edge function — we want the latest
payload, not the empty insert.

The trigger is **idempotent** (`on conflict do update`). If we
re-trigger or replay, we don't fan out twice — the decider's
per-event watermark + per-(event, subscription) unique constraint
handle that.

### 2.7 RLS

All four new tables get RLS enabled. Policies:

- `events` — no anon read; service role only (the bot is the only
  reader)
- `subscriptions` — owner-readable: a `user`-scoped subscription is
  visible to the owning profile (RLS on `auth.uid() = scope_id`); a
  `chat`-scoped subscription is visible to anyone who can read the
  chat (deferred until we have web UI; for 1.0a the bot writes via
  service role and reads via service role)
- `notifications` — owner-readable (same pattern as subscriptions)
- `decision_logs` — service role only (debug data, not for users)

For 1.0a all access is via the bot's service-role client. RLS
policies for direct user access are added in 1.0b alongside the web
UI.

---

## 3. Pipelines

### 3.1 Decider loop

Runs in the bot process as an `asyncio.create_task(...)` started in
`lifespan`. Polls `events` every **30 seconds**.

```
async def decider_loop():
    while True:
        await asyncio.sleep(30)
        events = fetch_unprocessed_events(limit=100)
        for ev in events:
            for sub in fetch_enabled_subscriptions():
                # Idempotency: skip if a notification row already
                # exists for this (event, sub) pair.
                if notification_exists(ev.id, sub.id):
                    continue
                decision = await judge(ev, sub, context_for(sub))
                write_decision_log(ev, sub, decision, model, latency)
                write_notification_row(ev, sub, decision)
            mark_event_processed(ev.id)
```

`context_for(sub)` is the bundle the judge needs:

- The subscription's full text
- The subscription's other recent notifications (last 30min) so the
  judge can dedup
- The subscriber's daily count (for daily-cap awareness)
- The subscriber's wall clock in their timezone (for quiet hours)
- Whether the event subject (`event.user_id`) is the same as the
  subscription owner (so the judge can apply the user's rule about
  self-events; default is to send, since user decision #2)

Errors during decision → log, mark this (event, sub) skipped (do
not write a notification row), do not block the rest of the batch.
Next loop iteration retries because `processed_at` only flips when
all subs for that event finished without error.

Concurrency: the loop runs strictly serially within one bot
process. For 1.0a we don't run multiple bot replicas, so no
distributed-lock concern. (When we do, the unique constraint on
`notifications(event_id, subscription_id)` is the cheap dedup; lock
acquisition can be added then.)

### 3.2 Renderer / delivery loop

Separate loop, polls `notifications` with `status = 'pending'`
every **15 seconds**.

```
async def delivery_loop():
    while True:
        await asyncio.sleep(15)
        rows = fetch_pending_notifications(limit=20)
        for row in rows:
            try:
                text = await render_notification(row)
                msg_id = await deliver(row, text)
                mark_sent(row.id, msg_id, text)
            except Exception as e:
                mark_failed_or_retry(row, e)
```

`render_notification` is an agent invocation:

- System prompt: see §4
- User message: structured payload — event payload + subscription
  description + scope hint
- Tools available: same set as the existing question-answering
  agent (read-only — list_users, lookup_user, get_recent_turns,
  get_project_overview, get_activity_stats, today_iso). Write tools
  (calendar / bitable / doc) are explicitly disallowed during
  rendering. The renderer must not take side effects.
- Output: markdown text, post-processed via the existing
  `markdown_to_post` and sent as a `post` message (to keep parity
  with the existing answer style).

`deliver` resolves `delivery_kind`:

- `feishu_user` → call `client.send_to_user(open_id, post_content)`
  (a new method on the existing `FeishuClient`)
- `feishu_chat` → call `client.send_to_chat(chat_id, post_content)`

Failures: classify as transient (5xx, network) → retry up to 3
times with backoff inside the same loop iteration; permanent (4xx
non-rate-limit) → mark `failed` with `error`. Rate-limit (429) →
back off for 60s then continue the loop.

### 3.3 Why these are two loops

Splitting decider and delivery makes each easier to reason about:

- Decider is CPU-cheap, LLM-bound at constant low cost (Haiku-sized).
- Delivery may be I/O-heavy (rendering with full tool calls, then
  Feishu round-trips).
- A slow render doesn't delay other decisions.

They communicate only through the `notifications` table. Either
loop can be restarted, killed, or replaced without the other
noticing.

---

## 4. LLM prompts

### 4.1 Judge prompt

A single prompt, ~600 tokens. Filled with a small Python format
function. Pseudocode:

```
你是 pmo_agent 的通知决策器。给你一条新事件、订阅人的偏好列表、
以及最近的通知历史。判断要不要给这个订阅发通知。

## 当前订阅

  scope: {user | chat}
  description: "{用户原话}"
  owner_local_time: {wall clock in their timezone}
  owner_today_count: {他们今天已收到的通知数}
  owner_recent_subjects: [...过去 30 分钟收到过的通知主题摘要...]

## 事件

  source: {turn}
  occurred_at: {ISO}
  subject_user: {handle, 或 "未绑定"}
  project_root: {...}
  is_subject_the_owner: {true | false}
  payload:
    user_message: "{用户输入的 prompt}"
    agent_summary: "{一句话总结}"

## 决策原则

1. description 是用户原话，按字面意思 + 合理引申理解。
2. 排除规则覆盖匹配规则（"项目 C 不要" 压过 "团队进展告诉我"）。
3. 静音时段：description 提到不要打扰的时段、且 owner_local_time
   在该时段内 → send=false, suppressed_by="quiet_hours"。
4. 5min 去重：如果 owner_recent_subjects 里有同主题的近期通知
   → send=false, suppressed_by="duplicate_in_window"。
5. 每日上限：如果 owner_today_count >= 20，且 description 没有
   明确说"重要事件 break through"
   → send=false, suppressed_by="daily_cap"。
6. 拿不准 → send=false。

## 输出 JSON

{
  "send": bool,
  "matched_aspect": "description 里哪一块匹配的（一句话）",
  "preview_hint": "如果 send=true，1 句话告诉渲染阶段重点写什么",
  "suppressed_by": "duplicate_in_window | quiet_hours | daily_cap |
                    explicit_exclude | mismatch | null",
  "reason": "一句话说明判断依据"
}
```

Model: ARK Coding Plan endpoint (same backend as the conversational
agent, per user decision #3). We use the lightest model available
on that endpoint and fall back to the default if not configured.

### 4.2 Renderer prompt

The renderer reuses the existing agent runner machinery
(`ClaudeSDKClient` + tool MCPs) but with a different system prompt.
Key differences from the question-answering prompt:

```
你是 pmo_agent 的 PMO 小助理。这次不是用户提问 — 是有一条事件
触发了用户的某个订阅，host 让你写一条主动通知。

## 当前任务

事件:
  {event payload, source, occurred_at, project_root, subject}

订阅:
  scope: {user | chat}
  description: "{原话}"
  preview_hint: "{judge 阶段的提示}"

## 输出格式

写一段 200-400 字的通知正文，markdown 可用。要求：
- 直接说事，不要 "我来告诉你" / "让我看看" 这种空话
- 重点写 [1] 改了什么 [2] 思考 / 技术方案。后者从 turn 上下文
  里挖（用 get_recent_turns / get_project_overview）
- 群通知里提到事件主体时用 飞书 mention 语法
  `<at user_id="{open_id}"></at>`，但前提是从 feishu_links 能查
  到那个人的 open_id；查不到就用 @handle 文字
- 末尾加一行小字写"—— 来自订阅 {description 的一句话摘要}"
  让用户知道为什么收到这条
- 不要加 [IMAGE:] 标记 — 主动通知里不生图（避免突然的视觉打扰）

## 不能做

- 不能调写工具 (schedule_meeting / append_action_items 等)
- 不能编内容 — 只用工具返回的事实
- 不能透露 user_id (UUID)
```

The renderer can call any read-only tool; image generation is
disabled for this path (the renderer's allowed_tools list omits it).

---

## 5. Subscription tools

Four new MCP tools are added to the existing `tools.py`. They
follow the same `_ok` / `_err` wrapper convention.

### 5.1 `add_subscription`

```python
@tool(
    "add_subscription",
    "Save a new natural-language subscription preference for the "
    "current asker. Use when the user says things like 'X 项目有进展告诉我' "
    "or '项目 C 不要发了' or '凌晨别打扰'. The description is stored "
    "verbatim — do NOT paraphrase or extract structured rules. \n\n"
    "Scope is inferred from the conversation: in a private chat the "
    "subscription belongs to the asker; in a group it belongs to the "
    "group (delivery target = group chat). The host injects the right "
    "scope based on chat_type.",
    {"description": str},
)
async def add_subscription(args: dict) -> dict:
    ...
```

The tool uses `RequestContext` (already populated in 1.0a's
`_handle_message`) to read `chat_type`, `chat_id`, and the asker's
`user_id`. The owner profile must have a `feishu_links` row — if
not, return an error explaining the user must bind first.

### 5.2 `list_subscriptions`

Returns the current scope's subscriptions. In a private chat, that's
the asker's user-scoped subs. In a group, it's the chat's
chat-scoped subs.

### 5.3 `update_subscription` and `remove_subscription`

Both take `id: str`. Both check that the subscription belongs to
the current scope before acting (so you can't `remove` someone
else's subscription by guessing UUIDs).

`update_subscription` only allows changing `description` and
`enabled` — scope is immutable.

### 5.4 `why_no_notification`

```python
@tool(
    "why_no_notification",
    "Look up why a particular event didn't trigger a notification. "
    "Use when the user asks 'why didn't you tell me about X' or "
    "'我没收到 vibelive 那条 push 的通知'. Searches recent decision "
    "logs for the asker's subscriptions, returns matched events with "
    "the suppressed_by reason and the judge's explanation.",
    {"query": str, "since_iso": str},
)
async def why_no_notification(args: dict) -> dict:
    ...
```

Implementation: fuzzy-match `query` against recent events' payloads
+ `decision_logs.judge_output.reason` for the asker's subscriptions
in the given time window (default 24h).

---

## 6. Reply-as-followup behaviour

Today the existing `_handle_message` doesn't look at
`parent_message_id`. We add:

```
if ev.parent_message_id:
    parent_notif = lookup_notification_by_feishu_msg_id(
        ev.parent_message_id
    )
    if parent_notif:
        framed_question += render_notif_context_block(parent_notif)
```

`render_notif_context_block` produces:

```
[parent_notification] (the user is replying to this notification)
  event: turn id=42, project=vibelive, subject=albert
  payload_summary: "albert 调了播放器 buffer ..."
  notif_text: "{the actual notification text we sent}"
```

This injects the prior notification into the conversation so when
the user says "这次改动大不大" the agent already has "this" pinned
to that turn.

---

## 7. Cost / latency budget

Numbers based on user decisions and a small team (5 people):

- Daily turn volume: ~200
- Active subscriptions per person: 3-5 (assumed)
- Active group subscriptions: ~2-3
- Total subscriptions: ~25
- Decisions per day: 200 × 25 = **5000**
- Average decision tokens: 1.5k input + 100 output

At ARK Coding Plan rates (approx Anthropic Haiku class, but pricing
is bundled), this fits comfortably within whatever monthly cap the
plan provides. We log per-decision token usage in `decision_logs`
and revisit if it exceeds budget.

Latency target:
- Decider: 30s loop + 0.5-1s/decision sequential ≤ 1 minute from
  turn write to decision write. Acceptable.
- Renderer: 5-15s per notification. Acceptable.
- End-to-end: turn → notification ≤ 2 minutes.

---

## 8. Open questions deferred to 1.0b/c

These are intentionally NOT decided in 1.0a. The architecture
permits any of these answers; we wait for real usage to choose.

1. **Pre-filter before LLM judge?** A coarse rule (e.g. project
   match by string) would cut decision volume 80%+. We don't add
   one in 1.0a so we can measure baseline judge accuracy
   uncontaminated.
2. **Multi-replica bot?** Today single-process. When we scale, the
   `(event_id, subscription_id)` unique constraint suffices for
   dedup, but lock-aware loop ownership is a future concern.
3. **Notification editing?** If a turn UPDATEs after we sent a
   notification (because `agent_summary` arrives late), do we patch
   the sent message? 1.0a: no — fire and forget. Revisit later.
4. **Cross-team subscriptions?** "Frontend group" as a subscription
   subject. Out of scope until we have group metadata.
