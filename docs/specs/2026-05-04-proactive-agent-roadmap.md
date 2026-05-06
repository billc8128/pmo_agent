# Proactive PMO Agent — Roadmap

- **Status**: Draft, branch `proactive-agent`
- **Date**: 2026-05-04
- **Companion docs**:
  - [1.0a Spec](2026-05-04-proactive-agent-1.0a-spec.md) — what we're building first
  - [1.0a Plan](2026-05-04-proactive-agent-1.0a-plan.md) — how we're building it

This document is the **strategic roadmap** for turning the existing
question-answering pmo_agent bot into a proactive PMO that watches
events, applies user-supplied preferences, and pushes notifications.

The 1.0a spec is the source of truth for the first slice. This file
exists to keep later slices and the long-term vision honest about
where they fit, so we don't forget why we're choosing the boundaries
we're choosing in 1.0a.

---

## 1. Product framing

### Today (passive PMO)

The bot answers questions on demand:

> 你: bcc 昨天做了啥？
> bot: bcc 昨天主要在 pmo_agent ...

User pulls. Bot looks up turns and summarises.

### Where we're going (proactive PMO)

The bot also pushes:

> 🔔 albert 在 vibelive 调了播放器 buffer，从 2MB 改到 5MB,
> 看 turn 上下文是为了解决直播首帧延迟。

System pushes. Bot decides when, what, to whom.

The key product insight: **the same bot, the same data, the same
tools — just a second trigger path**. Users see one Feishu app, one
conversation history, mixing pulls and pushes naturally.

### Mental model

A "PMO 小助理" — a personal assistant whose job is to:

1. Watch the team's event stream (turns, pushes, …)
2. Read each user's natural-language preferences ("vibelive 有进展告诉我",
   "凌晨别打扰", "项目 C 不要发了")
3. Decide what's worth telling whom, when
4. Write a short brief and send it

The same metaphor cleanly answers boundary questions ("Should it
auto-merge PRs?" — would your assistant? No.) — see the 1.0a spec
for how this is operationalised.

---

## 2. Three-stage roadmap

```
1.0a  ──────────►  1.0b  ──────────►  1.0c  ──────────►  2.0
skeleton           UI panel           more sources        team-level
                                      + investigator      + planning
```

Each stage builds on the previous. **We commit only to 1.0a now.**
1.0b/c/2.0 are sketched here so the architecture chosen in 1.0a
doesn't paint us into a corner later.

### 1.0a — Skeleton (this PR)

**Goal**: Get one end-to-end loop working — turn event → LLM
decision → notification delivered to Feishu — and validate that the
LLM judge produces useful decisions.

**User experience**

✅ in scope
- Conversational subscription management in Feishu
  - "vibelive 项目有进展告诉我" → bot stores subscription verbatim
  - "项目 C 不要发了" → another subscription, exclusion
  - "我都订阅了什么" → bot lists all
  - "取消第二条" → bot deletes
- Group chats can subscribe too — when a user @s the bot in a group
  and asks to subscribe, the subscription is owned by the group
  (delivery target = group chat). Private chat subscription owns by
  the user (delivery target = DM).
- Bot pushes proactive notifications to private chats and groups
- Following up under a notification carries that notification's
  context into the agent's reply
- "Why didn't you tell me about X?" — the agent can look up
  decision logs and explain
- Saying "今晚别打扰我" creates an ad-hoc quiet-hours subscription
- Notifications mention the event subject(s) by Feishu @open_id

❌ deliberately out of scope
- No web UI yet (manage subs in chat only)
- No GitHub / Gitea webhooks (turns are the only event source)
- No interactive buttons on notification cards
- No batched / digested notifications (one event → at most one
  notification per matched subscription per dedup window)
- No importance grading (quiet hours are absolute)
- Group notifications never @ the whole channel

**Validation criterion**: Have albert run 5 vibelive turns, confirm
1-2 minutes later you (with a "vibelive 进展告诉我" subscription) get
3-4 notifications that read well and don't spam.

**Estimate**: ~5h end-to-end.

### 1.0b — Public rules panel

**Goal**: Visualise the rules 1.0a built. No notification-decision
logic changes, just surfaces and management.

✅ added
- `/notifications/rules` is a public rules directory:
  - everyone can see active user-scope notification rules from all
    users
  - only safe fields are exposed: rule text, owner handle/display
    name, enabled status, and timestamps
  - raw `scope_id`, `created_by`, `chat_id`, Feishu IDs, and delivery
    targets never reach the browser
- Signed-in users can add a user-scope rule from the public page
- Signed-in users can pause/resume, edit, or archive their own rules
- `/me` links to the public rules panel
- `subscriptions.archived_at` separates archived/deleted rules from
  paused rules; existing disabled rows migrate to archived because
  there was no pause feature before 1.0b

❌ still out
- `/me/notifications` notification history
- Quiet-hour / daily-cap preference tables
- Group/chat rule management
- GitHub webhook (1.0c)
- Card buttons (1.0c)
- Per-team notifications (2.0)

**Estimate**: ~3h.

### 1.0c — Investigation-driven proactive PMO

**Goal**: Stop treating a single event as the notification decision.
The 1.0a decider is useful as a skeleton, but real subscriptions are
often "watch this topic and tell me when it matters", not "judge this
one turn in isolation". 1.0c changes the architecture so the first
LLM call decides whether an event is worth investigation, and a PMO
investigator agent reads enough context before deciding whether to
notify the user.

**New mental model**

```
turn / push event
  -> decider: is this worth PMO investigation for this subscription?
  -> investigation_job
  -> PMO investigator: read enough context, decide notify/suppress
  -> renderer
  -> Feishu
```

The decider becomes a lightweight gatekeeper. It no longer answers
"should we notify the user?" It answers "should the PMO agent spend
time looking into this for this subscription?" False positives are
acceptable because the investigator can suppress after reading more
context. False negatives are the main risk, so the decider prompt
should prefer "investigate" when the event plausibly relates to the
subscription.

**Added**

- `investigation_jobs` table:
  - `subscription_id`
  - `status = open | investigating | notified | suppressed | failed`
  - `seed_event_ids`
  - `initial_focus`
  - `decider_reason`
  - `flush_after`
  - `opened_at`, `updated_at`, `closed_at`
  - `investigator_decision`
- Decider loop writes / appends investigation jobs instead of writing
  notification decisions directly. The first 1.0c cut should still
  aggregate: for a given subscription, keep one open job per window
  (default 30 minutes). New plausible events append to that job's
  `seed_event_ids` until the job flushes. Flush when the window
  expires, when the job reaches a small event threshold (e.g. 3
  seeds), or when the seed event is explicitly conclusive ("完成",
  "上线", "决定", "阻塞解除"). After a job closes, the next plausible
  event opens a new job, and the investigator receives the last
  closed job outcome as dedup context.
- PMO investigator loop:
  - loads the subscription's original natural-language description
  - reads seed event turns
  - reads same-project recent turns
  - reads same-session nearby turns
  - checks recent notifications / decision history
  - stays within a fixed context budget: all seed turns up to a cap,
    at most 20 same-project recent turns, at most 5 same-session
    neighbouring turns per seed, and at most 10 recent notifications
    / decisions. If more context exists, the investigator should
    sample by recency and topic relevance instead of dumping the
    whole project history.
  - decides `notify=true|false`, with evidence event ids and a
    structured notification brief
- Investigator output is the authority for the notification decision:

  ```json
  {
    "notify": true,
    "reason": "why this is worth telling the user",
    "evidence_event_ids": [57, 61, 64],
    "brief": {
      "recommended_topic": "vibelive 播放器稳定性方案",
      "key_facts": [
        "buffer 从 2MB 调到 5MB",
        "新增卡顿重试日志",
        "补了丢帧监控"
      ],
      "risk_or_next_step": "继续观察 iOS 首帧延迟"
    },
    "dedupe_key": "vibelive-player-stability"
  }
  ```

  The renderer may choose wording, but it may not change
  `notify`, swap evidence ids, add facts not present in `key_facts`,
  or rename the project/topic.
- Renderer only turns the investigator's brief into Feishu markdown.
  It must not re-decide project/topic matching or invent a different
  project than the evidence supports.
- GitHub + Gitea webhooks can be added as more event sources once the
  investigation path is in place; they should feed the same
  `events -> investigation_jobs` path as turns.
- Notification cards can then add interaction buttons:
  - "Mute 1h" / "Don't tell me about this kind"
  - "Open full timeline"

**Not the 1.0c goal**

- No separate rule/score engine in front of the decider. If we need
  cheap indexing later, add it after observing real cost and recall
  problems. The first 1.0c cut keeps one LLM gatekeeper plus one PMO
  investigator, not a stack of policy layers.
- No automatic write actions. The investigator may recommend or
  explain, but it still only notifies.

**1.0a → 1.0c migration path**

- Keep `events`, `subscriptions`, `notifications`, delivery, and the
  Feishu follow-up linkage.
- Add `investigation_jobs`.
- Add nullable `investigation_job_id` columns to `decision_logs` and
  `notifications`. Existing 1.0a rows keep `NULL` and remain readable
  history.
- Change the decider loop from "write notification pending" to "open
  / append investigation job". 1.0a's direct notification path is
  disabled once the investigator loop is deployed.
- Write two audit records when a job runs:
  - decider gate log: why the event opened / appended to a job
  - investigator decision log: final `notify/suppress`, evidence, and
    brief
- Delivery still claims `notifications.pending`; only the writer of
  pending rows changes from the single-event decider to the
  investigator.

**Validation script**

1. Create a subscription: "监控 vibelive 播放器方案进展".
2. Insert 5 vibelive turns in one 30-minute window: 3 about player /
   buffer / stutter, 2 unrelated housekeeping turns.
3. Insert 1 oneship turn mentioning unrelated UI work.
4. Wait for the job flush.
5. Assert exactly one investigation job closes as `notified`.
6. Assert the resulting notification references the 3 related
   vibelive evidence event ids, mentions "播放器" or "播放链路", and
   does not include the 2 unrelated vibelive turns or the oneship
   event as evidence.
7. Reply under the notification and verify the bot uses the
   investigation brief / evidence context.

**Estimate**: ~1 day for the first rewrite over the existing 1.0a
delivery/renderer tables; more if GitHub/Gitea sources are included
in the same cut.

### 2.0 — From notification system to PMO presence

2.0 is not a feature list. It is a coherent expansion of 1.0c
along **three orthogonal axes**, each extending a different
piece of the
`events × subscriptions → (notify? topic? to whom?) → Feishu`
function. Detail in
[2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md).

The three axes:

#### 2.0a — External event sources (Axis 1: events broaden)

Today `events.source = 'turn'` is the only path. Real teams
collaborate through GitHub / Gitea / Linear / Feishu Calendar —
most "watchable" things happen there, not in turns alone. 2.0a
adds webhook ingestion (GitHub + Gitea first), an
`external_identities` mapping, repo↔project mapping so 1.0c's
project lockout still works, and renderer enrichment so
investigator briefs can read PR diffs / spec / plan files.

Unlocks: "vibelive merge → send spec+plan to albert", "PR review
pings", "release tag → changelog to project chat", etc.

Reuses 1.0c gatekeeper / investigator / renderer / delivery
unchanged — just a new event source feeding the same pipeline.

**Estimate**: 5-8 days. Lowest risk per unlocked value among
the three axes.

#### 2.0b — Output routing (Axis 2: delivery broaden)

1.0c subscriptions couple "who set this up" with "where the
notification lands." 2.0b decomposes this into two independent
concepts:

- **owner**: who can edit/disable the rule (user OR chat — same
  as 1.0a/b)
- **target**: where delivery actually lands
  (`user_dm | chat | mention_in_chat`, optionally with a specific
  user to @ inside a chat)

Plus permission gates so a third party can't weaponise the bot to
DM-spam someone. Two paths:
- **User-owned rules** with cross-user DM target → require
  explicit `target_consents` row (granted via the bot-mediated
  consent prompt; reply detection is parent-message-id bound,
  not pattern-matched).
- **Chat-owned rules** with cross-user DM target → both parties
  must currently be members of the rule's anchor chat; verified
  at creation via Feishu's `chats/{id}/members` API and rechecked
  at delivery time (6h cache).

Earlier drafts allowed "shared chat anywhere" as implicit
consent for user-owned rules. Removed in 2.0b round-3 review:
Feishu's API doesn't reliably enumerate "all chats two users
share," so that path was unimplementable cleanly.

Unlocks: chat-mediated rule creation, mention-in-chat as a
delivery target, third-party rules where owner ≠ target.

**Estimate**: 4-6 days. Risk is ACL design — get permissions
right or the bot becomes a stalker channel.

#### 2.0c — Judgment-driven proactive (Axis 3: trigger broaden)

1.0c investigators are rule-bound: they only run when a
subscription matched a candidate event. A real PMO doesn't wait
for a rule — they look at team state and decide on their own when
to speak, in which room, to whom. 2.0c adds an **observer** loop
that runs every ~30 minutes, reads a curated team state snapshot,
and emits zero or many "speech acts" without any pre-existing
subscription. Each speech act becomes a synthetic
investigation_job through the existing pipeline.

This is the qualitative jump from "notification system" to "PMO
presence" — but it's also product-fatal if done wrong. A single
overeager observer mis-DM ("albert seems to be slacking" sent to
the wrong person) can erase trust faster than missed
notifications.

The only survival mechanism is **user-controlled silence**: hard
daily cap, "this was useless" feedback per delivered speech act,
per-user per-category confidence learned from feedback.

**Estimate**: 10-15 days. By far the riskiest. Do not build
first.

#### Coupling and sequence

The three axes are mostly orthogonal but two coupling points
matter:

- **Axis 1 strengthens Axis 3**. The observer is much more useful
  when it can read GitHub state, not just turns. Without external
  sources, the observer is half-blind.
- **Axis 2 enables Axis 3 to land well**. Once observer can speak
  unprompted, "where to deliver" becomes critical — observer
  deciding "tell albert in #vibelive when…" needs the routing
  flexibility of Axis 2. Without Axis 2, observer-driven speech
  can only land in the asker's DM, which is the wrong room half
  the time.

Natural sequence: **2.0a → 2.0b → 2.0c**. Skipping ahead to 2.0c
is risky. Doing 2.0a first is the safest first step.

We don't commit to any specific 2.0 work until 1.0a/b/c usage
shows what matters. The roadmap is a memory aid, not a contract.
But if work must start before that signal arrives, 2.0a is the
default pick — failure mode is bounded (webhook arrives, no
subscription matches, suppressed silently, same as a turn that
matches nothing).

#### Out of scope across all 2.0 axes

- Voice / phone notifications — Feishu only
- Multi-org pmo_agent — separate product
- Replacing existing tools (Linear, GitHub, calendars) — bot is
  glue, not source of truth
- Native code execution / PR creation by bot
- Pricing / billing / usage caps — until paid users
- Bot-initiated DMs to users without bound feishu_links — would
  fail at delivery anyway

---

## 3. Architecture invariants

These hold across all stages. Changing any of them is a major
re-design and must be reconsidered explicitly.

1. **One bot, one Feishu app**. Push and pull share the same
   identity, the same conversation history.
2. **Event identity is append-only; payload is a versioned
   projection.** Every signal (turn, push, future) creates exactly
   one `events` row, identified by `(source, source_id)`. That row
   never disappears or splits. The `payload` column is a denormalised
   projection of the source — it is allowed to be rewritten when the
   source updates (e.g. `agent_summary` arrives 30s after the turn
   insert), and a `payload_version` integer tracks how many such
   rewrites have happened. The decider's per-(event, subscription)
   record stores which payload version it judged on, so a meaningful
   payload update is reconsidered without ever creating duplicate
   rows. Sources are pluggable; consumers don't know or care where
   an event came from.
3. **Subscriptions stay natural language at the product boundary**.
   `subscriptions.description` remains the source of truth. Future
   systems may index or summarise a subscription for routing, but
   those derived fields are hints, not the user's contract. Any
   investigation must still have access to the original text.
4. **Decision authority is explicit per stage**. In 1.0a, the decider
   returns `send/suppress` for one event. In 1.0c, the decider opens
   investigation jobs and the PMO investigator makes the final
   `notify/suppress` decision after reading context. The investigator
   must output a structured brief with evidence ids and key facts. We
   do not hide extra notification decisions inside the renderer.
5. **Agent work reuses the existing PMO tool surface**. Rendering and
   investigation are agent invocations with different system prompts;
   they use the same read tools (`get_recent_turns`,
   `get_project_overview`, future `fetch_pr_details`) instead of
   building a parallel data access layer.
6. **Subscriptions can be owned by users or chats** from day one.
   The data model supports both even though 1.0a's UX is mostly
   user-centric — adding chat-level later is a UX change, not a
   schema migration.
7. **Decision logs are first-class**. Every judge call writes its
   inputs, output, and reason. This is what makes "why didn't you
   tell me about X" answerable, and it's what makes prompt iteration
   data-driven.

---

## 4. Risks and how each stage tests them

| Risk | Tested in | Failure mode |
|------|-----------|--------------|
| LLM judge is too noisy / too quiet | 1.0a → 1.0c | Move final notification decision from single-event decider to PMO investigator |
| Notification spam from rebase storms | 1.0a (5min dedup) → 1.0c (investigation jobs) | Users disable subscriptions |
| Decision latency too high | 1.0a (30s loop) | Acceptable for MVP; revisit if >2min in real use |
| Cost per event scales badly | 1.0a (logged), 1.0c (job suppression) | Open one investigation job per plausible thread, not one notification per event |
| Users can't find their subscriptions | 1.0a (chat list) → 1.0b (web list) | Make UI primary if chat-only is friction |
| Group notifications get noisy in busy channels | 1.0c | PMO investigator suppresses low-value jobs; later downgrade to digest |

---

## 5. Out of scope across all stages (until further notice)

- **Write actions in response to events**: the proactive bot tells
  you about things; it doesn't take action. Auto-merging PRs,
  auto-commenting, auto-scheduling — none of these. (Bot already has
  write tools for user-initiated actions; those stay user-initiated.)
- **Voice / phone notifications**: Feishu only.
- **Subscriptions for non-pmo users**: only people with a profile
  + bound Feishu account can subscribe.
- **Cross-org pmo_agent instances**: one Supabase backend, one
  Feishu app. Multi-tenant is a separate product.

---

## 6. Glossary

- **Subscription**: a (scope_kind, scope_id, description, enabled)
  row. The description is the user's natural language and remains
  the source of truth even if future stages add derived indexes.
- **Event**: a row representing one happened thing (turn / push /
  …). Identified by `(source, source_id)` — that identity never
  changes. Carries a `payload` projection of the source data and
  a `payload_version` integer; both are allowed to mutate when the
  source updates.
- **Investigation job**: a 1.0c row representing "the PMO should look
  into these seed events for this subscription". It is not a
  notification; it may close as suppressed.
- **Notification**: a delivered-or-deliverable row created only after
  a decider/investigator decision says the user should be told. May
  or may not have been delivered, depending on `status`.
- **Decision log**: the LLM judge's full input/output for one
  (event, subscription) pair. Always written, even when nothing is
  delivered.
- **Judge / Decider**: in 1.0a, the LLM call that returns
  `{send: bool, reason, …}` for one (event, subscription) pair. In
  1.0c, the lightweight LLM gatekeeper that decides whether to open
  an investigation job.
- **PMO investigator**: the 1.0c agent invocation that reads enough
  project/session/topic context for an investigation job and makes
  the final `notify/suppress` decision.
- **Renderer**: the LLM call that, for one approved notification
  brief, produces the human-facing text.
