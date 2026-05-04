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

The default is `Asia/Shanghai` (per user decision #4 — the most
common case for this team). The OAuth callback today does NOT
extract `timezone` from the Feishu user_info response (see current
`web/app/api/feishu/oauth/callback/route.ts`); part of this slice
is to extend that callback to also read `userJson.data.timezone`
and include it in the `feishu_links` upsert. Plan §1.7 covers
that change.

If Feishu's user_info doesn't return a timezone for a given user
(some accounts don't set one), the column stays at its default and
the user can later override it via the web UI in 1.0b.

### 2.2 `events` — append-only signal stream

```sql
create table events (
    id            bigserial primary key,
    source        text not null,                -- 'turn' for now
    source_id     text not null,                -- (source, source_id) is unique
    user_id       uuid references profiles(id), -- subject of the event, if known
    project_root  text,                         -- canonical project, if applicable
    occurred_at      timestamptz not null,
    ingested_at      timestamptz not null default now(),
    processed_at     timestamptz,                       -- null = not yet decided on
    processed_version int default 0,                    -- which payload_version was decided on
    payload_version  int not null default 1,            -- bumped each time payload mutates
    payload          jsonb not null,
    unique (source, source_id)
);

-- Pick up: never-processed events AND events whose payload was
-- updated after the last decision (e.g. agent_summary arrived late).
create index events_unprocessed_idx
    on events (ingested_at)
    where processed_at is null or processed_version < payload_version;
```

The decider's watermark is **(processed_at IS NULL) OR
(processed_version < payload_version)**. The `payload_version`
counter exists because turn events get an empty `agent_summary`
on insert and the real summary arrives via UPDATE 5-30s later from
the summarise edge function. Without versioning, we'd either:
- decide on the empty summary → notification missing the punch line
- block forever waiting → notifications never go out for turns the
  summariser failed on

Versioning lets us decide once on what we have, then **re-decide
when the payload becomes meaningfully better**. The trigger §2.6
bumps `payload_version` only when fields the decider actually reads
change (`agent_summary`, `agent_response_full`) — not on every
trivial update.

**Notification rewrite rules** (enforced in `upsert_notification_row`):

| Existing status | New decided version | Action |
|-----------------|---------------------|--------|
| (no row)        | any                 | INSERT |
| `pending`       | > old               | UPDATE in place — decision changed but nothing has gone out yet |
| `pending`       | ≤ old               | no-op (already evaluated this version) |
| `suppressed`    | > old               | UPDATE in place — late summary changed our mind |
| `suppressed`    | ≤ old               | no-op |
| `failed`        | > old               | UPDATE in place — retry on better payload |
| `failed`        | ≤ old               | no-op |
| `sent`          | any                 | no-op — can't unsend, freeze the record |

This is what makes the late-summary regression test (validation
step 12) pass: the first decision writes `suppressed/mismatch`
with `decided_payload_version=1`; when the summary arrives and
`payload_version` becomes 2, the decider re-judges, gets a `send`
verdict, and the upsert rewrites the row to `pending` with
`decided_payload_version=2`.

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
    -- Which payload_version of the underlying event this decision was
    -- made on. Lets us replace stale decisions when the event payload
    -- gets a meaningful update (e.g. agent_summary arrives late).
    decided_payload_version int not null default 1,
    sent_at         timestamptz,
    error           text,
    -- Idempotency guard: at most one notification row per
    -- (event, subscription); re-decisions overwrite in place when
    -- allowed by the rewrite rules below.
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
    -- Token usage so we can size the budget honestly (§7).
    -- Some endpoints don't return usage; columns are nullable.
    input_tokens    int,
    output_tokens   int,
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
declare
    payload_significantly_changed boolean;
begin
    -- Compute "did decider-relevant fields actually change?" Branch
    -- explicitly on TG_OP so we never reference OLD on INSERT.
    if tg_op = 'INSERT' then
        payload_significantly_changed := true;
    else
        payload_significantly_changed :=
            (coalesce(old.agent_summary, '')
                is distinct from coalesce(new.agent_summary, ''))
            or (coalesce(old.agent_response_full, '')
                is distinct from coalesce(new.agent_response_full, ''));
    end if;

    insert into events (source, source_id, user_id, project_root,
                        occurred_at, payload, payload_version)
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
        ),
        1
    )
    on conflict (source, source_id) do update
        set payload = excluded.payload,
            payload_version = case
                when payload_significantly_changed
                    then events.payload_version + 1
                else events.payload_version
            end,
            ingested_at = case
                when payload_significantly_changed then now()
                else events.ingested_at
            end;
    return new;
end $$ language plpgsql;

create trigger turns_to_events
    after insert or update on turns
    for each row execute function on_turn_to_event();
```

We trigger on UPDATE too because `agent_summary` is filled in
asynchronously by the summarise edge function. The
`payload_significantly_changed` guard ensures the decider only
re-considers an event when the new content is materially different
— writing the same summary twice does not cause two notifications.

The trigger is **idempotent in the trivial sense** (same input →
same row), and the version field plus the
`notifications(event_id, subscription_id)` unique constraint give
the decider safe re-processing semantics.

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
        # "unprocessed" = never decided OR decided on a stale payload_version
        events = fetch_events_needing_decision(limit=100)
        if not events:
            continue

        # Pull ALL enabled subscriptions once per loop iteration —
        # not per event, not by event scope. An event about albert's
        # vibelive turn must reach every user / chat with a relevant
        # subscription, regardless of where the event originated.
        all_subs = fetch_all_enabled_subscriptions()
        # Group by (scope_kind, scope_id) so we can give the judge
        # the full sibling rule set per owner.
        subs_by_scope = group_by_scope(all_subs)

        for ev in events:
            decided_version = ev.payload_version
            for scope_key, scope_subs in subs_by_scope.items():
                # Each sub in this group is a candidate; the rest of
                # the group is sibling context (exclusions, quiet
                # hours, etc).
                for candidate in scope_subs:
                    siblings = [s for s in scope_subs if s.id != candidate.id]
                    existing = get_notification(ev.id, candidate.id)
                    if existing and existing.status == 'sent':
                        # Already delivered — can't unsend
                        continue
                    if existing and existing.decided_payload_version >= decided_version:
                        # Already evaluated this exact payload version
                        # for this (event, candidate) pair
                        continue
                    decision = await judge(ev, candidate, siblings,
                                           context_for(scope_key))
                    write_decision_log(ev, candidate, decision,
                                       model, latency, tokens)
                    upsert_notification_row(ev, candidate, decision,
                                            decided_version)
            mark_event_processed(ev.id, decided_version)
```

`mark_event_processed(event_id, version)` does:
```sql
update events
   set processed_at = now(), processed_version = $version
 where id = $event_id;
```

So a later UPDATE to that turn row that bumps `payload_version`
will pull the event back into `fetch_events_needing_decision`, and
the per-(event, candidate) `decided_payload_version` guard ensures
each candidate is re-judged exactly once per real payload change.

`context_for(sub)` is the bundle the judge needs. Critically, the
judge sees **all of the owner's preferences**, not just the
candidate subscription, because exclusions and quiet-hours are
written as separate `subscriptions` rows but must be able to
suppress matches from a *different* row. Concretely:

- **Candidate subscription**: the row currently being decided on
  (positive description, e.g. "vibelive 进展告诉我"). The judge
  considers this the potential match source.
- **All sibling subscriptions for the same scope**: every other
  enabled row owned by the same `(scope_kind, scope_id)`, ordered
  newest-first. These contain exclusions ("项目 C 不要"), quiet-hours
  ("今晚别打扰我"), and other modifiers that must veto the candidate
  if applicable.
- **Recent notifications for this scope** (last 30min). Each
  row carries:
    - `decided_at` (so the 5-min dedup rule has actual timestamps)
    - `event_id` (so the judge can ignore prior decisions about
      *the same* event when re-judging on a new payload version —
      otherwise a `suppressed/mismatch` row from version 1 would
      block version 2's send)
    - `status` (`sent` / `suppressed` / `pending` / `failed`)
    - `subject_summary` (one line of the rendered or candidate text)
    - `project_root`
    - `suppressed_by` (when applicable)
  The judge MUST ignore rows where `event_id == current_event.id`
  when applying duplicate-window logic, and MUST NOT count
  `suppressed/mismatch` rows as occupying the dedup slot at all
  (they didn't actually disturb the user). This is enforced by the
  prompt; see §4.1.
- **Daily count** for the owner (notifications with status='sent'
  since local-midnight in the owner's timezone).
- **Owner wall clock** in their timezone.
- **is_subject_the_owner**: whether `event.user_id` matches the
  subscription scope. Default is to send (per user decision #2);
  individual subscription descriptions can flip this if they
  explicitly say so.

The judge's verdict is therefore a function of (event, candidate
sub, all sibling subs, recent notifs, time, subject-relation), and
"项目 C 不要" or "今晚别打扰" written as a sibling row will reliably
veto a candidate match.

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
- Tools available: read-only subset — `list_users`, `lookup_user`,
  `get_recent_turns`, `get_project_overview`, `get_activity_stats`,
  `today_iso`, plus a new **`resolve_subject_mention(user_id)`**
  tool that returns the linked Feishu open_id (and display name) so
  the renderer can emit a real `<at user_id="ou_xxx"></at>` for
  group mentions. Image generation, write tools (calendar / bitable
  / doc), external link readers, and `resolve_people` (which is
  ambiguity-aware and prompts for follow-ups) are all explicitly
  disallowed during rendering — the renderer must produce a final
  string in one shot with no side effects.

  As a fallback when `resolve_subject_mention` returns nothing
  (subject hasn't bound their Feishu account), the renderer uses
  `@<handle>` plain text so the message is still readable.
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
你是 pmo_agent 的通知决策器。给你一条新事件 + 订阅人的全部偏好 +
最近通知历史。判断要不要给候选订阅发通知。

## 候选订阅（Candidate）

  id: {uuid}
  scope: {user | chat}
  description: "{用户原话}"

## 订阅人的其他生效偏好（Sibling rules）

按时间倒序，**任何一条都可能 veto 候选订阅**——比如某条说"项目 C
不要"、"凌晨别打扰"、"我自己干的事不用提醒"——只要它和事件相关，
就压过候选订阅。

  - id={uuid}, "{原话}"
  - id={uuid}, "{原话}"
  ...

## 订阅人状态

  owner_local_time: {wall clock in their timezone, e.g. "2026-05-04 23:42 Asia/Shanghai"}
  owner_today_sent_count: {今天已**实际发出**（status='sent'）的通知数}
  owner_recent_notifications: [
    { decided_at: "2026-05-04T23:30:00+08:00",
      event_id: 12345,
      status: "sent" | "suppressed" | "pending" | "failed",
      subject_summary: "albert 在 vibelive 调播放器 buffer",
      project_root: "/Users/.../vibelive",
      suppressed_by: null | "duplicate_in_window" | "quiet_hours" | ... },
    ...
  ]   # last 30 minutes; 用 decided_at 判去重时间窗

## 事件

  source: {turn}
  occurred_at: {ISO}
  subject_user: {handle, 或 "未绑定"}
  project_root: {...}
  is_subject_the_owner: {true | false}
  payload:
    user_message: "{用户输入的 prompt}"
    agent_summary: "{一句话总结，可能为空 — 此时只能凭 user_message
                    判断主题}"

## 决策原则

1. **排除/静音类 sibling 优先**：先扫一遍 sibling rules，看有没有任何
   一条会因当前事件或当前时间触发否定（"项目 X 不要"、"凌晨别打扰"、
   "周末不发"等）。命中就 send=false，suppressed_by 取最贴切的那个
   分类（"explicit_exclude" 或 "quiet_hours"）。
2. **去重**：扫 owner_recent_notifications，看 decided_at 在 5 分钟
   内且 subject 同主题的条目。注意两条排除项：
   - **跳过 event_id == 当前事件的所有旧记录**——这是同一个事件被
     payload_version 更新后重判，不该自己挡自己。
   - **跳过 status == 'suppressed' 且 suppressed_by == 'mismatch'
     的记录**——那次没真正打扰用户，不占去重坑。
   余下条目里若有 5 分钟内同主题的 → send=false,
   suppressed_by="duplicate_in_window"，reason 必须引用被命中的那条
   通知的 decided_at。
3. **每日上限**：owner_today_sent_count >= 20 → send=false,
   suppressed_by="daily_cap"。除非 sibling rules 里写了"重要事件
   break through"。
4. **是否匹配候选订阅**：到此都没否决，看候选 description 是否覆盖
   当前事件。命中 → send=true，写 matched_aspect 和 preview_hint。
5. **agent_summary 缺失**：如果 payload.agent_summary 为空且
   user_message 不足以判断主题 → send=false, suppressed_by="mismatch"，
   reason 注明 "summary not available yet"。等下一次 payload_version
   bump 重审。
6. 拿不准 → send=false。

## 输出 JSON

{
  "send": bool,
  "matched_aspect": "候选 description 里哪一块匹配的（一句话），未发出可空",
  "preview_hint": "若 send=true，1 句话告诉渲染阶段重点写什么",
  "suppressed_by": "duplicate_in_window | quiet_hours | daily_cap |
                    explicit_exclude | mismatch | null",
  "reason": "一句话说明判断依据，必须能让用户日后追问'为什么没通知'时复盘"
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
- 群通知里提到事件主体时调用 resolve_subject_mention(user_id) 拿
  open_id，然后用 `<at user_id="ou_xxx"></at>` 飞书 mention 语法。
  resolve_subject_mention 返回空说明那个人还没绑定飞书 → 直接写
  `@<handle>` 文字版本。**不要假设格式 — 一定要先调工具**。
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

### 5.0 Prerequisite: extend `RequestContext`

Today `RequestContext` carries `(message_id, chat_id,
sender_open_id, conversation_key)`. The subscription tools need
two more fields:

- `chat_type: str` — `'p2p' | 'group'`. Determines whether
  `add_subscription` writes a user-scoped or chat-scoped row.
- `asker_user_id: str | None` — the resolved profile UUID of the
  asker (None if Feishu account isn't bound). Used as
  `subscriptions.scope_id` for user scope, and as `created_by`
  always.
- `asker_handle: str | None` — convenience for tool error
  messages.

These are populated in `app.py::_handle_message` from
`feishu_events.ParsedMessageEvent.chat_type` (already parsed) and
`db_queries.lookup_by_feishu_open_id(sender_open_id)` (already
called for the `[asker]` framing). Then passed into the runner's
context the same way `message_id` / `chat_id` are today.

This is implemented as **§2.5 of the build plan, before the four
tools land**, because every tool depends on it.

### 5.1 The four tools

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
