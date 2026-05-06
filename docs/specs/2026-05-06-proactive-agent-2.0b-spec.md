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

-- Records HOW the cross-user permission was granted at creation
-- time. Read at delivery time by the permission re-check path.
-- Format:
--   null                        - no cross-user permission needed
--   'explicit:{consent_uuid}'   - target_consents row backing this
--   'chat:{chat_id}'            - chat that vouched for both parties
alter table public.subscriptions
    add column if not exists consent_anchor text default null;

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

**Lifecycle is single-row UPSERT, not insert-then-archive.**
Revoke is `UPDATE ... SET revoked_at = now()`. Re-grant is
`UPDATE ... SET revoked_at = null, granted_at = now()` on the
SAME row. The table never accumulates two rows per pair —
that's why the constraint is `(target_user_id, source_user_id)`
without status. The corresponding helper:

```python
def add_target_consent(target_user_id, source_user_id):
    sb_admin().table("target_consents").upsert({
        "target_user_id": target_user_id,
        "source_user_id": source_user_id,
        "revoked_at": None,
        "granted_at": "now()",
    }, on_conflict="target_user_id,source_user_id").execute()
```

Or in pure SQL terms:

```sql
insert into target_consents (target_user_id, source_user_id)
values (...)
on conflict (target_user_id, source_user_id)
do update set revoked_at = null,
              granted_at = now();
```

This pattern lets a target who declined or revoked previously
re-grant by simply running through the consent prompt again —
no schema cleanup needed.

Explicit `target_consents` rows are now the **default** path for
user-owned cross-DM rules (per §4.1) and the only path for
chat-owned rules where the target is not a current member of
the anchor chat.

Implicit consent (no row in this table) only applies inside
a chat-owned rule's anchor chat (§4.2). User-owned rules from
DM context have no chat to anchor implicit consent against, so
they always need an explicit row here.

### 3.4 New table: `pending_target_consents`

Bot-mediated consent prompts (§7) need state. Without a record
that "user A asked user B for consent on date D, message id M,"
the bot can't tell whether B's casual "ok" reply is an answer
to the consent prompt OR an unrelated response in a busy
conversation.

```sql
create table public.pending_target_consents (
    id                  uuid primary key default gen_random_uuid(),
    source_user_id      uuid not null references public.profiles(id) on delete cascade,
    target_user_id      uuid not null references public.profiles(id) on delete cascade,
    request_message_id  text not null,    -- Feishu message id of the
                                          -- bot's request DM, used to
                                          -- match parent_id replies
    rule_description    text not null,    -- the rule the source wants
                                          -- to set up (shown to target)
    status              text not null default 'pending' check (
                            status in ('pending', 'granted', 'declined', 'expired')
                        ),
    created_at          timestamptz not null default now(),
    expires_at          timestamptz not null default (now() + interval '7 days'),
    resolved_at         timestamptz,
    -- A given pair can have at most one pending request at a time;
    -- previously-resolved requests don't block new ones (status filter).
    constraint pending_consent_one_active
        unique (source_user_id, target_user_id, status)
);

create index pending_consent_message_idx
    on public.pending_target_consents (request_message_id);

create index pending_consent_expires_idx
    on public.pending_target_consents (expires_at)
    where status = 'pending';
```

Reply detection logic in `_handle_message` (1.0a path) consults
this table:

1. Incoming Feishu message has `parent_message_id` matching a
   row's `request_message_id` AND `status='pending'` AND
   `target_user_id = sender_profile_id` → this IS a consent
   reply. Parse intent (yes / no / details) and resolve.
2. Otherwise → not a consent reply. Treat the message as a
   normal chat agent message; "yes" / "ok" do nothing
   privileged.

The bot does NOT pattern-match "yes"/"agree" without an active
pending row keyed to the message thread. This is the design
choice that keeps the bot from accidentally granting permissions
when users casually agree to other things.

Expiry: pending requests auto-`expired` after 7 days via daily
cleanup; once expired, a new request can be opened (the unique
constraint excludes resolved/expired states because the partial
unique only fires for `status='pending'`... actually the
constraint as written includes status in the key — re-grants
work because the uniqueness key includes status, so old
expired/declined rows don't conflict with a new pending row).

### 3.5 No changes to other tables

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
subscription owned by O?". A central design principle:
**implicit consent only applies inside a specific known chat
context**. We do NOT enumerate "all chats two users share" —
Feishu's API doesn't expose that reliably and the bot has no
authoritative chat_memberships table. Implicit consent only
happens when the rule itself is being created INSIDE a chat
where both owner and target are visible chat members.

### 4.1 Owner is a user (scope_kind = 'user')

User-owned rules are created from a private DM with the bot.
There is **no chat context to anchor implicit consent**. So:

| Target kind     | Target identity              | Required for permission |
|-----------------|------------------------------|------------------------|
| `user_dm`       | the owner themselves         | always allowed |
| `user_dm`       | another user                 | requires explicit `target_consents(target=user, source=owner)` row, status=granted |
| `chat`          | a chat the owner is in       | always allowed |
| `chat`          | a chat the owner is NOT in   | not allowed |
| `mention_in_chat` | owner in chat AND target user IS a current member of chat AND target ≠ owner | requires `target_consents` (explicit) — the at-mention WILL DM the target via Feishu's notification, so this is functionally equivalent to user_dm cross-user |
| `mention_in_chat` | owner in chat AND target = self | always allowed |
| `mention_in_chat` | owner NOT in chat (regardless of target) | not allowed |
| `mention_in_chat` | owner in chat BUT target user not a chat member | not allowed (Feishu won't render `<at>` for non-members; renderer would silently drop the mention; better to fail at creation) |

User-owned cross-DM rules are explicit-consent-only by design.
The "shared chat as implicit proof of relationship" was tempting
but turned out to be unimplementable cleanly: the bot can call
Feishu's `chats/{chat_id}/members` for a specific chat but not
"give me all chats user A and user B both belong to." Without
that primitive, implicit consent for user-owned rules is a
permission rule we can't actually verify.

### 4.2 Owner is a chat (scope_kind = 'chat')

Chat-owned rules ARE created with a chat context — by definition,
the user who created the rule was in chat C when they did so.
This is the only place where implicit consent applies.

The rule's owner (chat C) is the implicit-consent anchor. At
creation time, both the rule's `created_by` profile AND any
referenced target user MUST be members of chat C, verified via
Feishu's `chats/{C}/members` API at that moment.

| Target kind     | Allowed when |
|-----------------|--------------|
| `user_dm` to created_by | always |
| `user_dm` to another current member of chat C | implicit consent (because both are in chat C right now); recorded as `consent_anchor=chat:{C}` on the subscription so delivery-time re-checks know what to verify |
| `user_dm` to anyone NOT a current member of C | requires explicit `target_consents` row |
| `chat` to the same chat C | always |
| `chat` to a different chat | not allowed (cross-chat routing out of scope) |
| `mention_in_chat` mentioning a current member of chat C | always (visible in-chat, no surprise DM) |
| `mention_in_chat` mentioning a non-member of C | not allowed |

The new column `subscriptions.consent_anchor` (text, nullable)
records HOW permission was granted at creation time. Three
possible values:

- `null` — no cross-user permission needed (target = owner OR
  target = chat where owner = chat)
- `explicit:CONSENT_ID` — the `target_consents` row backing this
- `chat:CHAT_ID` — the chat that vouched for both parties at
  creation time

Delivery-time re-check (§4.3) reads `consent_anchor` to know
what to verify: explicit consents check the `target_consents`
row; chat anchors re-verify both parties are still members of
the anchor chat.

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
  cross-user (target identity ≠ owner identity), check the
  subscription's `consent_anchor`:
  - `consent_anchor = explicit:CONSENT_ID` → re-read the
    `target_consents` row; if `revoked_at IS NOT NULL`, deny
  - `consent_anchor = chat:CHAT_ID` → re-call
    `chats/CHAT_ID/members` (cached 6h) to confirm BOTH
    `created_by` profile and target profile are still members
    of CHAT_ID; if either has left, deny
- If permission denies, do NOT deliver. Instead:
  - Atomically (lease-conditional, same claim_id pattern as
    1.0c's `mark_sent_if_claimed`) mark the notification
    `status = 'suppressed'`, `suppressed_by = 'permission_revoked'`
    via `mark_suppressed_if_claimed(notif_id, claim_id, …)` RPC
  - Log it for audit
  - Disable the subscription so future events stop trying

The recheck is cached per `consent_anchor` for 6 hours.
`chats/{CHAT_ID}/members` is the only Feishu API call we need
here, and we already need it at creation time. Cache scope is
keyed on `(consent_anchor, target_user_id)` so revoking one
target's consent doesn't invalidate other targets in the same
anchor chat.

### 4.4 Revocation paths

**Explicit revoke via `revoke_target_consent`**: target user
removes a `target_consents` row (sets revoked_at to now()).
A daily cleanup job finds subscriptions with
`consent_anchor = explicit:CONSENT_ID` for the revoked consent
and disables them. The next delivery attempt for any of those
subscriptions, even within the cache window, will hit the
explicit revoked_at check.

(Disabled, not archived — target can re-grant and the rule
can be re-enabled by the owner.)

**Implicit revoke via leaving the anchor chat**: target user
leaves CHAT_ID where the rule was created. There's no direct
event for this — Feishu doesn't push "user X left chat Y" to
the bot. The recheck path in §4.3 catches it when the
membership cache expires (6h max): the next delivery sees the
target is no longer in the anchor chat, the delivery is
suppressed via lease-conditional `mark_suppressed_if_claimed`,
and the subscription is disabled.

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

When user A wants to route to user B but doesn't have explicit
consent (and per §4.1 user-owned rules require explicit consent
for cross-DM), A's flow includes "Ask the bot to request
consent." The bot then:

1. Inserts a `pending_target_consents` row with
   `source_user_id=A, target_user_id=B, status='pending',
   rule_description='{description}'`.
2. Sends a DM from itself to B with rich text:
   ```
   {A's display name} wants to route bot notifications to your DM
   for the rule: "{description}".

   Reply to this message:
   - 同意 / yes / agree — grants consent
   - 拒绝 / no / decline — declines
   - 详情 / tell me more — show more about the rule
   ```
3. Captures the bot's outgoing message_id, writes it back to
   the `pending_target_consents.request_message_id`.

When B replies, the existing `_handle_message` path checks the
incoming message's `parent_message_id`:

- If parent matches a `pending_target_consents.request_message_id`
  AND `status='pending'` AND `target_user_id = sender's profile_id`:
  this IS a consent reply. Parse the text:
  - "同意" / "yes" / "agree" / "ok" / "好" → resolve
    `granted`, INSERT-OR-UPSERT `target_consents` row (per #4
    fix below), DM A "B granted consent."
  - "拒绝" / "no" / "decline" / "不要" / "不行" → resolve
    `declined`, no `target_consents` row, DM A "B declined."
  - "详情" / "details" / "tell me more" / unrecognised → bot
    DMs B with the full subscription detail and asks again
    (does NOT resolve the pending row)
- If parent does NOT match any pending row, the message is a
  normal agent message. "yes" / "ok" alone do NOT grant
  permission.

This is the critical anti-pattern guard: we never pattern-match
consent words without confirming the message is a reply to a
specific pending request. Casually saying "ok" in a different
DM context cannot accidentally authorize cross-user routing.

Pending requests expire after 7 days; A can issue a fresh
request after expiry.

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
