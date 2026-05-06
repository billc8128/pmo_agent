# Proactive PMO Agent 2.0 — Strategy

- **Status**: Strategic exploration, not committed
- **Date**: 2026-05-06
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Predecessors**: 1.0a (skeleton) + 1.0b (public rules panel) +
  1.0c (gatekeeper-investigator-renderer) all in flight

This document is the **product-level strategy** for 2.0. Unlike the
1.0 series specs which are implementation contracts, this one
sketches the destination and what we'd build next, **without
committing**. The sequencing question — what to build first — is
deliberately answered by "wait for 1.0a/b/c usage data."

---

## 1. Where 1.0 leaves us

After 1.0a-c, the bot can:

- Receive an arbitrary natural-language subscription
- Filter events deterministically by project (no more wrong-project
  firing)
- Aggregate a "thread" of related events into one investigation
- Read deeper context (turns, project overviews, history) before
  deciding to notify
- Render a faithful Feishu message with the cited evidence
- Manage rules from chat OR public web panel

What 1.0 explicitly **doesn't** do:

- Take action — only tell the user
- Coordinate between people — only notify individuals
- Schedule anything — only react to events
- Cross external systems — only `turns` from pmo_agent itself
- Maintain project state — no deadlines, no roadmap, no goals

These five gaps are the design space for 2.0.

---

## 2. Five candidate features, ranked by leverage

These are NOT in build order. Build order depends on data from
1.0a/b/c usage — see §3.

### 2.A — Scheduled briefs (周报 / 日报)

**What**: cron-triggered, no event needed. "Every Monday 09:00,
private-DM bcc with last-week summary of the team." "Every weekday
17:00, post a project-X day-end digest to the project chat."

**User stories**:
- "我想每周一早上看上周大家都做了啥"
- "项目群里每天五点总结一下今天的关键进展"
- "我在做的项目每周给我个客观的进度评估"

**Why high leverage**: doesn't need new architecture. Reuses 1.0c's
investigator pipeline with a cron trigger instead of an event
trigger. Output goes through the same renderer. Probably 60% of
real "PMO" value comes from this single feature.

**New parts needed**:
- `scheduled_briefs` table: subscription_id (user OR chat) +
  cron_expression + brief_type ('week_summary'|'day_summary'|...)
  + last_run_at
- `scheduled_brief_runs` table: trigger row with seed events
  collected by SQL query (e.g., "all events for albert in last 7
  days") instead of by gatekeeper
- A new `scheduler_loop.py` background task: every 5min, finds
  scheduled briefs whose cron is due, builds a synthetic
  investigation_job with seed_events from a time-window query,
  hands off to existing investigator → renderer → delivery
- New investigator prompt mode for "summarise N days of activity"
  rather than "is this thread worth a notification"

**Estimate**: 5-7 days. Most of this is the cron infra +
seed-collection query, not new LLM work.

**Risk**: scheduled briefs are by definition NOT triggered by a
specific event, so the "evidence_event_ids" structure in 1.0c
brief carries dozens of events. Renderer would need to handle
larger bundles. Might split into "highlight 5-10 events" sub-step.

### 2.B — Cross-person collision detection (同事撞车)

**What**: when two profiles work in the same project_root within
a short window, bot offers a quick "want me to introduce you?" or
"want a sync?". Special case of "stall detection inverted" —
notice multiple people converging.

**User stories**:
- "如果有人在改我正在改的代码告诉我"
- "我要做的事如果别人已经在做，提醒一下避免重复"

**Why interesting**: this is the single feature that's about
**team coordination** not personal feed. It needs to read
"current activity" across people, not just react to one person's
event.

**New parts needed**:
- New event-correlation pass: when ingesting a turn, check
  `events` for OTHER users active in the same project_root in
  last N hours; if found AND not yet correlated, open a
  `collision_jobs` row
- New event source `collision` (alongside `turn`, `github`)
- Investigator prompt that frames "two people, one project" as
  the question
- Subscriptions like "如果有人撞我代码告诉我" route to
  collision-source events specifically

**Estimate**: 4-6 days.

**Risk**: signal-to-noise. Two people touching `vibelive` for
totally unrelated reasons happens daily. Threshold tuning matters
a lot. Probably needs file-level overlap (1.0c's events don't
record changed files yet — needs daemon work too).

### 2.C — GitHub / Gitea webhook ingestion

**What**: webhook-driven event source. PR opened, commit pushed,
release tagged → events row → flows through existing 1.0c
gatekeeper → investigator → renderer.

**User stories**:
- "vibelive 有 PR 合并提示我"
- "我的项目有 release 了告诉我"
- "watch albert 的提交"

**Why obvious but lower**: 1.0c already supports multiple event
sources via the `events.source` column. Adding GitHub is "wire it
up" not "design new architecture." It's only **lower** because
turns already capture most of the signal — if albert is working,
a turn fires before a commit lands. GitHub adds value for
**non-pmo-user collaborators** (people on the team without daemon
running) and for **non-coding events** (release tags, issue
comments).

**New parts needed**:
- `bot/web/feishu/webhook.py` → add `bot/webhooks/github.py`
  + `gitea.py` route handlers
- Webhook signature verification (per source — GitHub HMAC,
  Gitea HMAC, signature secret env var per source)
- Map webhook payload to `events.payload` shape — agent_summary
  needs to be filled by a small LLM call ("summarize this PR")
  OR by extracting the title/description verbatim
- Possibly a `external_identities` table mapping
  `(github_user → profile_id)` so the gatekeeper can apply
  subscriptions like "albert 的 PR" correctly

**Estimate**: 3-5 days. Most work is in identity mapping.

**Risk**: webhook reliability. Need retry / dedup. GitHub will
re-deliver on receiver errors → events table needs (source,
source_id) unique handling that already exists in 1.0a's design.

### 2.D — Stall / blocker detection (项目卡住了)

**What**: bot proactively warns when a "watched" project goes
quiet, or when a subscription's expected cadence drops.

**User stories**:
- "C 项目 5 天没活动了，bcc 你说月底要 ship，要查查吗?"
- "albert 这周才跑了 5 个 turn，平时是 30+，他还好吗?"
- "我说要在 X 之前完成的事，截止前 24h 提醒我"

**Why exciting but expensive**: this is the closest 2.0 feature
to "real PMO behaviour." But it requires structured project
state that pmo_agent doesn't have today: deadlines, milestones,
ownership.

**New parts needed**:
- `projects` table: project_root + display_name + owner +
  goals (jsonb) + cadence_baseline + ship_target. Populated
  manually or via "tell bot the goal" chat command.
- A separate `stall_check` cron loop running daily: for each
  project, computes "expected vs actual" activity, opens an
  investigation if the gap crosses threshold
- New investigator prompt: "is this stall meaningful or just a
  weekend?"

**Estimate**: 8-12 days. The data-modeling work for `projects` is
the bulk; the LLM piece is straightforward.

**Risk**: project metadata management is its own UX. Ad-hoc Slack
"track this" approach OR explicit roadmap import OR "bot watches
your stated goals" — this is a product question we can't answer
without trying. **Probably the right path is to ship 2.A and
2.E first, then bolt this on once users tell us what kind of
"goal tracking" they want.**

### 2.E — PR / Linear / Meeting linking (turn 自动关联外部资源)

**What**: when a turn's payload mentions "PR #123" / "issue
ABC-456" / "feishu meeting xxx", bot auto-fetches that resource
and includes it in the brief.

**User stories**:
- "albert 提到 PR 1234 的时候，brief 里直接给我 PR diff 摘要"
- "回复了 issue 后通知我新评论"
- "约了会的时候提醒我提前 10 分钟"

**Why nice but expensive**: this isn't a new product feature, it's
**better content** in existing notifications. Each external
integration is its own auth/rate-limit/parsing problem.

**New parts needed**:
- `external_link_resolvers` registry: per-pattern resolver
  (regex on user_message + agent_response → fetch handler).
  GitHub PR / GitHub issue / Linear ticket / Feishu doc / Feishu
  calendar event handler each ~1 day's work.
- Cache layer for external API responses (`external_resources`
  table)
- Investigator prompt extended to consume linked resources

**Estimate**: 2-4 days per resolver. Probably ship one (GitHub PR)
first, see if anyone uses it.

**Risk**: privacy. Cached external content gets stored in
Supabase next to public turn data. Needs a clear "this resource
was readable by the bot but not by other users" boundary.

---

## 3. Sequencing — what to build first depends on observation

Don't pick yet. Wait for 1.0a/b/c usage data and let it answer:

| Observation | Likely choice |
|-------------|---------------|
| Users keep asking for "weekly summaries" | 2.A (scheduled briefs) |
| Users complain that 1.0c misses cross-team work | 2.B (collision detection) |
| Most subscriptions are about non-pmo-user activity | 2.C (GitHub webhooks) |
| Users state goals/deadlines and expect tracking | 2.D (stall detection) |
| Notifications feel underwritten without external context | 2.E (link resolvers) |

The instrumentation we already have to read these signals:
- `decision_logs` table — what gatekeeper saw / decided
- `investigation_jobs` table — what investigator concluded
- `notifications` table with `feishu_msg_id` / sent_at
- Conversation tools (the bot itself) — users will say what they
  want directly

After ~2 weeks of 1.0a/b/c in real use, run a small analysis
script: top-N missing notification topics, top-N suppressed
reasons, top-N user follow-up questions. Pick the 2.0 feature
that addresses the dominant signal.

**Default if no clear signal**: ship 2.A (scheduled briefs)
first. It's the highest-confidence "users will use this"
feature, infrastructure cost is bounded, and it doesn't touch
the 1.0c core. Risk is lowest.

---

## 4. Architectural invariants to preserve in 2.0

1. **Single Feishu app, single bot**. Group subscriptions are NOT
   a different bot.
2. **Events stay append-only**. Whatever 2.0 features add must
   produce `events` rows that look like `turn` events from the
   pipeline's perspective.
3. **Investigator owns notify/suppress**. Scheduled briefs use
   the same investigator; they just have a different trigger.
4. **No write actions in response to events**. 2.0 may extend
   what bot can DO when user asks (chat-driven), but proactive
   path stays read-only.
5. **All LLM calls have a budget**. New features must declare
   their token budget upfront. 2.A in particular will be heavy
   per-call but rare.
6. **Decision authority is explicit**. New "actors" (collision
   detector, stall detector) must define what they decide and
   what defers to investigator/renderer downstream.

---

## 5. Anti-patterns to avoid

- **Don't add a "rules engine"**. Specifically: when 2.A's
  scheduled briefs need "skip if user is on vacation," resist
  the urge to build a vacation table. Instead, let users say
  "本周不要发周报" via chat — same natural-language subscription
  mechanism.
- **Don't multiply event sources prematurely**. Adding GitHub +
  Gitea + Linear + Slack creates coupling that's hard to back
  out. Add ONE external source first, see how often investigator
  needs cross-source dedup, decide based on that.
- **Don't pre-build "team" abstractions**. The current
  subscription scope (user OR chat) is enough for two-team
  workflows by convention. A real `teams` table is needed only
  when org structure has hard boundaries (which 1.0a/b/c users
  don't have).
- **Don't auto-act**. Even if "auto-schedule a code review when
  PR opens" is tempting, gate it behind the user explicitly
  asking the bot to do it on every PR. Auto-action erodes user
  trust faster than missed notifications.

---

## 6. Out of scope (still, possibly forever)

- Voice / phone notifications
- Mobile push (Feishu desktop only)
- Multi-org / multi-tenant pmo_agent — separate product
- Replacing existing tools (Linear, GitHub, calendars) — bot is
  glue, not source of truth
- Native code execution / PR creation by bot
- Pricing / billing / usage caps — until we have paid users

---

## 7. Concrete next step (if you want to commit to ANY 2.0 work now)

The lowest-risk way to start 2.0 work without depending on data:

1. **Apply 1.0c sandbox + production**, watch it for 1 week
2. **Run an analysis script** on the resulting decision_logs and
   notifications, identifying top patterns
3. **Pick 2.A or 2.C** based on what you see — both reuse 1.0c
   infra without extending architecture
4. Write a 2.A or 2.C **mini-spec** at the same level of detail
   as the 1.0c spec
5. Iterate with codex review until ready to implement
6. Implement, deploy, observe again

If 1.0a/b/c watching is impatient: **just start 2.A**. Scheduled
briefs are the safest 2.0 feature to build speculatively because
they reuse everything below them — failure mode is "users don't
read the weekly digest" rather than "production is broken."
