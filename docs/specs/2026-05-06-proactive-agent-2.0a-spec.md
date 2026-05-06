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
- `external_repos` table for visible/admin-managed connected
  repositories
- Investigator enrichment: when a candidate job references a
  GitHub/Gitea PR, the investigator can read changed-file
  metadata and patch excerpts via a small fetch helper
- `/integrations` UI for viewing provider status, managing repo
  mappings, copying webhook URLs, and checking recent deliveries
- Optional service-role/admin-managed actor attribution via
  `external_identities`
- Decider's project lockout (1.0c §4.1) keeps working with repo
  identifiers (`github:owner/repo`) for webhook events

Out of scope (explicitly):

- Linear, Jira, Slack, Feishu Calendar, Feishu Doc — separate
  future axes
- OAuth-based identity claim / self-serve account claiming —
  2.0a does not let users self-assert external account ownership
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
    constraint extid_login_unique unique (provider, external_login)
);

-- Stable id uniqueness: a numeric external_id is the persistent
-- identity, login can drift. Without this constraint, after a
-- login rename the new login could be claimed by a second profile
-- → both rows have different logins but the same external_id,
-- which gives webhook actor lookup ambiguous results. Partial
-- unique index (skips NULLs) so legacy rows without external_id
-- aren't blocked.
create unique index extid_id_unique
    on public.external_identities (provider, external_id)
    where external_id is not null;

create index extid_profile_idx
    on public.external_identities (profile_id);
```

Why both `external_login` and `external_id`:

- GitHub allows users to rename their account. The login changes;
  the numeric id doesn't.
- Webhook payloads include both — we match on id when present,
  fall back to login otherwise.
- 2.0a does not expose a self-claim chat tool, because a login
  string alone does not prove ownership. Rows are service-role /
  admin-managed until an OAuth or challenge-based verification
  flow exists.

A profile can have multiple identities (one per provider).
Both external_login AND (when present) external_id are unique
per provider — two profiles can't both claim "billc8128" on
github, and after a rename two profiles can't both claim the
same numeric id with different logins.

### 3.2 `external_repos` — connected external repos

```sql
create table public.external_repos (
    id              uuid primary key default gen_random_uuid(),
    provider        text not null check (provider in ('github', 'gitea')),
    repo_full_name  text not null,    -- lowercase "billc8128/vibelive"
    project_root    text not null,    -- admin/display project key;
                                      -- NOT copied into webhook events
    created_by      uuid references public.profiles(id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint repo_unique unique (provider, repo_full_name)
);

create index repos_project_root_idx
    on public.external_repos (project_root);
```

A repo appears once in this registry. `repo_full_name` is stored
lowercase by the UI, admin script, Python helpers, and a database
CHECK constraint; webhook payloads are lowercased the same way.
`project_root` remains an admin/display field for grouping in the
UI and scripts; webhook events derive their canonical project
identity from the repo itself (`{provider}:{repo_full_name}`),
not from this column.

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

`source_id` is a stable resource identity, not the webhook
delivery uuid. Delivery ids are stored only in
`external_webhook_deliveries`.

| Event type | source_id format | Example |
|------------|------------------|---------|
| pull_request | `pull_request:{repo_full_name}:{pr_number}` | `pull_request:billc8128/vibelive:42` |
| push | `push:{repo_full_name}:{ref}:{after_sha}` | `push:billc8128/vibelive:refs/heads/main:abc123` |
| release | `release:{repo_full_name}:{tag}` | `release:billc8128/vibelive:v1.2.3` |
| issue_comment | `issue_comment:{repo_full_name}:{comment_id}` | `issue_comment:billc8128/vibelive:98765` |

This preserves the events table contract: one row per external
resource/update identity. A pull request opened, synchronized,
and merged updates the same `events` row instead of creating one
row per webhook delivery.

If a supported webhook payload lacks the resource identity needed
to build this `source_id` (PR number, push SHA, release tag, or
comment id), ingest keeps the archive row and marks it
`ignored_reason='missing_source_identity'`; it must not silently
drop the delivery after archive.

**Idempotency contract** (CRITICAL — see plan §3.4 for SQL):
GitHub and Gitea routinely re-deliver webhooks on receiver
errors. The `(source, source_id)` unique constraint dedupes the
ROW, but the upsert must NOT bump `payload_version` on
byte-identical re-delivery. If it did, every re-delivery would
re-enter `events_needing_decision` and produce duplicate
investigations / notifications.

For webhook events, `events.project_root` is the repo identifier
`{provider}:{repo_full_name}` (for example
`github:billc8128/vibelive`), not a developer-local filesystem
path from `external_repos`. This keeps lockout semantics global
and stable. Repo identifiers only hard-lockout on exact repo
identifier matches; short project-name matches fall through to
the gatekeeper LLM for webhook events to avoid same-name repo
collisions.

The upsert uses a `payload_fingerprint` md5 over the stable parts
of the **normalised** payload. It excludes top-level delivery
noise, duplicate derived repo identifiers (`project_root`,
`repo.project_root`), and identity lookup outputs
(`actor.profile_id`, `mentioned_profile_ids`) so rebinding an
identity does not re-notify old webhook events. Same fingerprint
→ no-op. Different fingerprint (e.g. PR description was edited
and the event re-ingested) → bump `payload_version`, re-enter
needs-decision. This mirrors 1.0c's late-summary semantics for
turn events.

`payload` jsonb contents per webhook event type — see §4.

`events.user_id` is always NULL for webhook events. The turn
path uses `events.user_id` for the event subject; webhook actor
identity is not the same concept and stays in
`payload.actor.profile_id` / `payload.actor.login`.

`payload.repo.project_root` mirrors the repo identifier for
gatekeeper/renderer compatibility. `external_repos.project_root`
is UI/admin metadata and must not be copied into webhook events.

### 3.5 `external_webhook_deliveries` — service-only delivery archive

Webhook bodies are recursively redacted before storage, then kept
for debugging / replay / audit-of-payload-edits. They are NOT in
`events.payload`.
Rationale:

- Raw GitHub PR payloads can run 50-200KB. Putting them on
  `events.payload` bloats the events table, multiplies decider /
  investigator prompts (every gatekeeper call would carry a
  full PR body), and risks shipping uninvolved fields (CI logs,
  review comments) to the LLM.
- The normalised compact `events.payload` (per §4) has
  everything the gatekeeper / investigator / renderer actually
  need.
- For rare deeper forensic inspection, the full delivery archive
  is a service-role-only side table. The side table stores the
  redacted parsed payload, not the original byte-for-byte body.

```sql
create table public.external_webhook_deliveries (
    id           bigserial primary key,
    provider     text not null check (provider in ('github', 'gitea')),
    delivery_id  text not null,             -- X-{Provider}-Delivery
    event_type   text not null,
    received_at  timestamptz not null default now(),
    raw_body     jsonb not null,            -- redacted parsed payload,
                                            -- not original bytes
    raw_headers  jsonb,                     -- minus auth/signing
    event_id     bigint references public.events(id) on delete set null,
    ignored_reason text,
    ignored_at   timestamptz,
    constraint webhook_delivery_unique unique (provider, delivery_id)
);

create index webhook_deliveries_event_idx
    on public.external_webhook_deliveries (event_id)
    where event_id is not null;

create index webhook_deliveries_received_at_idx
    on public.external_webhook_deliveries (received_at desc);

create index webhook_deliveries_ignored_idx
    on public.external_webhook_deliveries (ignored_at desc)
    where ignored_reason is not null;
```

RLS: service-role only — no policies needed since we use
service-role for all reads.

Retention: a daily cleanup job deletes rows where `received_at <
now() - interval '30 days'`. Useful debugging window without
forever-growth.

This table is NEVER read by the LLM agents. The bot's read tools
do not expose it. Operators inspect it via direct SQL when
something looks weird.

**Idempotent archive contract**: `archive_external_delivery`
preserves the first redacted body for a `(provider, delivery_id)`.
GitHub/Gitea redelivery arrives with the same key repeatedly; a
plain INSERT would raise on the unique constraint and short-
circuit ingest before `upsert_event` ever runs, while a normal
upsert would overwrite immutable evidence. The helper uses
`ON CONFLICT DO NOTHING` / Supabase `ignore_duplicates=True`,
then selects the existing row id when the insert was skipped:

```sql
insert into external_webhook_deliveries (
    provider, delivery_id, event_type,
    raw_body, raw_headers
) values (...)
on conflict (provider, delivery_id) do nothing
returning id;
```

The redelivery does not overwrite `raw_body` / `raw_headers`.
The `event_id` column is linked later by
`link_archive_to_event`, and ignored archive-only deliveries are
marked by `mark_archive_ignored(archive_id, reason)` with safe
reason values such as `unsupported_event_type`, `bot_actor`, and
`missing_source_identity`. If archive succeeds but later
normalisation / event upsert raises unexpectedly, ingest marks
the archive with `ignored_reason='ingest_error'` and logs the
exception. If archive itself fails, the webhook route returns 500
so the provider can retry rather than silently losing an
unarchived delivery.

`link_archive_to_event(archive_id, event_id)` is a thin
`UPDATE external_webhook_deliveries SET event_id = $event_id
WHERE id = $archive_id AND event_id IS NULL` — only writes
once, doesn't clobber an earlier successful link.

### 3.4 No changes to `subscriptions`, `notifications`,
   `investigation_jobs`, `decision_logs`

These all read `events.payload` opaquely. New event sources slot
in without schema changes.

The 1.0c project lockout (`subscriptions.metadata.matched_projects`)
continues to work for turn events as before. For webhook events,
`events.project_root` is a repo identifier such as
`github:billc8128/vibelive`. A subscription that explicitly
matches that repo identifier can be hard-skipped by lockout.
Short names like "vibelive" are not used to hard-skip webhook
events, because different providers/owners can have same-name
repos; those fall through to the LLM gatekeeper.

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

Each route reads one per-provider HMAC secret from environment:

- `GITHUB_WEBHOOK_SECRET` — used to verify
  `X-Hub-Signature-256` header (HMAC-SHA256 of raw body)
- `GITEA_WEBHOOK_SECRET` — used to verify `X-Gitea-Signature`
  header (HMAC-SHA256 of raw body)

2.0a deliberately uses a global provider secret per deployment,
not per-repo secrets. Per-repo secrets are a future hardening
step; until then, treat these secrets like DB credentials and
rotate if any connected repo's webhook configuration leaks.

Mismatched signature → 401 with no body. Missing secret →
500 with log line; the route fails closed.

Body limits are enforced before signature verification:
`Content-Length` is required, non-numeric values return 400,
missing values return 411, and values over 2MB return 413. The
route also checks actual bytes after reading. This avoids
chunked requests bypassing the declared body cap.

### 4.3 Event types we ingest in 2.0a

Before archive or normalisation, the parsed webhook payload runs
through a server-side best-effort redaction pass. This is a
compensation for the fact that GitHub/Gitea webhooks do not pass
through the local daemon redaction layer from `CLAUDE.md`.
Redaction covers common token/private-key/key-value-secret
patterns, including GitHub tokens, JWTs, Stripe live keys,
Bearer tokens, Postgres connection strings, and Feishu/Lark bot
webhook URLs. The original unredacted parsed body is not written
to Postgres.

For each event type we extract a stable shape into
`events.payload`. The redacted webhook body lives **only** in
`external_webhook_deliveries` (§3.5) for service-only debugging.
The LLM agents never see full webhook bodies — they read
`events.payload` (the normalised shape below), and for richer
content the investigator explicitly calls tools like
`fetch_pr_files` (§6).

Why no `payload.raw` even though earlier drafts had it: raw
GitHub PR bodies run 50-200KB, would multiply token cost on
every gatekeeper call, and would expose unrelated fields
(unrelated review comments, CI configurations, deploy keys)
to the LLM. Normalised payload + per-tool fetch is the cleaner
boundary.

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
  // raw NOT included — see external_webhook_deliveries (§3.5)
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
  // raw NOT included — see external_webhook_deliveries (§3.5)
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
  // raw NOT included — see external_webhook_deliveries (§3.5)
}
```

#### `issue_comment` (when comment mentions a known profile)

```jsonc
{
  "event_type": "issue_comment",
  "action": "created",
  "comment": { "id": 98765, "body": "...", "html_url": "..." },
  "issue": { "number": 567, "title": "..." },
  "repo": { ... },
  "actor": { ... },
  "mentioned_profile_ids": ["uuid", ...],   // resolved during
                                            // ingest from comment
                                            // body @-mentions
  // raw NOT included — see external_webhook_deliveries (§3.5)
}
```

We deliberately **don't** ingest `pull_request_review`,
`check_run`, `workflow_run`, `deployment_status`, etc. in 2.0a.
For ignored event types, we still store the redacted delivery in
`external_webhook_deliveries` for forensic value, but **no row
is written to `events`**. The archive row is marked with
`ignored_reason='unsupported_event_type'` so operators can tell
the difference between "accepted but intentionally ignored" and
"accepted but failed to normalise". Bot-authored deliveries are
also archived-only with `ignored_reason='bot_actor'`. Bot
detection uses provider metadata such as `sender.type=Bot` plus
the conventional `[bot]` login suffix; arbitrary `-bot` suffixes
are not treated as bots. These events don't enter the
investigation pipeline at all. Adding a typed shape for one of
these is a small follow-up if usage data shows demand. The
investigator does NOT have a "fall back to raw" path, by design:
raw bodies never reach LLM prompts.

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

Webhook timestamps are untrusted input. Normalisation clamps
`occurred_at` into `[now - 7 days, now + 1 hour]` before writing
the `events` row so malicious or malformed payload timestamps do
not poison ordering or future-window queries.

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

### 4.6 Webhook project identity

Webhook normalisation sets both `events.project_root` and
`payload.repo.project_root` to
`{provider}:{lower(repo_full_name)}`. It does not look up
`external_repos.project_root`, because that value is global UI
metadata and may be a developer-local filesystem path.

Implications for 1.0c project lockout (§4.1): exact repo
identifier matches may hard-skip mismatched webhook events.
Short project-name matches are intentionally treated as
insufficient for webhook hard-skip and fall through to the LLM
gatekeeper, which can read `payload.repo.full_name` and reason
normally.

---

## 5. Integration management UI

GitHub/Gitea are **system-level PMO agent integrations**, not
per-user account bindings. A maintainer connects a repository to
the PMO agent; after that, all PMO users can ask questions or
write notification rules against that repository, subject to the
normal PMO product permissions.

2.0a ships a dedicated `/integrations` page:

- Signed-in users can view connected providers, mapped
  repositories, webhook URLs, and recent accepted deliveries.
  Anonymous visitors see only the setup shell and login path; the
  page does not create a service-role client before auth.
- Signed-in PMO maintainers can add/remove repo mappings. The UI
  fails closed unless `PMO_INTEGRATION_ADMIN_USER_IDS` or
  `PMO_INTEGRATION_ADMIN_HANDLES` names allowed maintainers.
- Provider secrets and API tokens are never shown in the browser.
  They stay in the bot deployment environment.
- Redacted webhook payloads stay service-only in
  `external_webhook_deliveries`; the UI exposes only provider,
  event type, repo full name, delivery time, whether an `events`
  row was created, and user-readable status text translated from
  safe ignored reasons when a delivery was archived-only.

The setup flow is:

1. Open `/integrations`.
2. Add repo mapping: provider + `owner/repo` + `project_root`.
   The UI stores `owner/repo` lowercase.
3. Copy the generated `/webhooks/github` or `/webhooks/gitea`
   endpoint into the provider's repository webhook settings.
4. Select supported events: pull requests, pushes, releases,
   issue comments.
5. Send a test delivery; `/integrations` should show the accepted
   delivery and whether it linked to an event.

### 5.1 Optional actor attribution

`external_identities` is optional attribution, not source
access. It helps subscriptions like "my PRs", "albert 的 merge",
or "@external_login 的评论" map webhook actors/mentions to PMO
profiles. Repository access comes from the system integration
above.

2.0a keeps identity rows service-role/admin-managed. There is no
`link_external_identity` chat tool in the agent, because a user
typing "my GitHub is X" is not proof of ownership. The onboarding
alternative is: connect repos through `/integrations`, then add
`external_identities` via service-role/admin tooling only when
actor attribution is needed. A self-serve OAuth/challenge flow
can be added later.

---

## 6. Investigator enrichment

When a candidate investigation job references webhook events with
`payload.event_type='pull_request'`, the investigator can pull
changed-file metadata and patch excerpts before it decides
notify/suppress and writes the structured brief:

### 6.1 PR diff / files reading

For `pull_request` evidence, the investigator optionally calls a
new read-only tool:

```python
@tool(
    "fetch_pr_files",
    "Fetch the list of files changed in a GitHub or Gitea PR, "
    "optionally narrowed by paths_filter. Returns up to 30 files "
    "with paths + first 200 chars of patch_excerpt. \n\n"
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
  to avoid hammering external APIs. The cache key includes the PR
  head SHA when present (`repo/pr_number/head_sha`) so force-push
  updates do not reuse stale file lists.
- Retry transient HTTP/network failures with bounded backoff;
  permanent 4xx responses fail fast.
- `patch_excerpt` is produced by redacting the full returned patch
  first, then truncating to 200 characters. Truncating before
  redaction can leak token prefixes that cross the boundary.

This tool is added only to the investigator's tool subset. The
renderer deliberately does not get it; renderer must turn the
investigator's structured brief into Feishu markdown and must not
add new facts.

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
`fetch_pr_files` uses `resource_key=repo/pr_number/head_sha` when
the webhook payload includes `payload.pr.head_sha`, otherwise
falls back to `repo/pr_number`.

A daily reaper (or `delete from external_resource_cache where
expires_at < now() - interval '7 days'`) keeps the table small.

### 6.3 Investigator / renderer prompt boundary

The investigator prompt gets a small section for events with
external content:

```
如果 seed event 包含 PR，而订阅文案明确要求 spec / plan /
文件改动细节，可以调用 fetch_pr_files。该工具返回 patch_excerpt，
不是完整文件内容；不要把 patch 说成完整 spec/plan。
```

Renderer keeps the 1.0c invariant: it must not call external
fetch tools, add facts beyond the brief, or expand evidence.

---

## 7. Coupling with 1.0c

The 1.0c **pipeline orchestration** is reused unchanged
(gatekeeper loop, investigator loop, renderer loop, delivery
loop, all the RPCs). But the **payload projection** the
gatekeeper LLM actually sees IS turn-specific today and needs a
source-aware extension. Without that, a github PR event reaches
the gatekeeper as a bag of None fields and the LLM has nothing
to reason about.

### 7.1 Source-aware payload projection (REQUIRED)

`bot/agent/decider.py::build_judge_event(payload)` currently
extracts only turn fields (`turn_id`, `agent`, `user_message`,
`agent_summary`, `agent_response_full`, `project_path`,
`project_root`, `user_message_at`). For webhook events those are
all None.

2.0a replaces it with a dispatcher keyed on `payload.event_type`
(present in every webhook normalisation; defaults to "turn"
when absent for backward compat with 1.0c-shape payloads):

```python
def build_judge_event(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = payload.get("event_type") or "turn"
    if event_type == "turn":
        return _judge_event_for_turn(payload)
    if event_type == "pull_request":
        return _judge_event_for_pull_request(payload)
    if event_type == "push":
        return _judge_event_for_push(payload)
    if event_type == "release":
        return _judge_event_for_release(payload)
    if event_type == "issue_comment":
        return _judge_event_for_issue_comment(payload)
    return {"event_type": event_type, "summary": "(unrecognised event source)"}
```

Each per-source projection returns the same shape contract for
the gatekeeper:

```python
{
    "event_type": "pull_request" | "push" | "release" | ...,
    "headline": "<short user-facing one-liner>",
    "body_excerpt": "<200-400 chars of what happened>",
    "actor_handle": "<external_login or pmo handle>",
    "project_root": "<from event row>",
    "occurred_at": "<ISO>",
    # source-specific fields preserved from payload, e.g.:
    "pr_number": 1234, "merged": true,
    # ... (only fields gatekeeper might match against subscription
    #      descriptions; not the full raw body)
}
```

The shape is intentionally compact (~500-800 tokens including
the existing `headline` / `body_excerpt` truncation) so the
gatekeeper budget §8 holds.

Implementation lives next to the trigger code in plan §3.3
("Event payload normalisation"). Each per-source projection is
a small pure function with a unit test.

### 7.2 Investigator and renderer also need the projection

Investigator currently sees the full event payload via
`InvestigatableJobBundle.events`. With raw GitHub bodies these
are too big — the projection above gives the investigator the
same compact shape. **The full normalised payload (per spec §4)
remains on `events.payload`** for cases where the investigator
deliberately wants more detail (via the `fetch_pr_files` tool
in §6 or by reading payload directly through the read tools);
it just isn't dumped wholesale into the prompt.

Renderer reads investigator brief output — already prompt-shaped,
no projection needed there.

### 7.3 Things that are actually unchanged

- `events` row identity and the `(source, source_id)` unique
  constraint — extends naturally
- gatekeeper system prompt — reads from `build_judge_event`
  output, doesn't care what source produced it
- investigator system prompt — reads bundle.events list
  shape-by-shape; bundle entries get the §7.1 projection
- delivery loop — unchanged
- Lockout's project token extraction — webhook events use exact
  repo identifiers; short names fall through to the gatekeeper.

### 7.4 What this means for "fields the gatekeeper sees"

Sample subscription descriptions and what the projection
exposes:

| Subscription | Event source | Fields gatekeeper matches against |
|--------------|--------------|-----------------------------------|
| "vibelive merge 告诉我" | github pull_request, merged=true | `event_type=pull_request`, `merged=true`, `project_root=github:billc8128/vibelive`, `repo_full_name=billc8128/vibelive`, `headline="albert merged PR #42 ..."` |
| "albert 的 PR 提我" | github pull_request | `actor_handle=albert`, `event_type=pull_request` |
| "release 标签出来" | github release | `event_type=release`, `release.tag_name` |
| "vibelive 进展" | turn | original 1.0c fields |

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

Known 2.0a cost limit: there is no separate subject-index
fast-skip for broad subscriptions like "all my PRs". The
gatekeeper remains the first semantic filter. Stable
`source_id` coalesces repeated updates for the same PR into one
events row, but changed PR payloads still bump `payload_version`
and can re-enter the gatekeeper. Monitor gatekeeper call volume
by source/repo after rollout; if webhook volume dominates, 2.0b/c
should add subscription subject indexing or per-repo rate limits.

Investigator enrichment: each `fetch_pr_files` call is a single
external API hit + 24h cache. At <5 PRs/day, this is a few
calls/day at most.

Decider event fanout prefetches current notification rows in one
batch per event before iterating candidate subscriptions. It must
not do a `get_notification` round-trip per subscription on the
hot path.

Daily send-cap checks use an exact PostgREST count instead of
counting a capped page of notification rows.

Operational logging: webhook handler, ingest, normalizer, and
external fetch modules emit INFO logs with safe key fields
(`provider`, `event_type`, `delivery_id`, `archive_id`,
`event_id`, `source_id`, ignored reason, retry attempt, cache
hit/miss). Logs must never include raw body text, PR/comment
body, headers containing secrets, or external API tokens.

Project-token lockout cache invalidation is process-local in
2.0a. This is acceptable for the current single Railway bot
process; scale-out needs DB-backed invalidation or pub/sub.

---

## 9. Validation criteria

Concrete e2e scripts the implementation must pass:

### 9.1 Identity attribution → event ingestion

1. Service-role/admin maps github login `billc8128` to bcc's
   profile in `external_identities`
2. A test webhook delivery comes in with `actor.login=billc8128`
3. Resulting `events` row has `user_id=NULL` and
   `payload.actor.profile_id=bcc.profile_id`

### 9.2 Repo mapping → project lockout

1. A webhook from `billc8128/vibelive` arrives → events row has
   `project_root='github:billc8128/vibelive'`
2. A subscription explicitly mentioning
   `github:billc8128/vibelive` gets
   `matched_projects=["github:billc8128/vibelive"]`
3. Webhook from `github:other/vibelive` arrives → exact repo
   lockout skips the event for this subscription
4. A subscription that only says "vibelive" does not hard-skip
   same-name webhook repos; the LLM gatekeeper decides

### 9.3 PR merge → spec/plan delivery

1. bcc has subscription "vibelive merge 后把 spec 和 plan 发给我"
2. albert merges PR #42 in `vibelive` repo with
   files_changed=['docs/spec.md', 'docs/plan.md', 'src/foo.ts']
3. Webhook arrives → events row → gatekeeper opens job →
   investigator runs → calls `fetch_pr_files` with paths_filter
   for spec/plan → brief includes patch excerpts for those files
4. bcc's DM has a notification grounded in the changed-file /
   patch excerpts, without claiming to include full file contents

### 9.4 Identity attribution conflict

1. Admin inserts `billc8128` for bcc
2. A second insert/upsert attempt for albert violates the
   uniqueness contract or is rejected by the service helper

### 9.5 Webhook signature failure

1. POST to `/webhooks/github` with no `X-Hub-Signature-256` →
   401, no events row written
2. POST with wrong signature → 401, no events row written
3. POST with correct signature → 200, events row written

### 9.6 Idempotent re-delivery

1. GitHub re-delivers the same delivery uuid, or sends another
   delivery for the same PR/resource with changed payload
2. Second POST: events row's `(source, source_id)` unique
   constraint kicks in; ON CONFLICT DO UPDATE rewrites payload
   if changed
3. Only one investigation_job opens per subscription per
   aggregation window — covered by 1.0c's existing append logic

### 9.7 Redaction + observability

1. POST a signed PR payload whose PR body contains a fake GitHub
   token and `password=...`
2. Assert `external_webhook_deliveries.raw_body` and
   `events.payload` contain `[REDACTED]` and do not contain the
   original secret-like strings
3. Assert Railway/bot logs include `webhook.accepted`,
   `webhook.archived`, and either `webhook.event_upserted` or
   `webhook.archive_ignored` with provider/event/delivery ids
4. POST an unsupported but signed event → 200, archived-only,
   `ignored_reason='unsupported_event_type'`, and an INFO log with
   that reason
5. Simulate a normalizer/upsert exception after archive succeeds
   → webhook returns 200, archive row has
   `ignored_reason='ingest_error'`, and logs include the exception

---

## 10. Out of scope (still)

Anything carried forward from 1.0c §9 plus:

- Linear, Jira, Slack ingestion — separate axes
- OAuth-based provider installation (GitHub OAuth app / GitHub App)
  — 2.0a uses webhook setup plus `/integrations` repo mapping;
  OAuth/App installation comes later if needed
- Bot-initiated comments / labels / merges — invariant: no
  auto-actions
- Cross-source event correlation ("PR #X relates to turn #Y") —
  investigator can read across sources via existing tools, no
  new mechanism needed for 2.0a
- Repo discovery from commit metadata in turns — 2.0a requires
  explicit `external_repos` rows
