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
                                      + interactions      + planning
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

### 1.0b — Web UI panel

**Goal**: Visualise what 1.0a built. No new logic, just surfaces.

✅ added
- `/me` gets a "Notifications" section
  - Active subscriptions list with enable/disable + delete
  - Add subscription form (free text)
  - Link to notification history
- `/me/notifications` shows the last 50 notifications, grouped by
  status (sent / suppressed / failed) with the decision reason
- `/me` gets a "Notification preferences" mini-card with
  - Quiet hours range
  - Daily cap
  - Timezone (auto-detected from feishu_links, editable)
- `/chats/<chat_id>/subscriptions` (only visible to chat members) —
  manage group subscriptions in web

❌ still out
- GitHub webhook (1.0c)
- Card buttons (1.0c)
- Per-team notifications (2.0)

**Estimate**: ~3h.

### 1.0c — Productionised

**Goal**: Close the obvious gaps from real-world use.

✅ added
- GitHub + Gitea webhooks ingested as events
- Notification cards get interactive buttons:
  - "Mute 1h" / "Don't tell me about this kind"
  - "Open full timeline"
- Batching: events arriving within 30s on the same subject merge
  into one notification
- Daily cap is soft — overflow queues until next quiet-hours-end as
  a "what you missed" digest
- Notification cards visually flag the source event (turn vs push)

**Estimate**: ~4h.

### 2.0 — Team-level coordinator

This is where the bot crosses over from "personal feed" to actual
PMO behaviour. Each item below is roughly a week's work; we'd pick
the highest-value first based on real 1.0a/b/c usage.

- **Group subscriptions get richer**: scoped to specific channels
  with team-level rituals (weekly digest, sprint kickoff brief)
- **Scheduled briefs**: cron-triggered weekly / daily summaries of
  who-did-what
- **Cross-person awareness**: detect "two people working in the same
  file" and suggest a sync
- **Stall / blocker detection**: "C 项目 5 天没活动，bcc 你说月底要
  ship，要查查吗?" — needs project metadata (deadlines, owners)
- **PR / Linear / meeting linking**: turns mentioning PR #123 auto
  fetch; notifications include PR diff summary; bot can schedule
  code review meetings
- **Multi-team / org**: subscription scope expands to teams, with
  bridges between them

We don't commit to any of this until 1.0a/b/c usage shows what
matters. The roadmap is a memory aid, not a contract.

---

## 3. Architecture invariants

These hold across all stages. Changing any of them is a major
re-design and must be reconsidered explicitly.

1. **One bot, one Feishu app**. Push and pull share the same
   identity, the same conversation history.
2. **Events are append-only**. Every signal (turn, push, future)
   becomes a row in `events`. Sources are pluggable; consumers don't
   know or care where an event came from.
3. **Subscriptions are natural language**. We store the user's
   verbatim text in `subscriptions.description`; we never parse it
   into structured rules. Interpretation is the LLM's job, every
   time. This is what lets a single mechanism handle "vibelive 进展",
   "凌晨别打扰", "项目 C 不要", and arbitrary future preferences
   without schema migrations.
4. **The LLM judge is the only place rules live**. Quiet hours,
   dedup windows, daily caps, exclusion preferences — all expressed
   to the judge as context, all enforced via the judge's `send`
   verdict. We do **not** build a separate rules engine.
5. **Generation reuses the existing agent runner**. Writing a
   notification is an agent invocation with a different system
   prompt; it has access to the same tools (`get_recent_turns`,
   `get_project_overview`, future `fetch_pr_details`). We do not
   fork the agent loop.
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
| LLM judge is too noisy / too quiet | 1.0a | Re-tune prompt; add structured pre-filter only if prompt iteration plateaus |
| Notification spam from rebase storms | 1.0a (5min dedup) → 1.0c (batching) | Users disable subscriptions |
| Decision latency too high | 1.0a (30s loop) | Acceptable for MVP; revisit if >2min in real use |
| Cost per event scales badly | 1.0a (logged), 1.0c (pre-filter) | Add coarse rule-based pre-filter before LLM judge |
| Users can't find their subscriptions | 1.0a (chat list) → 1.0b (web list) | Make UI primary if chat-only is friction |
| Group notifications get noisy in busy channels | 1.0c | Per-chat rate limits; downgrade to digest |

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
  row. The description is user's natural language; nothing else
  parses it.
- **Event**: an append-only row representing one happened thing
  (turn / push / …). Has a `source` and `payload`.
- **Notification**: a (event_id, subscription_id, status,
  rendered_text, …) row representing one decision. May or may not
  have been delivered, depending on `status`.
- **Decision log**: the LLM judge's full input/output for one
  (event, subscription) pair. Always written, even when nothing is
  delivered.
- **Judge / Decider**: the LLM call that, for one (event,
  subscription) pair, returns `{send: bool, reason, …}`.
- **Renderer**: the LLM call that, for one approved (event,
  subscription), produces the human-facing text.
