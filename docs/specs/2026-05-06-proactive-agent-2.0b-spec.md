# Proactive PMO Agent 2.0b — Routing Flexibility

- **Status**: Draft for implementation
- **Date**: 2026-05-06
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Strategy**: [2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md)
- **Plan**: [2.0b Plan](2026-05-06-proactive-agent-2.0b-plan.md)
- **Predecessors**: 1.0a + 1.0b + 1.0c + 2.0a (recommended).
  2.0b is technically independent of 2.0a but they compose
  naturally and the value of group-mediated rules grows when
  external events feed the pipeline.

This is the **source of truth** for 2.0b's data model,
permission rules, and routing semantics. Implementation choices
that diverge update this file.

---

## 1. Why 2.0b

In 1.0a/b/c the subscription scope conflates two things:

- **Who can edit** the rule (the rule's owner)
- **Where notifications land** (the delivery target)

Because they are the same field (`scope_kind` + `scope_id`),
real PMO routing patterns can't be expressed:

> "群里的同事在大群讨论时让 bot 提醒我私聊 albert，让他确认下"
>
> "我在 #vibelive 群设了规则，希望 PR review 提醒发给提交人本人，
> 而不是发给我"
>
> "三个人都关心这个项目，我们想在群里共同管理规则，但通知都进
> 各自私聊"

These all share the structure: **rule owner ≠ delivery target**.

Today, every subscription's delivery is hard-tied to scope:
- user-scope subscription → DM the user
- chat-scope subscription → post in the chat

2.0b decomposes scope into two independent fields and adds the
permission gates that make cross-routing safe.

---

## 2. Scope

In scope:

- Schema split: subscription `owner` vs `target`
- Three target kinds: `user_dm`, `chat`, `mention_in_chat`
- Permission gates so a third party can't weaponise the bot to
  spam others
- Chat-mediated rule creation (group members can collaboratively
  manage chat-scope rules with audit)
- UX in chat tools and web rules panel for setting target
  separately from owner
- Migration path: existing 1.0a/b/c subscriptions get default
  target = current scope (no behavior change)

Out of scope (explicitly):

- Cross-chat routing ("rule in chat A, deliver to chat B") —
  too confusing for first cut, defer
- Per-target notification preferences (different quiet hours
  per target chat) — out
- Org/team-level routing — separate problem (future 2.x or 3.0)
- Rule-discovery permissions ("who can SEE this rule") — for now
  keep all chat-owned rules visible to all chat members
- OAuth-style consent flows for "let X DM Y" — using mutual
  binding + shared chat as proof of relationship instead

---

## 3. Data model changes

### 3.1 `subscriptions` schema split

Currently:

```sql
subscriptions (
    id, scope_kind, scope_id, description,
    enabled, created_by, chat_id,
    archived_at, metadata, ...
)
```

`scope_kind` + `scope_id` mean "who owns this rule." Delivery
was implicit: scope_kind='user' → DM the scope user; scope_kind=
'chat' → post in the scope chat.

After 2.0b:

```sql
alter table public.subscriptions
    add column if not exists target_kind text
        check (target_kind in ('user_dm', 'chat', 'mention_in_chat'))
        default null;

alter table public.subscriptions
    add column if not exists target_id text default null;

alter table public.subscriptions
    add column if not exists target_user_open_id text default null;

-- Backfill: every existing subscription gets target = current scope.
update public.subscriptions
   set target_kind = case
       when scope_kind = 'user' then 'user_dm'
       when scope_kind = 'chat' then 'chat'
   end,
   target_id = scope_id
   where target_kind is null;

-- Going forward, target_kind is NOT NULL via the migration's
-- post-backfill alter.
alter table public.subscriptions
    alter column target_kind set not null;
```

Three target kinds:

| target_kind          | target_id               | target_user_open_id        | Delivery |
|----------------------|-------------------------|----------------------------|----------|
| `user_dm`            | profile uuid            | NULL                       | Bot DMs that user |
| `chat`               | feishu chat_id          | NULL                       | Bot posts in that chat |
| `mention_in_chat`    | feishu chat_id          | feishu open_id (REQUIRED)  | Bot posts in chat with `<at user_id="ou_xxx">` mention |

Constraints:

```sql
alter table public.subscriptions
    add constraint subs_target_check check (
        (target_kind = 'user_dm' and target_id ~ '^[0-9a-f-]{36}$'
         and target_user_open_id is null) or
        (target_kind = 'chat' and length(target_id) > 0
         and target_user_open_id is null) or
        (target_kind = 'mention_in_chat' and length(target_id) > 0
         and target_user_open_id is not null)
    );
```

### 3.2 Owner unchanged

`scope_kind` + `scope_id` keep their meaning: who can edit /
disable / archive this rule. The renaming to `owner_kind` /
`owner_id` would be cleaner but mass-renames a column with
existing RLS policies, so we leave the names alone and just
treat scope as semantic-owner.

For human readability, all 2.0b user-facing UX uses the words
"owner" and "target." The schema column names stay.

### 3.3 New table: `target_consents`

For permissions: when a subscription's target is a different
profile from its owner, the target user must have consented to
receive bot deliveries from this owner.

```sql
create table public.target_consents (
    id            uuid primary key default gen_random_uuid(),
    target_user_id uuid not null references public.profiles(id) on delete cascade,
    source_user_id uuid not null references public.profiles(id) on delete cascade,
    granted_at    timestamptz not null default now(),
    revoked_at    timestamptz,
    constraint consent_unique unique (target_user_id, source_user_id)
);

create index target_consents_active_idx
    on public.target_consents (target_user_id, source_user_id)
    where revoked_at is null;
```

Semantics: `(target_user_id, source_user_id)` row with
`revoked_at IS NULL` means "target_user has agreed that
source_user can route bot notifications to them."

Bootstrap: there's an **implicit consent** when both users are
in the same Feishu chat. We don't need a row in this table for
that case. Reasons:

- If you're already in the same chat as someone, they can DM
  you. The bot routing rule can't reveal anything more sensitive
  than they could do directly.
- It avoids an "ask everyone for consent" cold-start problem.

Explicit `target_consents` rows exist for cases that don't have
shared-chat fallback — primarily future expansion (e.g. cross-org
team members not in any shared chat).

### 3.4 No changes to other tables

- `notifications`, `decision_logs`, `investigation_jobs`,
  `feishu_links`, `events` — unchanged.
- `delivery_kind` and `delivery_target` columns on
  `notifications` get populated from the subscription's
  `target_kind` / `target_id` instead of from `scope_kind` /
  `scope_id`. This is a one-line change in
  `_delivery_for_subscription()` (1.0c §3.2).

---

## 4. Permission rules

The rules below answer "is target T a valid delivery for a
subscription owned by O?":

### 4.1 Owner is a user (scope_kind = 'user')

| Target kind     | Target identity              | Required for permission |
|-----------------|------------------------------|------------------------|
| `user_dm`       | the owner themselves         | always allowed |
| `user_dm`       | another user                 | EITHER `target_consents(target=user, source=owner)` exists, OR owner+target share at least one Feishu chat (queried via Feishu API at subscription creation time, cached) |
| `chat`          | any chat the owner is in     | always allowed |
| `chat`          | a chat the owner is NOT in   | not allowed |
| `mention_in_chat` | any chat the owner is in   | always allowed (the @-mentioned user receives the message visibly in chat, no surprise DM) |
| `mention_in_chat` | chat the owner is NOT in   | not allowed |

### 4.2 Owner is a chat (scope_kind = 'chat')

A chat-owned rule is created by a chat member acting on behalf
of the chat. The rule has both a `created_by` profile (who set
it up) and a `scope_id` (the chat).

| Target kind     | Allowed when |
|-----------------|--------------|
| `user_dm` to created_by | always |
| `user_dm` to another chat member | shared-chat rule applies — both parties are in this very chat, so consent is implicit |
| `user_dm` to a non-member | requires explicit `target_consents` row |
| `chat` to the same chat (this chat) | always (default for chat-scope) |
| `chat` to a different chat | not allowed (cross-chat routing is out of scope) |
| `mention_in_chat` for this chat, mentioning a chat member | always |
| `mention_in_chat` for this chat, mentioning a non-member | not allowed |

### 4.3 Permission check is at subscription creation time

The permission check runs in `add_subscription` /
`update_subscription` and `createNotificationRule` /
`updateNotificationRule`. If the target is not allowed for the
owner, the tool returns an error with a friendly message
explaining what consent or chat membership is missing.

**Delivery-time re-check is REQUIRED for cross-user targets.**
The original draft of this spec said permissions are not
re-checked at delivery — that's wrong. It would mean: A and B
share group #vibelive, A sets a rule "ping B's DM about merges,"
B leaves #vibelive a month later, A's rule keeps DMing B
forever. That's the stalker-channel scenario the spec is
supposed to prevent.

Specifically, at the moment of delivery (in
`bot/agent/delivery_loop.py`'s `_delivery_for_subscription`):

- For target_kind in `('user_dm', 'mention_in_chat')` AND
  cross-user (target identity ≠ owner identity), call
  `permissions.check_target_allowed(...)` again with current
  state.
- If the permission no longer holds (target left shared chat
  AND no explicit consent → permission denied), do NOT deliver.
  Instead:
  - Mark the notification `status = 'suppressed'` with
    `suppressed_by = 'permission_revoked'`
  - Log it for audit
  - Disable the subscription so future events stop trying

The recheck is cached per (owner_id, target_id) for 6 hours via
the same shared-chat membership cache used at creation time —
so it doesn't add a Feishu API call to every delivery, just to
the first delivery in any 6h window for a given pair.

### 4.4 Revocation paths (two)

**Explicit revoke via `revoke_target_consent`**: target user
removes a `target_consents` row → trigger fires (or daily
cleanup) finds subscriptions where:

- target_kind in ('user_dm', 'mention_in_chat')
- target identity ≠ owner identity
- target_consents row revoked
- AND no shared-chat backup (verified via cache)

…and disables them. (Disabled, not archived — target can
re-grant and re-enable.)

**Implicit revoke via leaving shared chat**: target user leaves
the only chat they shared with the rule's owner. There's no
direct event for this — Feishu doesn't push "user X left chat Y"
to the bot. The recheck path in §4.3 is what catches this:
when the cache expires (6h max) and the next delivery is about
to fire, the shared-chat check returns false, the delivery is
suppressed, and the subscription is disabled the same way as
explicit revoke.

This means there's an at-most-6-hour window where a
just-departed user could still receive a DM. Acceptable
trade-off:
- Without the recheck (the original draft): unbounded window,
  permanently broken consent.
- With the recheck on every delivery (no cache): every
  cross-user delivery triggers a Feishu API call → rate-limit
  exposure + latency.
- With 6h cache (this design): bounded staleness, bounded API
  cost, eventual consistency.

---

## 5. Chat tools changes

### 5.1 `add_subscription` extended args

```python
@tool(
    "add_subscription",
    "...",
    {
        "description": str,
        "scope_kind": str,           # already in 1.0c — owner kind
        "target_kind": str,          # NEW: 'user_dm' | 'chat' | 'mention_in_chat'
        "target_handle": str,        # NEW: for user_dm/mention, friendly name
        "target_chat_id": str,       # NEW: for chat/mention, friendly name
    },
)
```

Defaults when args are omitted:
- target_kind defaults to "same as scope" (existing behavior):
  scope_kind='user' → target_kind='user_dm', target_id=owner;
  scope_kind='chat' → target_kind='chat', target_id=chat
- if user explicitly passes target_handle, resolve via
  existing `lookup_user(handle)` to get profile_id; for
  mention_in_chat we additionally resolve open_id via
  `feishu_links.feishu_open_id` for that profile_id

### 5.2 New tool `grant_target_consent`

```python
@tool(
    "grant_target_consent",
    "Allow another pmo_agent user to route bot notifications "
    "to your DM via subscriptions they create. Use when you "
    "want a teammate to be able to forward you alerts they care "
    "about. Without this, they can only route to you if you and "
    "they share a Feishu chat. \n\n"
    "If the target user later regrets giving consent, "
    "revoke_target_consent removes it.",
    {"source_handle": str},
)
```

### 5.3 New tool `revoke_target_consent`

Mirror of grant.

### 5.4 New tool `list_target_consents`

Returns:
- consents I've granted (others can DM me through bot)
- consents granted to me (I can DM these others)

---

## 6. Web rules panel changes

The public rules panel (`/notifications/rules`) UX needs three
extensions:

### 6.1 Target picker on rule creation

When creating a rule, the form currently asks only for
description. 2.0b adds:

- "Send to" radio: My DM (default) / A chat / A specific
  person
- For "A chat": dropdown of chats the user is in (queried via
  `feishu_links` + Feishu API)
- For "A specific person": input field for handle, with
  autocomplete from `profiles` table; resolves to user_dm
  target

If the user picks a target that requires consent and consent
isn't there, the form shows: "X needs to grant you consent
first. Would you like the bot to ask them for you?" (button
sends a DM from bot to X with a "consent prompt" — see §7).

### 6.2 Group rules section

When viewing a chat's rules (new page
`/chats/<chat_id>/rules`), members can:

- See all rules with this chat as owner
- Add new rules (becomes their `created_by` row in subscriptions)
- See who created each rule
- Disable/archive rules (any member can archive, but
  `created_by` is shown for accountability)

This page is only accessible to members of the chat (verified
via `feishu_links` + Feishu API "is X a member of chat Y").

### 6.3 Consent management

A new section under `/me`:

- "People who can route alerts to me" (incoming consents)
- "People I can route alerts to" (outgoing consents)
- Grant / revoke buttons

---

## 7. Bot-mediated consent prompts

When user A wants to route to user B but doesn't have consent
or shared chat, A's flow includes "Ask the bot to request
consent." The bot then:

1. Sends a DM from itself to B saying "{A's display name} wants
   to route bot notifications to your DM via this subscription
   description: '{description}'. Reply 'yes/同意' to consent,
   'no/拒绝' to decline, or 'tell me more/详情' to see the
   subscription details."

2. B's reply is parsed (existing chat agent path); if
   yes/agree, a `target_consents` row is inserted; if no, the
   bot acks the decline.

3. A receives a follow-up DM either way.

This makes consent collection feel like a normal team
interaction rather than a permissions UI.

---

## 8. Coupling with 1.0c

These pieces of 1.0c are reused unchanged:

- `events` table and ingestion (turns + 2.0a webhooks)
- gatekeeper / decider / lockout — operate on
  `(event, subscription)` pair, target is opaque to them
- investigator — same; produces a brief, doesn't care where it
  lands
- delivery loop — already keys off
  `notifications.delivery_kind` / `delivery_target`

These pieces of 1.0c need adjustment for target_kind ≠ scope:

- `_delivery_for_subscription()` (1.0c §3.2 in spec, in
  `bot/agent/delivery_loop.py`) currently derives delivery from
  scope. 2.0b changes it to derive from target:
  ```python
  def _delivery_for_subscription(sub) -> tuple[str, str, str | None]:
      """Returns (delivery_kind, delivery_target, mention_open_id).
      delivery_kind only ever takes two values in 1.0c+2.0b:
      'feishu_user' (DM) or 'feishu_chat' (chat post). The
      mention_in_chat target kind reuses 'feishu_chat' delivery
      kind plus a non-null mention_open_id field — there is NOT
      a third 'feishu_chat_mention' delivery kind."""
      if sub.target_kind == "user_dm":
          link = feishu_link_for_user_id(sub.target_id)
          return "feishu_user", link.open_id, None
      if sub.target_kind == "chat":
          return "feishu_chat", sub.target_id, None
      if sub.target_kind == "mention_in_chat":
          # Same delivery_kind as plain chat; renderer detects the
          # mention via mention_open_id being non-null.
          return "feishu_chat", sub.target_id, sub.target_user_open_id
  ```

- Renderer behavior:
  - delivery_kind = 'feishu_chat' AND mention_open_id IS NULL →
    plain chat post (1.0c semantics)
  - delivery_kind = 'feishu_chat' AND mention_open_id non-null →
    chat post with `<at user_id="ou_xxx">` prepended to the
    rendered text. The mention target MUST be a member of the
    chat (validated at subscription creation time per §4.1; if
    membership lapses, the delivery-time recheck in §4.3 either
    suppresses or downgrades to text @handle).
- Notification table extension: add `mention_open_id text` column
  (nullable). Set at notification-creation time from
  `_delivery_for_subscription`'s third return value. Cleared
  when a 1.0a/c-style turn-source notification is created
  (legacy path doesn't set it).

---

## 9. Cost / latency budget

Permission checks at subscription creation time:
- Most cases hit the cheap path (target = owner, OR owner+target
  share a known chat from cache)
- Cold lookups call Feishu's `chats/{chat_id}/members` API
  occasionally; cached for 6h

No new LLM calls. No new background loops.

Storage:
- `target_consents` ~ 1 row per pair × N²/2 in worst case (N=20
  team members → max ~200 rows)
- `subscriptions` row size grows by ~30 bytes (3 nullable text
  fields)

---

## 10. Validation criteria

### 10.1 Existing subscriptions still deliver after migration

1. Existing 1.0c subscription "vibelive 进展告诉我" with
   scope_kind='user', scope_id=bcc.profile_id
2. Run migration 0021; verify subscription row gains
   target_kind='user_dm', target_id=bcc.profile_id
3. Trigger an event that matches; verify notification still
   reaches bcc's DM

### 10.2 Cross-DM with shared chat

1. bcc and albert share group chat #vibelive
2. bcc creates subscription targeting albert's DM
3. Permission check passes (shared chat); subscription created
4. albert's DM receives the notification

### 10.3 Cross-DM without shared chat → blocked

1. bcc and someone-not-in-any-shared-chat ("xyz")
2. bcc tries to create subscription targeting xyz's DM
3. Permission check fails; subscription NOT created; error
   message explains "xyz needs to grant consent or share a chat"

### 10.4 Consent grant flow

1. xyz uses `grant_target_consent` with source_handle='bcc'
2. bcc retries the cross-DM subscription; now permitted
3. xyz revokes consent
4. Subscription is automatically disabled (next reconciler run
   or via trigger)

### 10.5 Mention-in-chat

1. In #vibelive group, bcc creates subscription with
   target_kind='mention_in_chat', target_chat_id=#vibelive_id,
   target_user_open_id=albert.open_id
2. Event fires; notification sent to #vibelive with
   `<at user_id="ou_albert">` rendering correctly

### 10.6 Group rule UX

1. albert in #vibelive uses chat agent to create a rule for
   the chat
2. Rule appears in /chats/#vibelive/rules with created_by=albert
3. bcc (also in chat) sees the rule and can archive it
4. After archive, no more deliveries fire

---

## 11. Out of scope (still)

Carried from 1.0c §9 + 2.0a §10 plus:

- Cross-chat routing (rule in chat A delivers to chat B)
- Per-target preferences (different quiet hours per chat)
- Bulk rule import / export
- Org/team abstractions
- Rule visibility ACL beyond "all chat members can see chat
  rules"
- OAuth-style consent flows beyond bot-mediated ones in §7
