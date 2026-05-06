# Proactive PMO Agent 2.0 — Strategy

- **Status**: Strategic exploration, not committed
- **Date**: 2026-05-06 (rev. 2)
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Predecessors**: 1.0a (skeleton) + 1.0b (public rules panel) +
  1.0c (gatekeeper-investigator-renderer) all in flight

This document is the **product-level strategy** for 2.0.

The previous draft of this doc enumerated 5 unrelated candidate
features. That framing was wrong. 2.0 is not a feature list — it
is a coherent expansion along **three orthogonal axes** of the
1.0c architecture. Each axis can advance independently, and each
of them, on its own, is incomplete without the others.

---

## 1. The three axes

The 1.0c bot is a function:

```
events × subscriptions  →  (notify? topic? to whom?)  →  Feishu DM
```

Three things about that function are still impoverished:

1. **The input event stream is too narrow.** Only `turn` events
   exist. Users actually work in GitHub / Gitea / Linear /
   Feishu Calendar — most "watchable" things happen there, not
   in turns alone.

2. **The output channel is too constrained.** Subscriptions and
   deliveries are mostly user-DM-shaped. Real PMO behaviour is
   chat-mediated: people @ each other in groups, ask the bot to
   "tell albert later," set up rules collaboratively in a project
   channel.

3. **The trigger logic is too mechanical.** Today the bot reacts
   to events that match a stored subscription. A real PMO
   doesn't wait for a rule — they look at the team's state and
   decide on their own when to speak, in which room, to whom.
   1.0c is "rule-driven proactive"; 2.0 is "judgment-driven
   proactive."

Each axis maps to a piece of the 1.0c diagram:

```
Axis 1 (events)        Axis 3 (trigger logic)
       │                       │
       ▼                       ▼
   ┌────────┐  ┌────────┐  ┌──────────┐  ┌────────┐
   │ events │→ │decider │→ │investiga-│→ │renderer│→ Feishu
   │  +     │  │ (gate- │  │   tor    │  │        │
   │webhook │  │ keeper)│  │          │  │        │
   └────────┘  └────────┘  └──────────┘  └────────┘
                                              │
                                              ▼
                                    Axis 2 (output channel:
                                    DM/chat routing, who
                                    can ask whom, where)
```

All three axes preserve the 1.0c architecture. Each is its own
slice of work; together they constitute "2.0."

---

## 2. Axis 1 — External event sources

### What's happening

Today `events.source = 'turn'` is the only path. A turn is a single
person × single agent × single project interaction. But the actual
team's collaboration happens through:

- **GitHub / Gitea**: PR opened/merged, commit pushed, release
  tagged, issue commented, review requested
- **Linear / Jira**: ticket state change, sprint boundary,
  estimate vs actual
- **Feishu Calendar / Lark Doc**: meeting scheduled, doc updated,
  doc shared with you
- **Slack / Feishu chat**: someone @-mentioned you, thread spawned

The user examples you gave are GitHub-shaped:

> 每个项目A的 merge 都要发给 X 总结，让他确认技术方案
> 每次有 merge 都把当次改动的 spec 和 plan 文件发给 X

These can't be expressed in 1.0c because there's no "merge" event
to subscribe against. Subscriptions like "vibelive merge 告诉我"
are technically allowed, but they fire only when a turn happens
to mention "merge" — not on the actual merge.

### What's needed

A second event ingestion layer that mirrors the existing turn
trigger:

```
GitHub PR merged webhook
       │
       ▼
   /webhooks/github route in bot/web
       │ verifies HMAC, writes →
       ▼
   events row (source='github', source_id='pr-1234',
              user_id=mapped, project_root=/repo/X,
              payload={pr_number, title, body, diff_url, files,
                       author, reviewers, base_branch, ...})
       │
       ▼
   1.0c gatekeeper / investigator / renderer (UNCHANGED)
```

Three pieces this needs:

1. **Webhook routes**: `/webhooks/github`, `/webhooks/gitea`. HMAC
   signature verification per source. Idempotent retries (GitHub
   re-delivers on errors).

2. **Identity mapping**: `external_identities` table linking
   `(github_login, profile_id)`, `(gitea_username, profile_id)`.
   Populated either via the user telling the bot "我的 GitHub 是
   billc8128" OR via OAuth (later — start with self-claim).
   Without this mapping, gatekeeper can't apply subscriptions
   like "albert 的 PR" because it doesn't know which PR author
   counts as albert.

3. **Project mapping**: `(github_repo_full_name → project_root)`
   so a PR on `billc8128/vibelive` lands under
   `project_root='/Users/.../vibelive'` and the existing project
   lockout in 1.0c continues to work. Either store this in a new
   `external_repos` table OR derive it from the daemon's
   `project_root` for existing pmo users.

4. **Renderer enrichments**: when the brief's evidence references
   a github PR, the renderer can fetch its diff/files (cached)
   and embed a summary. Most user value here is "include the
   spec/plan files mentioned in the merge" — which means
   investigator needs the ability to read PR file contents.

The pipeline downstream is unchanged: existing gatekeeper,
investigator, renderer, delivery all work because they read
`events.payload` opaquely.

### What it unlocks

- "vibelive 项目 PR merge 时把 spec 和 plan 给 albert"
- "我提的 PR 收到 review 通知我"
- "release 标签打了之后，给项目群发布 changelog"
- "GitHub 上有人 @ 我了，提醒我去回复"

Crucially, this is also a prerequisite for Axis 3 — judgment-driven
proactive — because a real PMO uses GitHub state heavily to decide
whether something is worth speaking up about.

### Estimate / risk

**Estimate**: 5-8 days for GitHub + Gitea (mostly identity mapping
+ webhook reliability). Linear / Calendar later, separately.

**Risk**: webhook reliability and identity. If `external_identities`
isn't populated for everyone on the team, half the PRs will look
like they have no author for the bot's purposes. Bootstrap UX
needs care.

---

## 3. Axis 2 — Group-as-first-class

### What's happening

1.0a/b chose subscriptions to be either user-scoped OR chat-scoped.
The chat-scoped path was implemented but exclusively for "user @
bot in group, group becomes the subscription owner." There's no
notion of:

- **Cross-routing**: someone in a chat says "tell albert privately
  when X happens"
- **Mediated rules**: in the team channel, anyone can set up rules
  for the channel; bot accepts (with audit) instead of treating
  it as one user's request
- **Per-room conventions**: project channel has its own subscription
  set, default routing, default quiet hours

Your examples:

> 人和人在群聊里都可以让 pmo agent 发送提醒到这个群里或者和任何人的私聊里

This is the **routing flexibility gap**. Today subscription
scope determines where the notification lands. Subscription
scope = where it was created. This couples *who set it up* with
*where it's delivered*. A real PMO does both flexibly:
- "Tell me privately when this group's rule fires"
- "Tell the group when this private rule fires"
- "Tell albert (specifically) when X happens, not me"

### What's needed

Decompose subscription scope into two separate concepts:

```
Subscription
  ├── owner:  who CAN edit/disable this rule
  │           (user OR chat — same as 1.0a/b)
  └── target: where delivery LANDS
              ├── target_kind: user_dm | chat | mention_in_chat
              ├── target_id:   the open_id or chat_id
              └── target_user_open_id: optional, for "@ this user
                                       within the chat"
```

The schema split is small but the UX is not. Several questions:

1. **Permissions**: can a user set up a rule that delivers to
   ANOTHER user's DM? Probably not by default — that's spam.
   Either gate by mutual binding (both users have feishu_links
   AND have at least one shared chat) OR require explicit
   consent ("albert 同意接收 bcc 设置的提醒吗?").

2. **Group-level rule discovery**: in a chat, who can see/edit
   the chat's rules? Default: any chat member. But abusable
   when chat has 200 people. Probably: chat member can ADD,
   only original creator can DISABLE.

3. **Cross-chat routing**: "in group A, watch project X; deliver
   to group B" is technically two-table-row but UX-wise it's
   confusing. Probably defer this — start with target=this_chat
   or target=specific_user_dm.

4. **@-mention as delivery target**: bot post in chat with
   `<at user_id="ou_xxx">` mentions. Distinct from DM in that
   the whole chat sees it. User cases: "when X happens, ping
   bcc in this group instead of DM-ing him."

### What it unlocks

- Project chat as collaborative PMO surface (everyone sets rules,
  everyone sees delivery)
- "Tell albert in #vibelive when his PR breaks the build" — third
  party sets a rule about a fourth party
- Bot becomes a proper team member: people @ it, give it
  instructions about other people, and it follows them
  (within ACL)

### Estimate / risk

**Estimate**: 4-6 days for schema + routing + basic permissions
UX. Bootstrap UX (how to opt-in to receiving rules others made
about you) is the hard part.

**Risk**: stalker-by-default. Without permission gates, this
feature lets one user spam another's DM through the bot. Get the
ACL right before shipping.

---

## 4. Axis 3 — Judgment-driven proactive

### What's happening

1.0c has investigators, but they are **rule-bound**: they only run
when a subscription matched a candidate event. The investigator
decides "should I notify the user about THIS thread" but not
"should I speak up at all today, in any room, to anyone, about
anything."

Your phrasing:

> 没有做到真人 pmo 那样根据 context 自己判断什么时候应该主动
> 说话，在哪里主动说话、和谁主动说话

This is the qualitative jump from "notification system" to "PMO
agent." A real PMO does these things daily:

- "Albert hasn't shown up in stand-up logs for 3 days; nobody's
  flagged it; let me ping him." — no rule existed for this
- "The release retro is in 2 days but nobody started the doc;
  let me drop a reminder in the project channel." — situational,
  not subscribed
- "BCC mentioned the deploy issue 3 times this week, looks
  recurring; let me proactively suggest a sync between him and
  the SRE." — synthesis across people/topics
- "I noticed the team's been heads-down for 6 hours without a
  break; let me suggest a coffee." — vibe sensing

None of these match the "subscription → match → notify" model.

### What's needed

A new background process I'll call the **observer**. It's a step
above the gatekeeper, with broader inputs and looser triggers:

```
Every N minutes (e.g. every 30min), the observer:
  1. Reads the team's state — recent events across all sources,
     active investigations, recent notifications, current time,
     known goals/deadlines (from Axis 4 if it ever exists).
  2. Asks the LLM: "given this snapshot, is there anything a real
     PMO would proactively say right now? If so, what? to whom?
     in what room?"
  3. The LLM may return zero, one, or many "speech acts." Each
     speech act is essentially a synthetic investigation_job:
     "I want the bot to say X to Y in Z."
  4. Each speech act goes through the SAME investigator → renderer
     → delivery pipeline as 1.0c, with a different trigger reason.
```

**This is a different LLM mode than the gatekeeper.** Gatekeeper
asks "is this event worth investigating for THIS subscription."
Observer asks "is anything in the team's state worth speaking
about, regardless of any subscription." The same investigator
infrastructure handles both because both eventually produce a
brief that the investigator can flesh out.

Three things this needs that don't exist:

1. **Team state snapshot**: a curated view of "what's relevant
   for the observer." Not raw events — a digested narrative:
   "team activity over last 24h: bcc finished PR 1234; albert is
   3 days into vibelive player rewrite; oneship has been quiet
   since Tuesday; the pmo_agent investigator missed a beat at
   T-3min." This is itself an LLM-summarised artifact, written
   by a separate cheap call, refreshed every 15min.

2. **Speech-act schema**: structured output of the observer.

   ```jsonc
   {
     "speak": true,
     "to_whom": "bcc",                 // user_id, chat_id, or "many"
     "where": "dm",                    // dm | chat:CHAT_ID | mention_in:CHAT_ID
     "why_now": "albert went quiet for 3 days, expected daily",
     "evidence_event_ids": [...],
     "topic": "albert disappearance",
     "headline_hint": "...",
     "confidence": "low|medium|high",
     "expires_at": "..."               // observation may go stale
   }
   ```

3. **Frequency / trust budgets**: observer-driven speech is
   NOT subscribed-to. Users haven't asked for it. So the bot
   must be very stingy. Per-user-per-day cap, per-chat-per-day
   cap, "user said this kind of thing was not useful" → reduce
   confidence threshold for that type of speech act, etc. This
   is the moral hazard area: if the observer is too chatty,
   users disable the bot. If too quiet, the feature has no value.

### What it unlocks

- The bot becomes a real PMO presence, not a notification feed
- The `主动` in `proactive-agent` finally means what the name
  implies
- Most user complaints about "the bot doesn't notice obvious
  things" — covered

### Estimate / risk

**Estimate**: 10-15 days. Core observer is 4-5 days; budget /
trust UX is 5+ days; team state summariser is 2-3 days.

**Risk**: this is the FEATURE that can destroy the product if
done wrong. An over-eager observer that DMs "looks like albert is
slacking" to the wrong person, even once, is product-fatal.

The right shape is probably:
- Observer ALWAYS produces speech acts with low confidence by
  default
- Bot delivers them only with a hard-locked daily cap (e.g. 1-2
  per user per day)
- Each delivery includes a "was this useful?" UX
- That feedback feeds back into per-act-type confidence
- Speech acts user marks "not useful" 3+ times in same category
  → bot stops attempting that category for that user

This is the only 2.0 piece that requires real telemetry-driven
iteration, not just spec-and-build.

---

## 5. Are these three independent?

Mostly yes, with two coupling points:

**Axis 1 strengthens Axis 3**. The observer is much more useful
when it can see GitHub state, not just turns. "albert pushed 5
commits to vibelive in 3 hours" is a stronger signal than just
seeing turns. So if you build the observer first without
external sources, it'll be substantially weaker.

**Axis 2 enables Axis 3 to land well**. Once observer can speak
unprompted, "where to deliver" becomes critical. "Tell albert in
#vibelive when…" is exactly the routing flexibility Axis 2
provides. Without Axis 2, observer-driven speech can only land
in the asker's DM, which is the wrong room half the time.

So the natural sequence is:

```
Axis 1 (events broaden)  →  Axis 2 (output broaden)  →  Axis 3 (trigger broaden)
```

Building Axis 3 first is risky. Building Axis 1 first is safest
and sets up the rest.

---

## 6. Sequence proposal

### 2.0a — Axis 1: external event sources (5-8 days)

GitHub + Gitea webhooks. `external_identities` table.
`external_repos` mapping. Renderer enrichment for PR
diff/spec/plan files. End-to-end smoke: "bcc opens PR in
vibelive, albert with a 'vibelive merge 告诉我' subscription
gets a notification with PR summary."

### 2.0b — Axis 2: routing flexibility (4-6 days)

Decompose subscription owner from delivery target. Add
`target_kind` / `target_id` / `target_user_open_id` columns.
Group-aware UX: chat-mediated rule creation + ACL gates.
End-to-end smoke: "in #vibelive group, set rule 'tell albert in
DM when his PR breaks the build'; wire works."

### 2.0c — Axis 3: judgment-driven observer (10-15 days)

Team state snapshot job. Observer LLM call. Speech-act schema
plumbed through investigator → renderer. Trust budget +
"useful?" feedback. End-to-end: observe team for a week, see
how many speech acts fire, tune.

This is **explicitly the longest and riskiest** of the three.
Don't start until 2.0a + 2.0b are stable AND we've watched 1-2
weeks of usage data to know what kinds of "spontaneous PMO
moments" users actually want.

---

## 7. Architectural invariants 2.0 inherits from 1.0

Even with all three axes built, the following must hold:

1. **One Feishu app, one bot identity.** Group rules don't spawn
   a second bot.
2. **Events are append-only by `(source, source_id)`.** Webhook
   sources play by the same rules as turn events.
3. **Investigator is the only notify/suppress decider.** Observer
   produces candidates; investigator validates them like any
   other event.
4. **Renderer doesn't decide.** Even for observer-triggered
   speech acts, renderer faithfully transcribes the brief.
5. **No write actions.** 2.0 broadens what bot SAYS, not what
   bot DOES.
6. **Token / cost budgets per loop.** Observer in particular has
   a tight budget — it runs every 30min over the whole team's
   state.
7. **User can disable everything from chat.** If observer becomes
   annoying, "stop being so chatty" must work as a subscription
   instruction.

---

## 8. What this means for "what to build first"

If you want to commit to 2.0 work now, the right starting point
is **2.0a (external sources)**:

- Smallest scope per chunk of value (3-5 days for first source)
- Reuses 1.0c pipeline entirely; no architectural extension
- Strengthens both 2.0b and 2.0c as prerequisites
- Failure mode is bounded: webhook arrives, doesn't match any
  subscription, gets suppressed quietly. Same as a turn that
  matches nothing.

If you want to commit but can't yet (1.0c not on prod): wait,
watch 1.0c's decision_logs for ~1 week, then either:
- Many users frustrated by "GitHub events not reachable" → 2.0a
- Many users frustrated by "can't tell X to Y" → 2.0b first
- Many users say "you should've noticed Y" → 2.0c first (risky)

The default answer if no clear signal is still **2.0a**.

---

## 9. Anti-patterns to avoid

- **Don't build cross-axis abstractions early.** A "universal event
  metadata schema" or a "unified routing table" sounds clean but
  ossifies decisions before we know what they should be.
- **Don't auto-act in 2.0.** Even tempting things like "when PR
  opens, schedule code review meeting" — gate behind explicit
  user instruction every time.
- **Don't add an observer threshold knob in config.** It's a
  trust-loss accelerator. Let the bot learn from per-user
  feedback instead.
- **Don't pre-build a `teams` abstraction.** 1.0c's
  user-or-chat scope + 2.0b's flexible routing covers
  team-by-convention without a teams table.
- **Don't ship Axis 3 without "this was useless, never do this
  again" UX.** The observer's ONLY survival mechanism is
  user-controlled silence.

---

## 10. Out of scope across all axes

- Voice / phone notifications — Feishu only
- Multi-org pmo_agent — separate product
- Replacing existing tools (Linear, GitHub, calendars) — bot is
  glue, not source of truth
- Native code execution / PR creation by bot
- Pricing / billing / usage caps — until paid users
- Bot-initiated DMs to users without bound feishu_links — would
  fail at delivery anyway

---

## 11. Concrete next decision

Three options, in increasing commitment:

A) **Wait** — finish 1.0c sandbox + production, watch real
   subscriptions for 1-2 weeks, decide based on observed pain
   points.

B) **Start 2.0a (GitHub webhooks) now** — lowest-risk 2.0 work,
   doesn't depend on 1.0c usage data, sets up 2.0b/c. About
   5-8 days.

C) **Skip ahead to 2.0c (observer)** — most ambitious, biggest
   risk to product trust. Don't recommend without first having
   2.0a + 2.0b in place.

Default: A. If you can't wait: B.
