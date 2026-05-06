# Proactive PMO Agent 2.0b — Implementation Plan

- **Status**: Draft, branch `proactive-agent`
- **Date**: 2026-05-06
- **Spec**: [2.0b Spec](2026-05-06-proactive-agent-2.0b-spec.md)
- **Strategy**: [2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md)
- **Predecessors**: 1.0a + 1.0b + 1.0c required. 2.0a (external
  events) recommended but not required — 2.0b is technically
  independent and could ship first if external events aren't
  ready, though the value of routing flexibility grows when
  webhook events are available.

This is the practical "how to land 2.0b" plan. Spec is the
source of truth for behaviour. This document is the source of
truth for **build order**.

---

## 0. Pre-flight (~10 min)

- [ ] Confirm 1.0c is in production AND end-to-end working
- [ ] Confirm latest migration is 0019 (1.0c series done) or
      0020 (2.0a applied). 2.0b uses **0021**.
- [ ] Audit existing subscription rows for any non-default
      delivery — should be zero pre-2.0b. If any exist, they
      need explicit migration handling (none expected).
- [ ] Confirm at least 2 users have feishu_links bound — needed
      for cross-DM permission tests
- [ ] Confirm bot has Feishu permission `im:chat.member:read` or
      equivalent (needed for "is X a member of chat Y" lookup)

---

## 1. Migration 0021 (~30 min)

**File**: `backend/supabase/migrations/0021_subscription_routing.sql`

(Numbering assumes 2.0a shipped 0020. If 2.0b ships first,
renumber to 0020 and update 2.0a's plan to 0021.)

Creates:

- `subscriptions.target_kind` (text, check, default null)
- `subscriptions.target_id` (text, default null)
- `subscriptions.target_user_open_id` (text, default null)
- **`subscriptions.consent_anchor` (text, default null)** —
  records HOW cross-user permission was granted at creation
  time. Read by delivery-time recheck (spec §4.3). Format:
  `null` (no cross-user permission) | `explicit:{consent_uuid}`
  | `chat:{chat_id}`. Backfill leaves it NULL for all existing
  rows since none of them are cross-user.
- Backfill: every existing subscription gets target = current
  scope (per spec §3.1), consent_anchor stays NULL
- `alter column target_kind set not null` (after backfill)
- Constraint `subs_target_check` per spec §3.1
- `notifications.mention_open_id` (text, default null) — used
  by mention_in_chat delivery (per spec §8 / plan §7)
- `target_consents` table per spec §3.3
- Index `target_consents_active_idx`
- **`pending_target_consents` table per spec §3.4**: id /
  source_user_id / target_user_id / request_message_id
  (NULLABLE — see #3 fix below) / rule_description / status /
  created_at / expires_at / resolved_at. NOT a constraint
  unique (source_user_id, target_user_id, status); INSTEAD
  partial unique index on (source_user_id, target_user_id)
  WHERE status='pending' so multiple resolved/expired rows for
  the same pair don't conflict but at most one pending exists.
- New SQL RPC `mark_suppressed_if_claimed(p_notif_id, p_claim_id,
  p_suppressed_by)` per plan §7 — lease-conditional UPDATE
  flipping pending → suppressed. ACL: revoke from public/anon/
  authenticated, grant to service_role only. search_path pinned.

RLS:
- `target_consents` enabled. Policies:
  - target_user can read their own incoming consents
    (`auth.uid() = target_user_id`)
  - source_user can read their own outgoing consents
    (`auth.uid() = source_user_id`)
  - Inserts/updates only via service-role (the bot mediates)
- `pending_target_consents` enabled. Policies:
  - source_user can read their own outgoing requests
  - target_user can read their own incoming requests
  - Inserts/updates only via service-role

**Apply path**: via Supabase Management API.

**Smoke tests**:

1. **Backfill correctness**: pre-migration, count subscriptions
   by scope_kind. Post-migration, verify target_kind/target_id
   set correctly:
   - all `scope_kind='user'` rows → `target_kind='user_dm'`,
     `target_id=scope_id`
   - all `scope_kind='chat'` rows → `target_kind='chat'`,
     `target_id=scope_id`
   - target_user_open_id is NULL for all
2. **Constraint**: insert a row with `target_kind='user_dm'`
   AND `target_user_open_id` set → expect constraint violation.
3. **Constraint**: insert a row with
   `target_kind='mention_in_chat'` and target_user_open_id
   NULL → expect constraint violation.
4. **target_consents unique + UPSERT**: insert two rows with
   same (target_user_id, source_user_id) → second fails
   uniqueness. Then run the upsert per spec §3.3
   (`on conflict ... do update set revoked_at=null,
   granted_at=now()`) → succeeds, original row updated, no
   second row.
5. **target_consents revoke→regrant cycle**: insert row, set
   revoked_at, then run the upsert → revoked_at=null,
   granted_at refreshed (same row id).
6. **pending_target_consents partial unique**: insert pending
   row for pair (A,B). Insert second pending row for (A,B) →
   fails on partial unique. Resolve first row to 'declined'.
   Insert new pending row for (A,B) → succeeds (declined row
   doesn't conflict because partial unique only fires for
   status='pending').
7. **pending_target_consents request_message_id nullable**:
   insert pending row with request_message_id=NULL → succeeds.
   UPDATE to set request_message_id later → succeeds.
8. **RLS**: as anon, select target_consents +
   pending_target_consents → 0 rows. As authenticated as user
   X, select → only rows where X is target or source.

**Exit criterion**: all 8 smoke tests pass; ROLLBACK leaves DB
clean.

---

## 2. Bot DB layer additions (~30 min)

**File**: `bot/db/queries.py`

New helpers:

- `add_target_consent(target_user_id, source_user_id) -> dict` —
  upsert per spec §3.3: ON CONFLICT DO UPDATE SET
  revoked_at=null, granted_at=now()
- `revoke_target_consent(target_user_id, source_user_id) -> bool`
- `is_consent_granted(target_user_id, source_user_id) -> bool`
- `list_consents_for_user(user_id, direction='incoming'|'outgoing') -> list[dict]`
- `chat_member_open_ids(chat_id) -> set[str]` — lookup of all
  open_ids currently in a Feishu chat, cached 6h. Used for
  chat-owned-rule consent_anchor verification at both creation
  and delivery time. **The bot can call
  `chats/{chat_id}/members` against a known chat_id; it CANNOT
  enumerate "all chats user X is in" — so we never use this
  helper for user-owned rules.**
- `pending_consent_create(source_user_id, target_user_id,
  rule_description) -> uuid` — creates row in
  `pending_target_consents` with status='pending'; returns id
  (request_message_id is filled in later via
  `pending_consent_attach_message`).
- `pending_consent_attach_message(id, message_id)` — sets
  request_message_id after the bot DM has been sent.
- `pending_consent_lookup_by_reply(parent_message_id,
  sender_profile_id) -> row | None` — used by
  `_handle_message` to detect consent replies.
- `pending_consent_resolve(id, status, granted=False)` — sets
  status to 'granted'/'declined' and resolved_at; if granted=True
  also calls `add_target_consent`.

**Removed in this revision**: `users_share_chat()`. The earlier
draft used it for "user-owned cross-DM with shared chat as
implicit consent." That path was removed in spec §4.1 because
Feishu's API doesn't support enumerating two users' shared
chats reliably. User-owned cross-DM is now explicit-consent-only.

The Subscription dataclass needs FOUR more fields:
`target_kind`, `target_id`, `target_user_open_id`,
`consent_anchor`. Without `consent_anchor`, the delivery-time
recheck path can't tell whether a row needs explicit-consent
verification or chat-anchor verification.

**Exit criterion**: smoke from REPL — grant a consent, check
it's granted, revoke (UPDATE revoked_at), check is_consent_granted
returns False, re-grant via the upsert helper, check returns True
again. Pending consent: create a pending row (without message id),
attach a message id, look up by parent_message_id, resolve.

---

## 3. Permission check module (~45 min)

**File**: `bot/agent/permissions.py` (new)

Single function `check_target_allowed`:

```python
@dataclass
class PermissionResult:
    allowed: bool
    reason: str  # human-readable, surfaced in tool errors

def check_target_allowed(
    *,
    owner_kind: str,         # 'user' | 'chat'
    owner_id: str,
    target_kind: str,        # 'user_dm' | 'chat' | 'mention_in_chat'
    target_id: str,
    target_user_open_id: str | None,
    requesting_profile_id: str,  # for chat-owned, the asker
) -> PermissionResult:
    """Implements spec §4. Returns (allowed, reason)."""
```

Cases per spec §4.1 / §4.2 in order:

1. Trivial allow: target = owner (user_dm to self, chat to self)
2. Cross-DM with consent
3. Cross-DM with shared chat
4. Chat target = owning chat
5. Mention-in-chat where target user is in the chat
6. Default deny with reason

**Exit criterion**: 8+ unit tests covering each case, including
explicit deny cases (cross-DM no consent + no shared chat;
cross-chat target not allowed; mention non-member denied).

---

## 4. Chat tools updated (~1h)

**File**: `bot/agent/tools_meta.py`

### 4.1 `add_subscription` extended

Args gain `target_kind`, `target_handle`, `target_chat_id`. New
flow:

1. Resolve owner from existing scope inference logic (1.0a §5.0)
2. Resolve target:
   - If `target_kind` not provided: default to "same as owner"
   - Else: parse target_handle (resolve via lookup_user) or
     target_chat_id (validate via Feishu API)
3. Run `check_target_allowed`; on deny, return error with
   reason
4. Insert row with explicit target columns
5. If `target_kind != owner-default`, send a courtesy DM to
   target user explaining "X created a rule that will alert
   you about Y"

### 4.2 New tools

- `grant_target_consent(source_handle: str)` — asker grants
  consent for source to route to asker's DM
- `revoke_target_consent(source_handle: str)`
- `list_target_consents()` — returns both directions

### 4.3 Update `update_subscription`

Allow updating `target_*` fields too (spec §3.2 said "scope
immutable" but 1.0a's restriction was for safety; 2.0b allows
target changes since they don't affect rule ownership). Same
permission check before applying.

### 4.4 Update `list_subscriptions`

Returned rows include the new target columns so the user can
see where their rules deliver, not just what they say.

**Exit criterion**: from chat —
- "vibelive 进展告诉 albert" → asker creates user_dm target =
  albert; if albert is in shared chat, allowed; if not,
  consent prompt path
- "我都订了什么" → bot lists rules with target column
- "albert 同意 bcc 给他发消息" (from albert's chat) — grants
  consent

---

## 5. Web rules panel changes (~2h)

**File**: `web/app/notifications/rules/page.tsx`,
`actions.ts`, `rules-panel.tsx`.

### 5.1 Target picker on rule create form

Three radio options + conditional inputs:
- "My DM" (default; current behavior)
- "A chat" (dropdown of chats the user is in — fetched from
  `feishu_links` joined with bot's chat-membership query)
- "Specific person" (text input with handle autocomplete from
  `profiles` table)

For "Specific person," after the user types a handle and
deselects, the page calls a server action that runs
`check_target_allowed`. If the result is "needs consent," the
form shows: "X needs to grant you permission. [Send request]"
button → sends a DM via bot.

### 5.2 Rule list shows target

Each rule row in the existing list gains a "→" indicator with
the target. Examples:
- "vibelive 进展告诉我 → My DM"
- "vibelive merge → albert's DM"
- "release → #vibelive"
- "PR review → @albert in #vibelive"

### 5.3 Group rules page

New route `/chats/[chat_id]/rules`. Page logic:

- Verify viewer is a member of chat_id (via
  `feishu_links.user_id` + bot's chat-membership query)
- Fetch rules where scope_kind='chat' AND scope_id=chat_id
- Show same UI as /me/notifications but for chat-owned rules
- Adding rules: form sets scope_kind='chat', scope_id=chat_id,
  created_by=viewer.profile_id

### 5.4 Consent management on /me

New section "People who can route alerts to me / People I can
route alerts to," each with grant/revoke buttons.

**Exit criterion**: visible on production deploy of the rules
panel:
- Rule create form has the 3-option target picker
- Rule list shows target column
- /chats/<chat_id>/rules works for chat members and rejects
  non-members
- /me has consent management

---

## 6. Bot-mediated consent prompt (~45 min)

**Flow** in spec §7. Implementation:

- Web "Send request" button → server action that DMs B from bot
  with the consent request text + a hint that B can reply
  yes/no/details
- Bot's chat agent path (already exists via Feishu webhook +
  `_handle_message`) needs to recognize replies to bot DMs that
  look like consent acknowledgments. Two ways:
  1. Detect via reply_to (the DM is a reply to the bot's
     consent-request message) → trust the conversation context
  2. Pattern-match common phrases: "同意", "拒绝", "yes", "no",
     "tell me more"

When the bot identifies a yes:
- Insert `target_consents` row
- DM the source user "X agreed to receive your alerts"
- DM B "consent saved; you can revoke anytime via revoke command"

When no:
- DM source: "X declined"
- No row inserted

Other replies (like "tell me more"): bot replies with the rule
description and asks again.

**Exit criterion**: end-to-end manual test:
- bcc on web tries to route to xyz; "needs consent" prompt
- bcc clicks "Send request"
- xyz's Feishu DM has bot message
- xyz replies "同意"
- bcc retries on web; subscription created
- xyz revokes via chat tool; subscription disabled

---

## 7. Delivery loop adjustments (~45 min)

**File**: `bot/agent/delivery_loop.py`

`_delivery_for_subscription` updated per spec §8:

```python
def _delivery_for_subscription(sub: Subscription) -> tuple[
    str, str, str | None
] | None:
    """Returns (delivery_kind, delivery_target, mention_open_id),
    or None when permission has been revoked (caller should mark
    the notification suppressed and disable the subscription).

    Per spec §4.3, cross-user targets get a delivery-time
    permission re-check. The recheck path is keyed on
    `consent_anchor` (NOT on scope_kind), so chat-owned rules
    with cross-user user_dm targets also recheck.
    """
    # Recheck applies whenever consent_anchor is non-null.
    # Anchor=null means "no cross-user permission needed"
    # (e.g. user-owned rule targeting self, or chat-owned rule
    # targeting same chat) → no recheck.
    if sub.consent_anchor is not None:
        if not _consent_still_valid(sub):
            return None

    if sub.target_kind == "user_dm":
        link = queries.feishu_link_for_user_id(sub.target_id)
        if not link or not link.feishu_open_id:
            return "feishu_user", "", None  # delivery will fail
        return "feishu_user", link.feishu_open_id, None
    if sub.target_kind == "chat":
        return "feishu_chat", sub.target_id, None
    if sub.target_kind == "mention_in_chat":
        return "feishu_chat", sub.target_id, sub.target_user_open_id
    raise ValueError(f"unknown target_kind: {sub.target_kind}")


def _consent_still_valid(sub: Subscription) -> bool:
    """Re-checks permission per the consent_anchor format.

    Per spec §4.3:
    - explicit:CONSENT_ID  -> re-read target_consents row, NO
                              positive cache (revokes must take
                              effect immediately, even within
                              the would-be cache window)
    - chat:CHAT_ID         -> re-fetch chat membership, cache
                              positive results 6h
    """
    anchor = sub.consent_anchor or ""
    if anchor.startswith("explicit:"):
        consent_id = anchor.split(":", 1)[1]
        # Direct DB read; no cache. Cheap (single-row UUID lookup)
        # and revoke must propagate to the next delivery.
        row = queries.target_consent_get(consent_id)
        return bool(row and row.get("revoked_at") is None)

    if anchor.startswith("chat:"):
        chat_id = anchor.split(":", 1)[1]
        # Both source and target must still be members of the
        # anchor chat. The chat_member_open_ids helper caches
        # 6h; we compute the boolean per-call.
        members = queries.chat_member_open_ids(chat_id)
        # Need owner's open_id and target's open_id.
        # Owner: if scope_kind=user, sub.scope_id=profile, look up
        #        feishu_link. If scope_kind=chat, owner concept is
        #        a chat, but for cross-user user_dm the original
        #        creator's profile is in sub.created_by.
        owner_profile = sub.created_by if sub.scope_kind == "chat" \
                        else sub.scope_id
        owner_link = queries.feishu_link_for_user_id(owner_profile)
        target_link = queries.feishu_link_for_user_id(sub.target_id) \
                      if sub.target_kind == "user_dm" else None
        owner_open_id = owner_link.feishu_open_id if owner_link else None
        target_open_id = (
            target_link.feishu_open_id if target_link
            else sub.target_user_open_id  # for mention_in_chat
        )
        if not owner_open_id or not target_open_id:
            return False
        return owner_open_id in members and target_open_id in members

    # Unknown anchor format → fail closed
    return False
```

**Caller** (the delivery loop's `process_pending` per 1.0c
§4.4) handles the None return. Critically, **the suppression
must be lease-conditional** — same pattern as `mark_sent_if_claimed`
in 1.0c. Without the claim_id guard, a stale worker that lost
the lease could overwrite a notification another worker is
already sending:

```python
delivery = _delivery_for_subscription(sub)
if delivery is None:
    # Lease-conditional. If our claim has been reaped/lost,
    # this returns False and we let the next claim cycle
    # handle the row.
    suppressed = queries.mark_suppressed_if_claimed(
        notif_id=notif.id,
        claim_id=current_claim_id,
        suppressed_by="permission_revoked",
    )
    if not suppressed:
        logger.warning(
            "permission_revoked: lost claim on notif=%s; skipping",
            notif.id,
        )
        continue
    queries.update_subscription(
        subscription_id=sub.id,
        scope_kind=sub.scope_kind, scope_id=sub.scope_id,
        enabled=False,
    )
    logger.warning(
        "permission_revoked: disabled sub=%s (owner=%s target=%s)",
        sub.id, sub.scope_id, sub.target_id,
    )
    continue
```

**New RPC** in migration 0021:

```sql
create or replace function public.mark_suppressed_if_claimed(
    p_notif_id bigint,
    p_claim_id uuid,
    p_suppressed_by text
) returns bigint
language sql
security definer
as $$
    update public.notifications
       set status = 'suppressed',
           suppressed_by = p_suppressed_by,
           claim_id = null,
           claimed_at = null,
           updated_at = now()
     where id = p_notif_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;
```

Same shape as 1.0c's lease-conditional RPCs (mark_sent_if_claimed,
mark_failed_if_claimed). Only the lease-holder can flip the row
to suppressed; stale workers see 0 rows returned.

ACL: revoke from public/anon/authenticated, grant to
service_role only. search_path pinned. Mirrors the §1 ACL
pattern.

**Test for stale claim**: in addition to the cross-DM
revocation tests, add `test_permission_revoked_respects_lease`:
- Worker A claims notif 1 (claim_id=A).
- Worker A's claim is reaped; worker B claims it (claim_id=B).
- Worker A's revoked-permission code path tries to call
  `mark_suppressed_if_claimed(1, A, ...)` → returns NULL.
- Worker B is unaffected; the row keeps B's claim and its
  natural delivery flow.

Notification row gains an additional optional column
`mention_open_id` (or this gets stuffed into payload_snapshot
as a sidecar field — choose simpler: add a column).

Renderer reads `mention_open_id` from notification row; when
present and delivery_kind='feishu_chat', wraps the brief
opening with `<at user_id="ou_xxx">` mention.

**Files touched**:
- `bot/agent/delivery_loop.py`
- `bot/agent/renderer.py` — add mention rendering
- `bot/db/queries.py` — Notification dataclass gains
  `mention_open_id`; create_notification RPC takes it

**Migration**: add the column to notifications:

```sql
alter table public.notifications
    add column if not exists mention_open_id text;
```

This is a small alter that goes into 0019 alongside the rest.

**Exit criterion**: a mention_in_chat subscription generates a
notification that, when sent to Feishu, includes the @-mention.

---

## 8. End-to-end validation (~1h)

Run the validation scripts from spec §10 (numbering aligns with
spec §10's revised list — no longer includes "shared-chat
implicit consent for user-owned rules" since that path was
removed in §4.1):

1. Existing subscriptions still deliver post-migration
2. User-owned cross-DM **without explicit consent** → blocked
   (no shared-chat fallback, regardless of common chats)
3. User-owned cross-DM **with explicit consent** → allowed
4. Consent grant flow via bot-mediated prompt (pending row →
   reply with parent_message_id match → resolved + consent row)
5. Consent prompt does NOT match a casual "yes" reply that has
   no parent_message_id link to a pending row
6. Chat-owned rule with cross-user target who IS a chat member
   → allowed via consent_anchor=chat:{C}
7. Chat-owned rule with cross-user target who is NOT a chat
   member → requires explicit consent
8. Delivery-time recheck for chat anchor: user leaves anchor
   chat → next delivery (after cache expiry) suppressed via
   `mark_suppressed_if_claimed` + subscription disabled
9. Delivery-time recheck for explicit consent: target revokes
   → next delivery suppressed (no positive cache delay for
   explicit revoke; see plan §7)
10. mention_in_chat (target is chat member) → renders
    `<at user_id="ou_xxx">` correctly
11. Group rule UX: chat members can manage chat-scoped rules

**Exit criterion**: 11/11 validation scripts pass.

---

## 9. Roadmap update (~10 min)

Mark 2.0b done in roadmap §2.0:
- Move 2.0b from "next" to "deployed"
- Update notes about 2.0c (observer) — it can now use flexible
  routing for speech acts

---

## 10. Commit + push

```
2.0b: routing flexibility (owner ≠ target)

Decompose subscription scope into rule owner (who can edit) vs
delivery target (where notifications land). Three target kinds:
user_dm, chat, mention_in_chat. Permission gates so cross-DM
routing requires either explicit consent (target_consents table)
or shared Feishu chat — no surprise spam.

See docs/specs/2026-05-06-proactive-agent-2.0b-spec.md for full
behavior contract; this commit implements §3 schema, §4 perms,
§5/§6 UX (chat tools + web), §7 bot-mediated consent prompt, §8
delivery layer adaptations.

Existing 1.0c subscriptions are backfilled with target = current
scope (no behavior change). New target options are opt-in.
```

Push, deploy via Railway + Vercel.

---

## Cut points

If time-pressured:

- **Skip §6 (bot-mediated consent prompt)** — initial version
  can require users to use chat tools directly to grant
  consent. Web's "send request" button can come later.
- **Skip §5.3 (group rules page)** — chat members can manage
  rules via chat tools only; web UI is a polish add-on.
- **Skip mention_in_chat target kind initially** — start with
  only user_dm and chat. mention_in_chat is the riciest target
  for delivery render (needs open_id resolution). Add in a
  followup.

Don't cut: §1 migration with backfill, §3 permission check,
§4.1 add_subscription extension, §7 delivery loop adaptation.
That's the irreducible 2.0b — anything less can't express the
core "owner ≠ target" use cases.

---

## Risks specific to 2.0b rollout

1. **Stalker channel risk**. Without permission checks, this
   feature lets bcc DM-spam albert through the bot. Mitigation:
   spec §4 permission rules MUST be in place before any
   cross-target delivery is allowed. Add automated test that
   verifies permission denials.

2. **Feishu chat membership lookups can rate-limit**. The
   shared-chat permission check requires Feishu API calls.
   Mitigation: 6h cache per (user_a, user_b) pair. Worst case:
   degraded permission resolution falls back to "deny by
   default" (fail closed).

3. **Group rules become noisy**. Chats with 50+ members where
   anyone can add rules → potential rule-explosion. Mitigation:
   `created_by` audit + UI showing each rule's creator;
   consider per-chat rule cap in 2.0c if abuse appears.

4. **Backfill correctness**. The migration backfills existing
   subscriptions to default targets. If any existing rule had
   non-default delivery (none expected pre-2.0b but check),
   backfill could break it. Mitigation: pre-migration query to
   audit subscription state; abort if surprises.

5. **mention_in_chat target user not in chat**. The
   permission check enforces "target user is in chat" at
   creation time, but membership can change. Mitigation:
   delivery layer falls back to text-mention (`@handle`) if
   `<at user_id>` resolution returns "not a member" from
   Feishu. Renderer handles this gracefully (reuses 1.0c's
   fallback).
