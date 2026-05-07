# Proactive PMO Agent 2.0c — Chat Memory, People Memory, Observer

- **Status**: Draft for implementation
- **Date**: 2026-05-07
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Strategy**: [2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md)
- **Plan**: [2.0c Plan](2026-05-07-proactive-agent-2.0c-plan.md)
- **Predecessors**: 1.0c + 2.0a + 2.0b. 2.0c assumes the bot can
  already read external repo state and route notifications to DMs or
  chats.

This is the **source of truth** for 2.0c's product boundary, data
model, retrieval tools, people-memory contract, and observer
architecture. Implementation choices that diverge update this file.

---

## 1. Why 2.0c

The current PMO agent only sees a Feishu group when somebody explicitly
mentions it. In `bot/app.py`, group messages that do not @ the bot are
ignored. That makes the bot a query tool, not an in-room PMO.

A real PMO silently observes the room, remembers decisions, learns who
understands which area, and later answers or nudges with that context:

> "基于我们聊的信息，看看有哪些达成一致的 todo"
>
> "这个问题该找谁确认？"
>
> "刚才 vibelive 播放器方案是怎么定的？"
>
> "这个技术方案是不是需要 @ hellobit 看一下？"

2.0c adds that missing layer in two phases:

1. **Passive memory infrastructure**: opt-in group message capture,
   searchable chat history, and PMO-maintained people notes. The bot
   still only responds when asked.
2. **Active observer**: a later loop that uses chat memory + people
   memory + repo/turn events to decide when a PMO should proactively
   speak.

Phase 1 is infrastructure. Phase 2 is judgment. Do not ship Phase 2
until Phase 1 has enough real usage to tune trust and noise.

---

## 2. Scope

### In scope for Phase 1

- Explicit opt-in / opt-out for recording a Feishu group
- Store supported message facts in opted-in groups, including messages
  that do not @ the bot:
  - `text`
  - `post` / rich-text as extracted redacted text
  - shared-link / shared-chat / file metadata such as title, URL, and
    file name, but not binary content
- Redact obvious secrets before persistence
- Keep redacted message text and safe metadata as the fact layer for
  later retrieval
- Search tools for the current chat:
  - recent messages
  - keyword / semantic-ish search
  - message windows around a matched message
- People memory with objective identity fields plus one natural-language
  `pmo_notes` field maintained by the PMO agent
- Chat tools for questions like:
  - "刚才达成了哪些 todo"
  - "今天这个群有哪些决策"
  - "谁比较适合看这个问题"
- Web or chat-visible status showing whether a chat is being recorded

### In scope for Phase 2

- A low-frequency observer loop over opted-in chats
- Observer-generated candidates that go through investigation and
  renderer before delivery
- People-memory assisted @ recommendations
- Per-chat and per-user trust budgets
- Feedback commands such as "这类提醒没用" and "别再主动提醒这个"

### Out of scope

- Recording groups without explicit opt-in
- Recording DMs by default
- Using private DM content to influence group-visible people notes
- File/image/audio binary content and reactions in the first cut
- Behavioral / performance judgment such as "X is unreliable" or "Y is
  slacking"
- Auto-assigning tasks or modifying external systems
- Cross-chat retrieval unless a later permission design explicitly
  allows it

---

## 3. Product invariants

1. **Chat-level opt-in by any member**. A group must explicitly enable
   memory before non-@ messages are persisted. This is a practical
   chat-level consent model, not proof that every individual member
   consented.
2. **Silent means silent**. Non-@ messages in an enabled chat are stored
   and indexed only. They do not wake the conversational agent.
3. **Redacted chat messages are the fact layer**. Summaries and people
   notes are hints. Answers and proactive statements should be able to
   retrieve supporting redacted messages and safe metadata.
4. **People memory is a PMO note, not a scorecard**. It is a natural
   language work-context note, not a structured ranking of humans.
5. **Current chat by default**. A user asking in a chat can retrieve
   that chat's memory. Cross-chat search is out of scope for Phase 1.
6. **Private stays private**. DM content must not be used to update a
   group-visible person note unless the user explicitly asks to save
   that fact.
7. **Observer does not bypass investigation**. Phase 2 observer creates
   candidates; investigator validates and grounds them before anything
   is sent.
8. **Renderer does not decide**. Renderer formats investigator output.
9. **Every active feature has a kill switch**. "停止记录这个群" and "不要主动提醒"
   must be available from chat.
10. **Any chat member can stop memory**. The same weak-consent model that
    lets one member enable memory must let any member disable it.
11. **People notes are not directly disclosed**. Conversational tools
    expose topic-scoped people signals, not raw `pmo_notes`.
12. **Bot-mention confidence gates storage**. If the bot cannot resolve
    its own Feishu `open_id` for a group message, it must not guess
    whether the message @s the bot. Return a short "not ready" ack and
    do not store or answer that message.

---

## 4. Phase 1A/1B — Chat Memory

### 4.1 Opt-in UX

The bot only starts storing non-@ group messages after an explicit
command in that group, for example:

> @包工头 开始记录这个群

The bot replies:

> 已开启这个群的 PMO 记忆。我会记录之后的文字消息，用于回答这个群里的
> 上下文问题；不会因为普通消息主动回复。可以随时说「停止记录这个群」。

This reply is a public notice in the chat, not a silent state change. It
must make clear that any group member can stop recording. If a member
objects, the bot accepts:

> @包工头 停止记录这个群

from any member in the chat.

Disable:

> @包工头 停止记录这个群

The bot replies:

> 已停止记录这个群。之后的普通消息不会进入 PMO 记忆。历史记录保留到
> 当前保留期结束；如需清理历史记录，请说「清理这个群的 PMO 记忆」。

Status:

> @包工头 这个群有没有开启记忆？

The bot replies with enabled/disabled, who enabled it, when, retention
period, and whether active observer is enabled.

Phase 1 does **not** enable active observer. The status can show:

> 主动观察：未开启

Open consent gap: Phase 1 does not implement per-member opt-out inside
an opted-in chat. If the product needs stronger consent, add a
`chat_memory_member_opt_outs` table before broad rollout. Until then,
the operational rule is simple: any member can disable memory for the
whole chat.

### 4.2 Feishu webhook handling

Current behavior:

```python
if parsed.chat_type == "group" and not parsed.is_at_bot:
    return PlainTextResponse("group not addressed")
```

2.0c changes this to an ack-first flow:

1. Parse the Feishu message event into a neutral message envelope. Phase
   1 supports `text`, `post`, shared-link/shared-chat metadata, and file
   metadata. Unsupported message types can be ignored, but the decision
   must be explicit in parser tests.
2. Run existing webhook-level duplicate suppression
   (`event_id_of` / `already_seen`) before scheduling storage.
3. If this is a group message and the bot identity cache is not ready,
   return `"not ready"`. Do not store the message and do not call the
   conversational agent; a wrong `is_at_bot` value is worse than missing
   one memory row.
4. If this is a group message, schedule best-effort storage in a
   background task. Storage must use a short TTL cache for
   `chat_memory_settings.enabled` and must not block the webhook ack.
5. If it does not @ the bot, return `"stored"` if the cache says memory
   is enabled, otherwise `"group not addressed"`. Do not wait for the DB
   insert.
6. If it @s the bot, continue into `_handle_message` as today.

Group messages in disabled chats still return `"group not addressed"`
unless they @ the bot.

Storage is best-effort. If Supabase is temporarily unavailable, the bot
may miss a chat-memory row rather than causing Feishu webhook retries.
This is acceptable for memory; it is not acceptable for the existing
interactive reply path.

Bot-authored messages are not required in Phase 1. If Feishu delivers
the bot's own messages back to the app, the ingester should ignore them
or mark `sender_is_bot=true` and exclude them from default search.

Interactive behavior remains text-first: `_handle_message` only needs to
answer for `text` / text-equivalent `post` messages in Phase 1. Shared
files and links are persisted for later retrieval but do not have to
wake the conversational agent unless Feishu presents them as text with a
bot mention.

Feishu recall/edit events are part of Phase 1 privacy semantics:

- recall event: set `chat_messages.deleted_at=now()` for the message id;
  retrieval tools exclude deleted rows by default
- edit event: update `text_redacted`, `redacted_payload`, and
  `edited_at`; retrieval tools show the latest redacted text

### 4.3 Data model: `chat_memory_settings`

```sql
create table if not exists public.chat_memory_settings (
    chat_id              text primary key,
    enabled              boolean not null default false,
    enabled_at           timestamptz,
    enabled_by_user_id   uuid references public.profiles(id) on delete set null,
    enabled_by_open_id   text,
    disabled_at          timestamptz,
    disabled_by_user_id  uuid references public.profiles(id) on delete set null,
    disabled_by_open_id  text,
    retention_days       int not null default 90 check (retention_days between 1 and 730),
    observer_enabled     boolean not null default false,
    people_loop_cursor   timestamptz,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create table if not exists public.chat_memory_settings_history (
    id                  bigserial primary key,
    chat_id             text not null
                        references public.chat_memory_settings(chat_id) on delete cascade,
    action              text not null check (
                          action in ('enable', 'disable', 'retention_change',
                                     'observer_enable', 'observer_disable',
                                     'clear_history')
                        ),
    actor_user_id       uuid references public.profiles(id) on delete set null,
    actor_open_id       text,
    old_value           jsonb,
    new_value           jsonb,
    created_at          timestamptz not null default now()
);
```

RLS: service-role only in Phase 1. User-facing reads/writes happen
through bot tools or authenticated web server actions after checking
chat membership.

`chat_memory_settings` stores current state only. Every enable, disable,
retention change, observer toggle, and clear-history action appends a
row to `chat_memory_settings_history`; current-state timestamps may be
overwritten on repeated toggles.

### 4.4 Data model: `chat_messages`

```sql
create table if not exists public.chat_messages (
    id                  bigserial primary key,
    feishu_message_id   text not null unique,
    chat_id             text not null
                        references public.chat_memory_settings(chat_id) on delete cascade,
    chat_type           text not null check (chat_type in ('group', 'p2p')),
    sender_open_id      text not null,
    sender_user_id      uuid references public.profiles(id) on delete set null,
    sender_display_name text,
    message_type        text not null default 'text' check (
                          message_type in ('text', 'post', 'share_chat',
                                           'file', 'link', 'unknown')
                        ),
    text_redacted       text not null default '',
    is_at_bot           boolean not null default false,
    sender_is_bot       boolean not null default false,
    parent_message_id   text,
    root_message_id     text,
    mentions            jsonb not null default '[]'::jsonb,
    redacted_payload    jsonb not null default '{}'::jsonb,
    content_metadata    jsonb not null default '{}'::jsonb,
    occurred_at         timestamptz not null,
    ingested_at         timestamptz not null default now(),
    edited_at           timestamptz,
    deleted_at          timestamptz
);

create index chat_messages_chat_time_idx
    on public.chat_messages (chat_id, occurred_at desc);

create index chat_messages_sender_time_idx
    on public.chat_messages (sender_open_id, occurred_at desc);

create index chat_messages_parent_idx
    on public.chat_messages (parent_message_id)
    where parent_message_id is not null;

create index chat_messages_text_fts_idx
    on public.chat_messages
    using gin (to_tsvector('simple', text_redacted));

create index chat_messages_deleted_idx
    on public.chat_messages (chat_id, deleted_at)
    where deleted_at is not null;
```

`redacted_payload` is not the raw Feishu body. It is the parsed,
redacted payload needed for debugging and future feature extraction.
`content_metadata` stores safe structured facts such as shared-link
title/URL, shared-chat title, or file name. Raw encrypted event bodies
and file/image/audio bytes are not stored.

Messages are idempotent by `feishu_message_id`. Re-delivery must not
create duplicates. `event_id_of` / `already_seen` remains the first
webhook-level duplicate gate; `feishu_message_id` uniqueness is the DB
idempotency backstop for retries that get past the process-local event
cache.

Retrieval tools exclude rows with `deleted_at is not null` and
`sender_is_bot=true` by default.

`text_redacted` should never be an empty string for a stored row. If
all visible text is removed by redaction, store `[REDACTED]` so search
and audit surfaces can distinguish "redacted content existed" from
"there was no textual content."

`to_tsvector('simple', ...)` is only a baseline index. It does not
segment Chinese well, so Phase 1 relies on substring fallback for Chinese
recall. If any enabled chat grows beyond roughly 50k stored messages or
search latency/recall becomes poor, upgrade the search backend to
PGroonga or embeddings before broad rollout.

### 4.5 Redaction

Reuse and extend the external-event redaction module. Redaction applies
before writing `text_redacted` and `redacted_payload`.

Minimum patterns:

- access tokens / API keys
- Bearer tokens
- JWTs
- postgres/mysql/redis connection strings
- Feishu webhook URLs
- GitHub/Gitea tokens
- Stripe live keys
- email addresses
- Chinese mainland mobile phone numbers and E.164 phone numbers
- Chinese resident ID numbers
- bank-card-like digit runs
- IPv4 host + optional port when it appears near password / ssh /
  database context

Redaction is best-effort. The product copy must not claim "we can never
store secrets"; it should say "the bot redacts common secret patterns
before storing messages."

### 4.6 Retrieval tools

Tools are scoped to the current chat by default. The LLM must not pass
an arbitrary chat_id unless the request context proves the user is in
that chat.

Conversational tool schemas must not include `chat_id`. If a model sends
an unexpected `chat_id` key anyway, the tool layer rejects the call
instead of silently ignoring it. This makes cross-chat prompt injection
observable in tests.

#### `get_recent_chat_messages`

Input:

```json
{
  "since": "2026-05-07T00:00:00+08:00",
  "until": null,
  "limit": 80,
  "sender": null
}
```

Output:

```json
{
  "messages": [
    {
      "message_id": "om_xxx",
      "sent_at": "2026-05-07T14:39:00+08:00",
      "sender": "bcc",
      "text": "vibelive 的 dev 分支...",
      "is_at_bot": false
    }
  ]
}
```

#### `search_chat_messages`

Input:

```json
{
  "query": "vibelive 播放器 todo",
  "since": "2026-05-01T00:00:00+08:00",
  "until": null,
  "limit": 30
}
```

Search should combine:

- FTS on `text_redacted`
- simple substring fallback for Chinese text
- recency ordering

Phase 1 does not require embeddings. If recall is poor, embeddings can
be added later without changing the tool contract.

#### `search_chat_messages_with_context`

Preferred input for natural questions like "刚才那个方案怎么定的":

```json
{
  "query": "vibelive 播放器方案",
  "since": "2026-05-01T00:00:00+08:00",
  "until": null,
  "limit": 8,
  "before": 8,
  "after": 8
}
```

Output returns each hit plus a same-chat message window around it. This
is the default tool for "刚才 / 前面 / 那个" style questions because the
LLM does not know Feishu `message_id` values upfront.

#### `get_chat_window`

Input:

```json
{
  "anchor_message_id": "om_xxx",
  "before": 12,
  "after": 12
}
```

Returns messages around the anchor in the same chat. This is the
primary anti-hallucination tool: search finds candidates; window gives
the conversation needed to answer.

Typical chain:

1. For a natural-language memory question, call
   `search_chat_messages_with_context`.
2. If the user points at a specific returned hit, call
   `get_chat_window(anchor_message_id=...)` to expand context.
3. Answer only after reading the window, not from a single search hit.

### 4.7 Answering contract

When answering from chat memory, the bot should:

- Prefer retrieving a window around relevant messages rather than
  answering from a single hit
- Say "我没看到明确结论" when the chat does not contain consensus
- Separate "已达成一致" from "有人提出但未确认"
- Avoid inventing owners/deadlines unless the chat says them
- Use message evidence internally, but do not spam message ids in the
  final answer unless the user asks for traceability

---

## 5. Phase 1C — People Memory

### 5.1 Shape

People memory is intentionally simple. Objective identity is
structured; PMO understanding is natural language.

```sql
create table if not exists public.people_memory (
    person_key        text primary key,
    profile_id        uuid references public.profiles(id) on delete set null,
    feishu_open_id    text,
    display_name      text,
    handle            text,
    pmo_notes         text not null default '',
    notes_updated_at  timestamptz,
    last_observed_at  timestamptz,
    metadata          jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint people_memory_identity_check check (
        person_key like 'profile:%' or person_key like 'feishu:%'
    )
);

create unique index people_memory_profile_idx
    on public.people_memory (profile_id)
    where profile_id is not null;

create unique index people_memory_feishu_idx
    on public.people_memory (feishu_open_id)
    where feishu_open_id is not null;

create index people_memory_last_observed_idx
    on public.people_memory (last_observed_at desc);

create table if not exists public.people_memory_updates (
    id             bigserial primary key,
    person_key     text not null references public.people_memory(person_key) on delete cascade,
    update_source  text not null check (
                     update_source in ('background_loop', 'identity_merge', 'manual_repair')
                   ),
    model          text,
    input_tokens   int,
    output_tokens  int,
    old_note_hash  text,
    new_note_hash  text,
    created_at     timestamptz not null default now()
);

create index people_memory_updates_source_time_idx
    on public.people_memory_updates (update_source, created_at desc);

create index people_memory_updates_person_time_idx
    on public.people_memory_updates (person_key, created_at desc);
```

`person_key`:

- `profile:{uuid}` for a bound PMO user
- `feishu:{open_id}` for a Feishu user the PMO agent has seen but
  cannot map to a PMO profile yet

If a `feishu:{open_id}` person later binds a PMO account, merge into
`profile:{uuid}` and preserve / rewrite `pmo_notes`.

People retrieval and suggestions are scoped to people observed in the
current chat by default. Cross-chat people search is a separate
permission surface and is out of scope for Phase 1.

### 5.1.1 Identity merge

Merge is triggered when the system learns a stable PMO profile for a
previously unbound Feishu open_id:

- Feishu OAuth callback creates / updates `feishu_links`
- bot request context lookup resolves `sender_open_id` to a profile
- admin repair links an open_id to a profile

Merge algorithm:

1. Look up `people_memory` rows for `feishu:{open_id}` and
   `profile:{uuid}` under a row lock.
2. If only the Feishu row exists, move it to `profile:{uuid}` and set
   `profile_id`, `feishu_open_id`, `display_name`, and `handle`.
3. If only the profile row exists, fill missing Feishu objective fields.
4. If both rows exist:
   - if one note is empty, keep the non-empty note
   - if both notes are non-empty and materially different, store both in
     `metadata.merge_sources` and schedule a background note rewrite;
     until rewrite completes, concatenate them under a short
     "merged context" heading rather than dropping either
5. Delete the stale `feishu:{open_id}` row after the profile row is
   updated.
6. Backfill recent `chat_messages.sender_user_id` for rows with the
   matched `sender_open_id`. This can be capped/batched; historical
   rows still remain searchable by `sender_open_id`.

The merge path must be idempotent. Running it multiple times for the
same open_id/profile pair should not duplicate note text.

### 5.2 `pmo_notes` contract

`pmo_notes` is a short natural-language note written for the PMO agent,
not for public display by default.

Good note:

> hellobit 最近主要在 vibelive 播放器和 Agora RTC 媒体流验证上推进，
> 技术判断比较强，适合确认底层实现方案、diff 细节和压测标准。群里如果
> 讨论播放器、RTC、媒体流或 stats 校验，可以优先请他看。普通 dev push
> 总结不需要频繁打扰他，除非需要他确认技术方案。

Bad note:

> hellobit 很靠谱，别人不如他。

The updater prompt must forbid:

- personal character judgments
- productivity / performance ranking
- sensitive attributes
- claims not grounded in work context
- using private DM context in group-visible recommendations

The note should include:

- what the person seems to be working on
- what they are likely good to ask about
- what not to bother them with
- recent project / repo / module context
- uncertainty when the evidence is thin

### 5.3 People-memory update modes

Phase 1 supports two update modes:

1. **Opportunistic update request**: when the user asks "谁该看" or
   "这个 TODO 应该给谁", the conversational agent can inspect recent chat
   messages and suggest relevant people. It must not directly rewrite
   people notes in the user-facing conversation.
2. **Background update loop**: every 30-60 minutes, for enabled chats
   with new messages, update notes for active participants. This loop
   never sends messages.

The background loop should use a small budget:

- max 20 active people per run
- max 80 recent messages per chat
- max 200 note rewrites per day globally by default
- max 4k input tokens and 600 output tokens per note rewrite
- max 30k input tokens per loop run
- debounce: do not rewrite the same person's note more than once every
  10 minutes unless a human explicitly requests a correction
- skip people with no meaningful new evidence
- preserve existing notes when there is nothing new

Daily caps and debounce checks are DB-backed via
`people_memory_updates`, not process memory. A deploy/restart must not
reset the day's update budget.

### 5.4 People tools

#### `summarize_people_signal`

Input:

```json
{
  "query": "hellobit",
  "topic": "vibelive 播放器 Agora RTC stats 校验"
}
```

The tool reads matching raw `pmo_notes` server-side and returns a
topic-scoped summary safe for conversational output. It must not return
the raw note text. It defaults to people observed in the current chat.

#### `suggest_people_for_topic`

Input:

```json
{
  "topic": "vibelive 播放器 Agora RTC stats 校验",
  "limit": 5
}
```

Returns candidate people with reasons derived from `pmo_notes` plus
recent chat evidence. It defaults to people observed in the current
chat and should not recommend someone known only from another chat
unless a future permission design allows cross-chat people search.

The tool should be conservative: if notes are thin, return "not enough
signal" rather than guessing.

`suggest_people_for_topic` may read raw notes internally, but its output
must contain only concise, topic-scoped reasons. The conversational
agent never receives the full `pmo_notes` blob.

#### Internal writer: `update_people_memory_note`

This is **not** exposed to the conversational agent. It is an internal
background-loop writer only. A user in a group must not be able to
prompt-inject a note rewrite such as "把 hellobit 写成不靠谱".

The background updater writes a replacement `pmo_notes` value after it
has read enough recent context and passed budget/debounce checks.

It must store an audit row in `people_memory_updates` for:

- new note
- updater model name
- input/output token usage if available
- timestamp

### 5.5 Answering contract

When suggesting a person:

- Prefer "这个问题看起来适合请 X 看" over "@X 你来处理"
- If confidence is low, ask the group who owns it
- Explain the work-context reason briefly
- Do not expose private notes verbatim if they contain internal
  reasoning; summarize only what is relevant
- Respect "do not bother X for this class of updates" when it appears in
  `pmo_notes`

This is enforced structurally: the conversational agent can call
summary/suggestion tools, but cannot call a raw-note read API.

---

## 6. Phase 2 — PMO Observer

Phase 2 merges with the 2.0 strategy's "judgment-driven proactive"
axis. It should start only after Phase 1 has real chat-memory usage.

### 6.1 Observer input

Every N minutes, for each observer-enabled chat:

- recent `chat_messages`
- topic-scoped people signals for active participants
- recent repo events from 2.0a
- recent turn events
- recent notifications and suppressed investigations
- active chat-owned subscriptions from 2.0b

The observer should not read unbounded raw history. It reads a curated
snapshot plus tools for drilling into raw messages.

### 6.2 Observer output

Observer produces zero or more candidates:

```json
{
  "speak": true,
  "candidate_type": "unanswered_question|stale_blocker|decision_summary|handoff|rule_suggestion",
  "target_kind": "chat|mention_in_chat|user_dm",
  "target_id": "oc_xxx",
  "target_user_open_id": null,
  "topic": "vibelive player dev push needs technical summary",
  "topic_key": "vibelive:dev_push:tech_summary",
  "why_now": "The chat has an active request and a new repo push landed.",
  "evidence_message_ids": ["om_xxx"],
  "evidence_event_ids": [123],
  "suggested_people": ["feishu:ou_xxx"],
  "confidence": "low|medium|high",
  "expires_at": "2026-05-07T16:00:00+08:00"
}
```

Observer candidates are not notifications. They enter an investigator
path that reads enough raw context and decides whether to notify.

### 6.3 Candidate storage

Use a separate table rather than forcing `investigation_jobs` to accept
nullable `subscription_id` in the first pass:

```sql
create table if not exists public.observer_candidates (
    id                    bigserial primary key,
    chat_id                text not null
                           references public.chat_memory_settings(chat_id) on delete cascade,
    status                 text not null default 'open' check (
                              status in ('open', 'investigating', 'notified',
                                         'suppressed', 'expired', 'failed')
                            ),
    candidate_type         text not null,
    target_kind            text not null check (
                              target_kind in ('user_dm', 'chat', 'mention_in_chat')
                            ),
    target_id              text not null,
    target_user_open_id    text,
    topic                  text not null,
    topic_key              text not null,
    why_now                text,
    evidence_message_ids   text[] not null default '{}'::text[],
    evidence_event_ids     bigint[] not null default '{}'::bigint[],
    suggested_people       text[] not null default '{}'::text[],
    observer_decision      jsonb not null default '{}'::jsonb,
    investigator_decision  jsonb,
    notification_id        bigint references public.notifications(id) on delete set null,
    opened_at              timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    expires_at             timestamptz,
    closed_at              timestamptz,
    constraint observer_candidates_target_check check (
        (target_kind = 'user_dm'
         and target_id ~ '^[0-9a-f-]{36}$'
         and target_user_open_id is null) or
        (target_kind = 'chat'
         and length(target_id) > 0
         and target_user_open_id is null) or
        (target_kind = 'mention_in_chat'
         and length(target_id) > 0
         and target_user_open_id is not null)
    )
);

create index observer_candidates_status_idx
    on public.observer_candidates (status, opened_at)
    where status in ('open', 'investigating');

create index observer_candidates_chat_open_idx
    on public.observer_candidates (chat_id, opened_at desc)
    where status in ('open', 'investigating', 'notified');

create index observer_candidates_expires_idx
    on public.observer_candidates (expires_at)
    where status in ('open', 'investigating') and expires_at is not null;

create index observer_candidates_target_user_time_idx
    on public.observer_candidates (target_user_open_id, opened_at desc)
    where target_kind in ('user_dm', 'mention_in_chat')
      and target_user_open_id is not null
      and status = 'notified';

create index observer_candidates_chat_topic_time_idx
    on public.observer_candidates (chat_id, topic_key, opened_at desc)
    where status = 'notified';
```

`topic` is human-readable. `topic_key` is the stable dedupe key used for
budgets and feedback, e.g. `vibelive:dev_push:tech_summary`. The
observer prompt should produce a lowercase colon-separated slug with
project/repo, event kind, and intent when those are known. Budget checks
use exact `topic_key` match rather than fuzzy matching free text.

Target semantics mirror 2.0b:

| target_kind | target_id | target_user_open_id |
|-------------|-----------|---------------------|
| `user_dm` | profile uuid | NULL |
| `chat` | Feishu chat_id | NULL |
| `mention_in_chat` | Feishu chat_id | Feishu open_id to mention |

Target validation must stay in sync with 2.0b's
`subscriptions.subs_target_check`. Implementation must factor the
shared rule into a SQL helper such as
`public.is_valid_delivery_target(kind, target_id, target_user_open_id)`
and use that helper for both `subscriptions` and
`observer_candidates`. A migration test should prove 0023's
`subs_target_check` has been replaced or mirrored by the shared helper.
Do not let the two target schemas drift.

Later, if the implementation shows high overlap with
`investigation_jobs`, merge them. First version should keep observer
schema separate so it does not destabilize subscription-driven
notifications.

### 6.3.1 Observer notifications and `subscription_id`

`notifications.subscription_id` is `not null` in the 1.0c/2.0b schema.
Do not make it nullable for observer delivery in 2.0c. Instead, create
synthetic observer subscription rows on demand:

- `scope_kind='chat'`
- `scope_id=<source chat_id>`
- `description='__system_observer__'`
- `created_by=null`
- `target_kind`, `target_id`, and `target_user_open_id` copied from the
  observer candidate's validated target
- metadata / comment marks the row as system-owned

One synthetic row can be reused for the same source chat and delivery
target. Observer-generated notification rows point at that synthetic
subscription and also keep `observer_candidates.notification_id` for
audit. This preserves the existing notification delivery path without
pretending observer candidates are user-authored watch rules.

### 6.4 Trust budgets

Defaults:

- max 1 observer-generated message per chat per day
- max 1 observer-generated DM per user per day
- max 3 observer candidates investigated per chat per day
- no observer @ mention if confidence is low
- no observer delivery if the same `topic_key` was notified in the last
  24h

Budgets are DB-backed. A deploy/restart must not reset observer caps.
The implementation should count `observer_candidates` with
`status='notified'` over the relevant time window, not keep an
in-memory counter.

User feedback:

- "这类提醒没用" reduces future confidence for that candidate_type in
  that chat
- "不要主动提醒这个" disables matching observer topic for the chat
- "停止主动观察这个群" sets `observer_enabled=false`

Feedback storage:

```sql
create table if not exists public.observer_feedback (
    id                    bigserial primary key,
    chat_id                text not null
                           references public.chat_memory_settings(chat_id) on delete cascade,
    observer_candidate_id  bigint references public.observer_candidates(id) on delete set null,
    user_id                uuid references public.profiles(id) on delete set null,
    user_open_id           text,
    feedback_kind          text not null check (
                              feedback_kind in (
                                'not_useful',
                                'suppress_candidate_type',
                                'suppress_topic',
                                'disable_observer'
                              )
                            ),
    candidate_type         text,
    topic_key              text,
    note                   text,
    created_at             timestamptz not null default now()
);

create index observer_feedback_chat_kind_idx
    on public.observer_feedback (chat_id, feedback_kind, created_at desc);

create index observer_feedback_topic_idx
    on public.observer_feedback (chat_id, topic_key, created_at desc)
    where topic_key is not null;
```

Budget checks must read this table before delivering an observer
candidate. `disable_observer` should also update
`chat_memory_settings.observer_enabled=false`; the feedback row keeps
the audit trail.

RLS: service-role only in the first version. User feedback is written
through bot tools after the sender is resolved from the Feishu request
context.

### 6.5 Observer is not a rule engine

Do not add hard-coded rules like:

- if text contains "bug" then notify
- if "push" then summarize
- if user says "help" then @ owner

The observer LLM decides whether a PMO should intervene. Deterministic
code may enforce safety and budgets, but not product judgment.

---

## 7. Permissions and privacy

### 7.1 Chat membership

Phase 1 retrieval is only allowed inside the current chat. The request
context already includes `chat_id`; conversational tools must reject
caller-supplied `chat_id` keys. A later web/admin path may support
server-verified cross-chat access, but Phase 1 chat tools do not.

Web UI may show chat-memory status only to signed-in users who are
known members of that chat. If membership cannot be verified, hide
message search from web and keep it chat-only.

### 7.2 Bound vs unbound users

If the sender has a bound PMO profile, `chat_messages.sender_user_id`
is set. Otherwise store `sender_open_id` and display name only.

People memory can exist for unbound Feishu users through
`person_key='feishu:{open_id}'`.

### 7.3 Visibility of `pmo_notes`

First version:

- Background/internal code can read raw notes for updating and routing
- Web UI does not expose notes by default
- A future "what do you know about me?" flow should allow a user to
  inspect and correct their own note

Conversational agents cannot read raw notes. They receive only
topic-scoped output from `summarize_people_signal` or
`suggest_people_for_topic`.

### 7.4 Retention

Default retention:

- `chat_messages`: 90 days
- `people_memory.pmo_notes`: no automatic deletion, but notes decay by
  being rewritten from recent evidence
- `observer_candidates`: 90 days

Cleanup jobs must physically delete expired chat messages. Summaries
must not claim support from deleted messages.

The cleanup job is part of Phase 1, not a later operations chore. If a
chat's `retention_days` changes, the next cleanup run should apply the
new value.

---

## 8. LLM prompt contracts

### 8.1 Conversational agent

Add to system prompt:

- The current chat may have PMO memory.
- For questions about "刚才 / 今天 / 我们聊的 / 达成一致 / todo / 谁适合",
  use chat-memory tools before answering.
- For "刚才 / 前面 / 那个" questions, prefer
  `search_chat_messages_with_context` first. Use `get_chat_window` only
  after a search result gives you an anchor `message_id`.
- For group memory, answer only from the current chat unless explicitly
  provided broader context.
- If evidence is unclear, say so.
- Do not create subscriptions when the user is asking about past chat
  context.
- Do not ask tools to search another chat by id. Chat-memory tools are
  current-chat scoped.
- Do not ask for raw people notes. Use topic-scoped people-signal tools.

### 8.2 People note updater

System:

```text
You maintain a concise PMO work-context note about one teammate.
Rewrite the note from the old note plus recent observable work context.
Do not judge personality, reliability, productivity, health, or private
attributes. Focus on what this person has recently worked on, what they
seem useful to ask about, and when not to interrupt them. If evidence is
thin, say that. Output only the note text.
```

Output: plain text, 100-300 words preferred.

The wrapper, not the LLM output, records model name and token usage from
the API response. The prompt remains "output only note text" so the note
field stays natural language.

### 8.3 Observer

System:

```text
You are a quiet PMO observer for an opted-in Feishu chat. Decide whether
a real PMO should proactively say anything now. Be very conservative.
Most runs should produce no candidates. If you propose a candidate, it
must be grounded in recent chat/repo/turn evidence and safe to say in
the target room. Output JSON only.
```

Observer must not produce final message text. It produces candidate
briefs.

---

## 9. Validation scenarios

### Scenario 1 — opt-in storage

1. In a group, send `@包工头 开始记录这个群`.
2. Send three ordinary messages without @.
3. Verify `chat_messages` has three rows for that chat.
4. Verify the bot did not reply to ordinary messages.
5. Verify the bot's public opt-in notice is not returned by default
   retrieval.

### Scenario 2 — passive TODO extraction

1. In an enabled chat, send:
   - "vibelive dev push 后要总结 diff"
   - "hellobit 的普通 push 不用打扰"
   - "今天先让 bcc 确认通知文案"
2. Ask `@包工头 基于我们聊的信息，有哪些达成一致的 TODO?`
3. Expected: bot retrieves chat messages and distinguishes decisions
   from suggestions.

### Scenario 3 — people suggestion

1. In enabled chat, discuss Agora RTC / vibelive player with hellobit.
2. Run people-memory update.
3. Ask `@包工头 这个播放器 stats 校验问题该找谁?`
4. Expected: bot suggests hellobit with a work-context reason and does
   not hard-assign him.

### Scenario 4 — disabled chat privacy

1. In a group that has not enabled memory, send ordinary messages.
2. Verify no `chat_messages` rows are stored.
3. Ask the bot about those ordinary messages.
4. Expected: bot says it has no recorded memory for this chat.

### Scenario 5 — observer gated off in Phase 1

1. Enable chat memory.
2. Send a message that looks like a blocker.
3. Wait longer than the Phase 1 background loop.
4. Expected: no proactive bot message, because observer is disabled.

### Scenario 6 — Phase 2 observer budget

1. Enable observer for a chat.
2. Create three candidate-worthy situations in one day.
3. Expected: at most one observer-generated message is delivered to
   that chat by default.

### Scenario 7 — recall privacy

1. In an enabled chat, send a message containing project context.
2. Trigger a Feishu recall event for that message.
3. Ask the bot about the recalled topic.
4. Expected: retrieval excludes the recalled row and the bot does not
   quote the recalled text.

### Scenario 8 — identity merge

1. Let an unbound Feishu user send relevant chat messages.
2. Verify a `feishu:{open_id}` people-memory row can be created.
3. Bind that Feishu user to a PMO profile.
4. Expected: people memory merges into `profile:{uuid}` and recent
   `chat_messages.sender_user_id` backfills without duplicating notes.

---

## 10. Open decisions

1. **Retention default**: 90 days is proposed. If the team wants longer
   memory, increase after privacy review.
2. **Web UI for chat memory**: Phase 1 can be chat-only. A web search UI
   is useful but requires stronger membership verification.
3. **Embeddings**: not required for first cut. Add if FTS + substring is
   not enough.
4. **People-note visibility**: first cut keeps it internal. A later user
   correction flow is strongly recommended before using notes for
   stronger proactive @ behavior.
5. **Observer launch**: should be behind a separate per-chat flag even
   after Phase 1 ships.
6. **Per-member opt-out**: Phase 1 supports "any member can disable the
   whole chat." A more granular per-member opt-out is deferred and
   should be revisited before large-org rollout.
7. **Web self-inspection**: Phase 1.5 should add "what do you know about
   me?" and people-note correction UX. Phase 1 intentionally ships
   chat-only retrieval first.
8. **Multi-IM identity model**: `feishu:{open_id}` is the only unbound
   person prefix in 2.0c. If Slack or another IM is added, migrate to a
   general `external:{platform}:{id}` identity model instead of adding
   ad hoc prefixes indefinitely.
