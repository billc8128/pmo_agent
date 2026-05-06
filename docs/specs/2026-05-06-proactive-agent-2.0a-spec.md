# Proactive PMO Agent 2.0a — External Event Sources

- **Status**: Draft for implementation
- **Date**: 2026-05-06
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Strategy**: [2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md)
- **Plan**: [2.0a Plan](2026-05-06-proactive-agent-2.0a-plan.md)
- **Predecessors**: 1.0a + 1.0b + 1.0c — all infrastructure carries
  forward unchanged. 2.0a only adds new event sources upstream.

This is the **source of truth** for 2.0a's data model, webhook
contracts, and identity mapping. Implementation choices that
diverge update this file.

---

## 1. Why 2.0a

1.0c's pipeline takes events and produces notifications. The
universe of events today is a single source: `turn`. But user
subscriptions like the following don't have anything to subscribe
to:

> "每次 vibelive 项目的 PR 合并都把 spec 和 plan 发给 albert，让他
> 确认技术方案"
>
> "我提的 PR 收到 review 通知我"
>
> "release 标签打了之后给项目群发 changelog"

These describe events that happen in **GitHub / Gitea**, not in
turns. A subscription containing the word "merge" today fires
opportunistically when a turn happens to mention the word —
NOT when an actual merge happens.

2.0a adds GitHub and Gitea webhook ingestion as additional event
sources alongside turns. Everything downstream — gatekeeper,
investigator, renderer, delivery — stays unchanged because it
operates on `events.payload` opaquely.

---

## 2. Scope

In scope:

- Webhook routes for GitHub and Gitea
- HMAC signature verification, idempotency, retry safety
- Mapping incoming webhook payloads to `events` rows
- `external_identities` table mapping (provider, external_login)
  → profile_id
- `external_repos` table mapping (provider, repo full_name)
  → project_root
- Renderer enrichment: when a brief's evidence references a
  GitHub PR / commit, the renderer can read PR diff / mentioned
  files via a small fetch helper
- Self-claim UX in chat ("我的 GitHub 是 billc8128") and on the
  web rules panel
- Decider's project lockout (1.0c §4.1) keeps working — webhook
  events feed the same `project_root` field used by lockout

Out of scope (explicitly):

- Linear, Jira, Slack, Feishu Calendar, Feishu Doc — separate
  future axes
- OAuth-based identity claim — 2.0a uses self-claim only; OAuth
  is 2.0a-followup if needed
- Bot writing back to GitHub (commenting on PR, adding labels,
  etc.) — 2.0 invariant: no auto-actions
- Repo discovery / auto-mapping without explicit configuration —
  user tells the bot which repo maps to which project
- Replaying historical PRs / events that landed before 2.0a was
  installed
- Unifying turn-source and webhook-source into one
  cross-source thread — 1.0c's investigation_jobs aggregate
  per-subscription, which already handles this naturally; we
  don't need a new "merge" abstraction layer

---

## 3. Data model

### 3.1 `external_identities` — map external user login to profile

```sql
create table public.external_identities (
    id           uuid primary key default gen_random_uuid(),
    profile_id   uuid not null references public.profiles(id) on delete cascade,
    provider     text not null check (provider in ('github', 'gitea')),
    external_login text not null,             -- the username on the
                                              -- external system, lowercased
    external_id  text,                        -- the numeric user id
                                              -- on the external system
                                              -- (preferred when
                                              -- available; logins can
                                              -- change but ids don't)
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    constraint extid_unique unique (provider, external_login)
);

create index extid_profile_idx
    on public.external_identities (profile_id);
```

Why both `external_login` and `external_id`:

- GitHub allows users to rename their account. The login changes;
  the numeric id doesn't.
- Webhook payloads include both — we match on id when present,
  fall back to login otherwise.
- For self-claim (initial UX), the user types their login.
  Background reconciliation later resolves to id when we make
  authenticated API calls.

A profile can have multiple identities (one per provider).
External logins are unique per provider — two profiles can't
both claim "billc8128" on github.

### 3.2 `external_repos` — map external repo to project_root

```sql
create table public.external_repos (
    id              uuid primary key default gen_random_uuid(),
    provider        text not null check (provider in ('github', 'gitea')),
    repo_full_name  text not null,    -- "billc8128/vibelive"
    project_root    text not null,    -- "/Users/.../vibelive"
                                      -- (matches turns.project_root
                                      --  format so 1.0c lockout works)
    created_by      uuid references public.profiles(id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint repo_unique unique (provider, repo_full_name)
);

create index repos_project_root_idx
    on public.external_repos (project_root);
```

A repo maps to exactly one project_root. A project_root may have
multiple repos (e.g., monorepo with subprojects, or vibelive
having both `vibelive` and `vibelive-mobile` repos pointing at
the same project).

### 3.3 `events` schema unchanged, new source values

Existing schema from 1.0a §2.2:

```sql
events (
    id, source, source_id, user_id, project_root,
    occurred_at, ingested_at, processed_at, processed_version,
    payload_version, payload, ...
)
unique (source, source_id)
```

2.0a adds two new `source` values: `github` and `gitea`.

`source_id` shape per provider:

| Provider | source_id format | Example |
|----------|------------------|---------|
| github   | `{event_type}:{delivery_id}` | `pull_request:abc123-uuid` |
| gitea    | `{event_type}:{delivery_id}` | `pull_request:def456-uuid` |

Both providers send a unique delivery uuid in webhook headers
(`X-GitHub-Delivery` / `X-Gitea-Delivery`); we use that as the
source_id suffix to make idempotency trivial. If a webhook
re-delivers the same delivery uuid, the existing
`(source, source_id)` unique constraint dedupes.

`payload` jsonb contents per webhook event type — see §4.

`user_id` is set when we can map the webhook's actor (PR author,
pusher, commenter) to a profile via `external_identities`. NULL
otherwise (still ingested, just unmapped — investigator can read
the external_login from payload directly).

`project_root` is set when the webhook's repo maps to a known
`external_repos.project_root`. NULL otherwise (still ingested,
but the project lockout in 1.0c won't filter it — it falls
through to the gatekeeper LLM as a "no project context" event).

### 3.4 No changes to `subscriptions`, `notifications`,
   `investigation_jobs`, `decision_logs`

These all read `events.payload` opaquely. New event sources slot
in without schema changes.

The 1.0c project lockout (`subscriptions.metadata.matched_projects`)
works for webhook events because we populate `events.project_root`
from the repo mapping. Subscription "vibelive merge 告诉我" with
`matched_projects=["vibelive"]` correctly hard-skips a github
event whose `project_root='/Users/.../oneship'` (assuming `oneship`
repo is mapped) just like it does for turn events.

---

## 4. Webhook ingestion contracts

### 4.1 Routes

```
POST /webhooks/github    — GitHub-hosted repos
POST /webhooks/gitea     — self-hosted Gitea repos
```

Both routes live in `bot/web/feishu/webhook.py` alongside the
existing Feishu webhook (or a new sibling module). They are
**bot-side** routes, not web app routes — they need
`sb_admin()` to write events with service-role permissions and
shouldn't go through Vercel's edge.

### 4.2 Signature verification

Each route reads a per-provider HMAC secret from environment:

- `GITHUB_WEBHOOK_SECRET` — used to verify
  `X-Hub-Signature-256` header (HMAC-SHA256 of raw body)
- `GITEA_WEBHOOK_SECRET` — used to verify `X-Gitea-Signature`
  header (HMAC-SHA256 of raw body)

Mismatched signature → 401 with no body. Missing secret →
500 with log line; the route fails closed.

### 4.3 Event types we ingest in 2.0a

For each event type we extract a stable shape into
`events.payload`. The original webhook body is preserved at
`payload.raw` so investigators / renderers can dig deeper.

#### `pull_request` (action=opened, closed-merged, synchronize)

```jsonc
{
  "event_type": "pull_request",
  "action": "opened" | "merged" | "synchronize",
  "pr": {
    "number": 1234,
    "title": "...",
    "body": "...",
    "html_url": "https://github.com/owner/repo/pull/1234",
    "diff_url": "https://github.com/owner/repo/pull/1234.diff",
    "base_branch": "main",
    "head_branch": "feature/x",
    "merged": true,
    "merged_at": "2026-05-06T...",
    "files_changed_count": 7,
    "additions": 142,
    "deletions": 38
  },
  "repo": {
    "full_name": "owner/repo",
    "default_branch": "main"
  },
  "actor": {
    "login": "billc8128",
    "id": "123456",                  // numeric, when present
    "profile_id": "uuid-or-null"     // resolved during ingest
  },
  "raw": { /* original webhook body */ }
}
```

#### `push` (commits to a branch)

```jsonc
{
  "event_type": "push",
  "ref": "refs/heads/main",
  "before": "abc...",
  "after": "def...",
  "commits_count": 3,
  "commit_summaries": [
    "Add foo", "Fix bar", "..."
  ],   // truncated to ~20 commits
  "repo": { ... },
  "actor": { ... },
  "raw": { ... }
}
```

#### `release` (action=published)

```jsonc
{
  "event_type": "release",
  "action": "published",
  "release": {
    "tag_name": "v1.2.3",
    "name": "...",
    "body": "...",
    "html_url": "..."
  },
  "repo": { ... },
  "actor": { ... },
  "raw": { ... }
}
```

#### `issue_comment` (when comment mentions a known profile)

```jsonc
{
  "event_type": "issue_comment",
  "action": "created",
  "comment": { "body": "...", "html_url": "..." },
  "issue": { "number": 567, "title": "..." },
  "repo": { ... },
  "actor": { ... },
  "mentioned_profile_ids": ["uuid", ...],   // resolved during
                                            // ingest from comment
                                            // body @-mentions
  "raw": { ... }
}
```

We deliberately **don't** ingest `pull_request_review`,
`check_run`, `workflow_run`, `deployment_status`, etc. in
2.0a — they're available in raw payload if investigator wants to
fall back, but they don't get their own typed shape until usage
data shows demand.

### 4.4 What `events.occurred_at` is set to

For each event type, prefer the most user-facing time:

| event_type | occurred_at source |
|-----------|---------------------|
| pull_request action=opened | pr.created_at |
| pull_request action=merged | pr.merged_at |
| pull_request action=synchronize | most recent commit time |
| push | the receive time (`now()` is fine; webhook arrives ~real-time) |
| release | release.published_at |
| issue_comment | comment.created_at |

`ingested_at` is always `now()` — used by the 1.0c forward-only
filter.

### 4.5 What `events.user_id` is set to

The actor (PR author, pusher, commenter, releaser) lookup against
`external_identities`:

```sql
select profile_id from external_identities
 where provider = 'github'
   and (external_id = $actor_id or external_login = $actor_login)
 limit 1
```

If multiple matches (shouldn't happen given the unique constraint
but defensively), pick the one with `external_id` set.

If no match: `events.user_id = NULL`. The event is still ingested
and goes through the pipeline. Subscriptions that don't depend
on the actor (e.g. "vibelive merge 告诉我") still fire.
Subscriptions that depend on actor (e.g. "albert 的 PR") rely on
the gatekeeper LLM seeing `actor.login` in payload and reasoning
about it — coarser than profile_id-based, but works for most
real cases.

### 4.6 Project root mapping

```sql
select project_root from external_repos
 where provider = $provider
   and repo_full_name = lower($repo_full_name)
 limit 1
```

Match → `events.project_root = found`. No match →
`events.project_root = NULL`.

Implications for 1.0c project lockout (§4.1): when project_root
is NULL, the gatekeeper's
`last_segment(event.project_root) == ""` short-circuit returns
False, so events without a known project don't get hard-skipped.
They fall through to the LLM gatekeeper, which can read
`payload.repo.full_name` and reason normally.

---

## 5. Identity claim — chat tool + web UI

### 5.1 Self-claim via chat tool

New agent tool `link_external_identity`:

```python
@tool(
    "link_external_identity",
    "Link the asker's GitHub or Gitea login to their pmo_agent "
    "profile. Required for subscriptions that reference 'my PRs', "
    "'my commits', or '@<external_login>'. Without this link, "
    "external events from this user appear as anonymous to the "
    "decider. \n\n"
    "Use when the user says things like 'my GitHub is X', "
    "'我的 gitea 用户名是 Y', '把我和 github billc8128 连起来'.",
    {"provider": str, "external_login": str},
)
async def link_external_identity(args: dict) -> dict:
    ...
```

Validations:
- asker must be a bound pmo_agent user (has feishu_links row)
- provider in {'github', 'gitea'}
- external_login matches `^[a-zA-Z0-9-]{1,39}$` (GitHub login
  rules; gitea is similar)
- if `(provider, external_login)` already claimed by another
  profile, return error: "this login is already claimed by
  another user; if that's a mistake, contact bcc"

The corresponding `unlink_external_identity` tool exists for
removal.

### 5.2 Self-claim via web rules panel

The `/notifications/rules` page already lets users add
subscriptions. 2.0a adds an **Identity** section above the rules:

```
[Identity]

  GitHub:  billc8128  [unlink]
  Gitea:   (not linked)  [link]
```

Same backing table; same uniqueness constraint. Hooked into the
existing Supabase auth (the user is signed in via Google/Feishu
OAuth, so `auth.uid()` gives us the profile_id).

### 5.3 Repo mapping UX

In 2.0a's first cut, repo mapping is **manually configured by
an admin** (you, bcc) via SQL or a small admin script. The
`external_repos` table is global to the deployment — there's
just one project ↔ repo set across all users.

A user-facing UI for repo mapping is deferred. Reasons:
- Most teams have <20 repos to map; a one-time SQL insert is
  fine
- User self-mapping invites typos and conflicts ("two users both
  claim repo X belongs to different project_roots")
- We learn what the right UX is by seeing what people actually
  ask for

Bootstrap recipe documented in plan §5.

---

## 6. Renderer enrichment

When the investigator's brief contains `evidence_event_ids` that
reference webhook events with `payload.event_type IN
('pull_request', 'push', 'release')`, the renderer can pull
additional context to make the message useful:

### 6.1 PR diff / files reading

For `pull_request` evidence, renderer optionally calls a new
read-only tool:

```python
@tool(
    "fetch_pr_files",
    "Fetch the list of files changed in a GitHub or Gitea PR, "
    "optionally with content of specific files (e.g. spec / "
    "plan files). Returns up to 30 files with paths + first 200 "
    "chars of content. \n\n"
    "Use when the user's subscription mentions 'send the spec / "
    "plan to X' or when the brief's key_facts cite specific "
    "files. Costs an external API call — use sparingly.",
    {"event_id": int, "paths_filter": list[str] or None},
)
```

Implementation:
- Read the event row, extract `payload.repo.full_name` and
  `payload.pr.number`
- Hit the external GitHub / Gitea API (auth via
  `GITHUB_API_TOKEN` env var, optional Gitea token)
- Cache results in a new `external_resource_cache` table for 24h
  to avoid hammering external APIs

This tool is added to the renderer's tool subset. It's also
available to the investigator (which is the right place for
"read enough context" — but in practice we expect investigator
to pull this only for high-signal cases).

### 6.2 Cache schema

```sql
create table public.external_resource_cache (
    id            uuid primary key default gen_random_uuid(),
    provider      text not null,
    resource_kind text not null check (resource_kind in (
                      'pr_files', 'pr_diff', 'commit', 'release_notes')),
    resource_key  text not null,         -- "repo/pr_number" or
                                         -- "repo/sha"
    content       jsonb not null,
    fetched_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    constraint resource_unique unique (provider, resource_kind,
                                       resource_key)
);

create index resource_cache_expires_idx
    on public.external_resource_cache (expires_at);
```

Lookup: `(provider, resource_kind, resource_key)` cache miss →
fetch from external → write cache → return. Hit → return cached.
Expired (`expires_at < now()`) → treated as miss.

A daily reaper (or `delete from external_resource_cache where
expires_at < now() - interval '7 days'`) keeps the table small.

### 6.3 Renderer prompt extension

The 1.0c renderer prompt is extended with a small section for
events with external content:

```
如果 brief 提到 PR / commit 而你需要补充改动详情，可以调
fetch_pr_files。**仅当订阅文案明确要求 spec / plan / 文件内容
时才调用**——大部分情况 brief 自己已经够说清楚了。
```

Renderer must NOT add facts beyond what the brief and the
fetched files actually contain — same 1.0c invariant.

---

## 7. Coupling with 1.0c

These pieces of 1.0c are reused unchanged:

- `events` ingestion path — we just add new source values
- gatekeeper (decider) — reads `events.payload` opaquely; the
  project lockout works on `events.project_root` populated by
  repo mapping
- investigator — reads payload opaquely; same prompt
- renderer — reads payload opaquely; same prompt + new optional
  tool
- delivery loop — unchanged

These pieces of 1.0c need small adaptations for non-turn events:

- The `agent_summary` field in payload is turn-specific. For
  webhook events, the renderer's "fallback heuristic" of
  "subject_summary = rendered_text OR agent_summary OR
  user_message" needs to also consider PR title / commit
  summary / release name. Implementation: when seeding
  `recent_notifications_for_scope`, derive subject_summary from
  the source-appropriate field. See plan §3.

- Lockout's `last_segment(event.project_root)` works for
  paths but webhook events have project_root populated from
  the `external_repos` table — same path format, no special
  case needed. ✅ checked: matches.

---

## 8. Cost / latency budget

Webhook ingestion is cheap (no LLM at ingest time). The
expensive LLM calls happen later via gatekeeper / investigator
on whatever events match a subscription.

Per-day estimate for a small team:

| Event source | Daily volume | Notes |
|--------------|--------------|-------|
| turn         | ~200          | Same as 1.0a |
| github       | ~30-50        | PRs + pushes + comments |
| gitea        | ~20-30        | Self-hosted side projects |
| total        | ~250-280      | ~25-40% increase |

Decider call growth proportional to event growth. Investigator
call growth lower — most webhook events go to the same
investigation_job per subscription per aggregation window.

Renderer enrichment: each `fetch_pr_files` call is a single
external API hit + 24h cache. At <5 PRs/day, this is a few
calls/day at most.

---

## 9. Validation criteria

Concrete e2e scripts the implementation must pass:

### 9.1 Identity claim → event ingestion

1. bcc claims github login `billc8128`
2. A test webhook delivery comes in with `actor.login=billc8128`
3. Resulting `events` row has `user_id=bcc.profile_id`

### 9.2 Repo mapping → project lockout

1. `external_repos` has `('github', 'billc8128/vibelive') →
   '/Users/a/Desktop/vibelive'` mapped
2. bcc subscribes "vibelive 进展告诉我"; metadata has
   `matched_projects=["vibelive"]`
3. Webhook from `oneship` repo arrives → events row has
   `project_root='/Users/.../oneship'` → gatekeeper lockout
   skips the event for this subscription
4. Webhook from `vibelive` repo arrives → events row has
   `project_root='/Users/.../vibelive'` → lockout doesn't fire,
   gatekeeper LLM sees the event, opens an investigation

### 9.3 PR merge → spec/plan delivery

1. bcc has subscription "vibelive merge 后把 spec 和 plan 发给我"
2. albert merges PR #42 in `vibelive` repo with
   files_changed=['docs/spec.md', 'docs/plan.md', 'src/foo.ts']
3. Webhook arrives → events row → gatekeeper opens job →
   investigator runs → calls `fetch_pr_files` with paths_filter
   for spec/plan → brief includes file contents
4. bcc's DM has a notification with the spec.md and plan.md
   excerpts visible

### 9.4 Identity claim conflict

1. bcc claims `billc8128`
2. albert tries to claim `billc8128` → tool returns error
   "already claimed"

### 9.5 Webhook signature failure

1. POST to `/webhooks/github` with no `X-Hub-Signature-256` →
   401, no events row written
2. POST with wrong signature → 401, no events row written
3. POST with correct signature → 200, events row written

### 9.6 Idempotent re-delivery

1. GitHub re-delivers the same delivery uuid (simulated by
   POSTing the same webhook twice)
2. Second POST: events row's `(source, source_id)` unique
   constraint kicks in; ON CONFLICT DO UPDATE rewrites payload
   if changed
3. Only one investigation_job opens per subscription per
   aggregation window — covered by 1.0c's existing append logic

---

## 10. Out of scope (still)

Anything carried forward from 1.0c §9 plus:

- Linear, Jira, Slack ingestion — separate axes
- OAuth-based identity (GitHub OAuth app) — 2.0a uses self-claim
  + manual repo mapping; OAuth comes later if needed
- Bot-initiated comments / labels / merges — invariant: no
  auto-actions
- Cross-source event correlation ("PR #X relates to turn #Y") —
  investigator can read across sources via existing tools, no
  new mechanism needed for 2.0a
- Repo discovery from commit metadata in turns — 2.0a requires
  explicit `external_repos` rows
