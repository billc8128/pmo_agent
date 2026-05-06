# Proactive PMO Agent 2.0a — Implementation Plan

- **Status**: Draft, branch `proactive-agent`
- **Date**: 2026-05-06
- **Spec**: [2.0a Spec](2026-05-06-proactive-agent-2.0a-spec.md)
- **Strategy**: [2.0 Strategy](2026-05-06-proactive-agent-2.0-strategy.md)
- **Predecessors**: 1.0a + 1.0b + 1.0c — all must be deployed and
  stable before starting 2.0a. The pipeline downstream of `events`
  must work end-to-end against turn events first; 2.0a is
  additive.

This is the practical "how to land 2.0a" plan. Spec is the
source of truth for behaviour. This document is the source of
truth for **build order**.

---

## 0. Pre-flight (~10 min)

- [ ] Confirm 1.0c is in production; one e2e turn → notification
      flow works end-to-end
- [ ] Confirm latest migration on production is 0019
      (`project_path_tokens_for_lockout`). 2.0a uses **0020**.
- [ ] Confirm pmo-bot is healthy on Railway
- [ ] Confirm at least one user has bound feishu_links (so
      identity claim has something to attach to)
- [ ] Confirm GitHub admin access on at least one repo we plan
      to webhook (we'll need to set the secret)

---

## 1. Migration 0020 (~30 min)

**File**: `backend/supabase/migrations/0020_external_event_sources.sql`

Creates:

- `external_identities` table per spec §3.1 with full column set:
  - `id`, `profile_id`, `provider`, `external_login`,
    `external_id`, `created_at`, `updated_at`
  - constraint `extid_login_unique unique (provider, external_login)`
  - **partial unique index** `extid_id_unique on
    (provider, external_id) where external_id is not null` —
    prevents two profiles from claiming the same numeric id with
    different logins after a GitHub rename
  - index `extid_profile_idx on (profile_id)`
- `external_repos` table per spec §3.2:
  - `id`, `provider`, `repo_full_name`, `project_root`,
    `created_by`, `created_at`, `updated_at`
  - constraint `repo_unique unique (provider, repo_full_name)`
  - index `repos_project_root_idx on (project_root)`
- `external_webhook_deliveries` table per spec §3.5:
  - service-role-only raw archive of full webhook bodies
  - `(provider, delivery_id)` unique
  - 30-day retention (cleanup outside the migration)
- `external_resource_cache` table per spec §6.2 for fetched
  PR diffs and similar.
- **`events.payload_fingerprint` column** (text, nullable) —
  webhook events compute md5 over the normalised payload
  (excluding volatile timestamps) and write it on insert.
  **Turn events**: 0020 does NOT extend `on_turn_to_event` to
  populate this column. Rationale:
  - 1.0c's existing on_turn_to_event computes a local fingerprint
    in PL/pgSQL but doesn't persist it; persisting it would
    require a 0020 backfill across the existing `events` table
    (potentially many rows) AND a function rewrite — a much
    bigger change than 2.0a needs.
  - The fingerprint guard ONLY matters for webhook redeliveries.
    Turn events arrive once per (turn_id, payload_version) and
    1.0c's logic already handles late-summary updates correctly
    via `payload_version`.
  - Therefore: webhook upsert path checks
    `payload_fingerprint IS DISTINCT FROM excluded.payload_fingerprint`
    AND `excluded.payload_fingerprint IS NOT NULL`. Turn events
    leave the column NULL — they never reach this upsert path
    anyway (they go through the trigger).
  - Cleaner-but-bigger alternative left for a future migration:
    extend `on_turn_to_event` to populate the column AND
    backfill all existing rows. Out of scope for 2.0a.
- RLS:
  - `external_identities` enabled. Policy: owner can read their
    own row (`auth.uid() = profile_id`); inserts/updates only via
    service-role. Lets users see their own claim on /me without
    leaking other users' claims.
  - `external_repos` enabled, no policies (service-role only —
    repo mapping is admin-managed).
  - `external_webhook_deliveries` enabled, no policies
    (service-role only — never read by LLMs).
  - `external_resource_cache` enabled, no policies (service-role
    only — cache is internal).

**Apply path**: via Supabase Management API (same pattern as
0005-0019).

**Smoke tests** (in transaction, ROLLBACK at end):

1. Insert a fake profile (or use existing one). Insert into
   `external_identities` with provider='github',
   external_login='test_user'; verify row exists. Insert again
   with same (provider, external_login) — expect
   `extid_login_unique` violation.
2. Two profiles, both insert with provider='github',
   external_id='12345' and DIFFERENT logins → second insert
   raises `extid_id_unique` violation. Same with both rows
   external_id IS NULL → both succeed (partial index skips
   NULLs).
3. `external_repos.(provider, repo_full_name)` uniqueness check
   (insert duplicate raises).
4. `external_webhook_deliveries.(provider, delivery_id)`
   uniqueness check.
5. Insert events row with payload_fingerprint='abc'; insert
   another with same (source, source_id) and fingerprint='abc' →
   ON CONFLICT no-op path verified by checking payload_version
   stays 1. Insert again with fingerprint='def' → version=2,
   ingested_at fresh. (See plan §3.4 SQL.)
6. With anon key: select from each new table → 0 rows / RLS
   denies (depending on table). Auth-as-fake-user select from
   `external_identities` → only own row visible.
7. With service-role: full access works on all four new tables.

**Exit criterion**: all 7 smoke tests pass; ROLLBACK leaves DB
clean.

---

## 2. Bot DB layer additions (~30 min)

**File**: `bot/db/queries.py` extends with helpers:

Identity:
- `link_external_identity(profile_id, provider, external_login,
  external_id=None) -> dict` — INSERT, idempotent if same
  profile re-claims same login (UPDATE updated_at). Raises on
  conflict if a different profile claims it (caller surfaces
  user-friendly error).
- `unlink_external_identity(profile_id, provider) -> bool` —
  DELETE.
- `lookup_profile_by_external_login(provider, external_login,
  external_id=None) -> str | None` — returns profile_id or
  None. Prefers external_id match when provided.
- `external_identities_for_profile(profile_id) -> list[dict]` —
  for the /me page UI.

Repos:
- `lookup_project_root_for_repo(provider, repo_full_name) -> str
  | None` — returns project_root or None.
- `register_external_repo(provider, repo_full_name,
  project_root, created_by=None) -> dict` — for admin script /
  bootstrap.
- `external_repos_for_project_root(project_root) -> list[dict]`
  — reverse lookup for renderer (e.g. "what repos are in this
  project's universe").

Resource cache:
- `lookup_external_resource(provider, resource_kind,
  resource_key) -> dict | None` — returns cached content if not
  expired.
- `write_external_resource(provider, resource_kind,
  resource_key, content, ttl_seconds=86400) -> None`.

**Dataclasses**: add `ExternalIdentity`, `ExternalRepo` mirroring
the table columns. Both are simple `@dataclass` with
`_dataclass_from_row`.

**Exit criterion**: smoke from Python REPL — link an identity,
look it up, link a repo, look it up, write a cache entry, read
it back.

---

## 3. Webhook ingestion (~3-4h)

### 3.1 Routes

**File**: `bot/web/feishu/webhook.py` already exists. Add
`bot/web/external/__init__.py` and `bot/web/external/github.py`,
`bot/web/external/gitea.py` (or co-locate in one
`bot/web/external/webhooks.py` if smaller). Route registration
in `bot/app.py` lifespan or wherever Feishu webhook routes are
registered.

```python
_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024  # 2MB cap

@app.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    # 1. Body size cap BEFORE reading body — protects against
    #    DoS via giant POST. GitHub PR bodies max ~150KB in
    #    practice; 2MB is generous.
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > _MAX_WEBHOOK_BODY_BYTES:
        return Response(status_code=413)  # Payload Too Large
    raw_body = await request.body()
    if len(raw_body) > _MAX_WEBHOOK_BODY_BYTES:
        # body() ignores Content-Length, double-check on actual bytes
        return Response(status_code=413)

    # 2. Signature first, error responses second. Don't parse JSON
    #    until signature passes — prevents wasting CPU on attacker
    #    bodies.
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_github_signature(raw_body, signature,
                                     settings.github_webhook_secret):
        return Response(status_code=401)

    # 3. JSON parse with explicit error handling. Malformed JSON →
    #    400, no DB writes. Don't surface parse errors to caller
    #    (could leak info); caller doesn't need details.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return Response(status_code=400)
    if not isinstance(payload, dict):
        return Response(status_code=400)

    event_type = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    if not event_type or not delivery:
        # Required headers missing → not a real GitHub webhook
        # (signature could have passed if attacker has the secret
        # but didn't replicate headers). Reject for hygiene.
        return Response(status_code=400)

    await ingest_external_event("github", event_type, delivery,
                                 payload, raw_body=raw_body)
    return Response(status_code=200)
```

`/webhooks/gitea` mirrors the structure with
`X-Gitea-Signature` / `X-Gitea-Event` / `X-Gitea-Delivery`.

**Failure mode summary** (the public-facing endpoint contract):

| Status | Reason |
|--------|--------|
| 200 | webhook accepted (whether or not it produced an `events` row — duplicate redelivery still 200) |
| 400 | malformed JSON / missing required headers |
| 401 | signature missing or wrong |
| 413 | body exceeds 2MB cap |
| 500 | internal error (logged, no body returned) — GitHub will retry |

NOT 422 / 404 / others — keep the surface small.

### 3.2 Signature verification

```python
def _verify_github_signature(body: bytes, header: str,
                              secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header)
```

Same shape for Gitea (HMAC-SHA256 of raw body, no `sha256=`
prefix in Gitea's case).

Both secrets live in env: `GITHUB_WEBHOOK_SECRET` /
`GITEA_WEBHOOK_SECRET`. Add to Railway and Vercel env vars
(Vercel only if web also handles webhooks; for 2.0a we keep
webhooks bot-side, so just Railway).

### 3.3 Event payload normalisation

**File**: `bot/external/normalizer.py` (new module).

Per event type, a function takes raw webhook body and produces
the typed shape from spec §4.3. Skeleton:

```python
def normalize_github(event_type: str, raw: dict) -> dict | None:
    handler = {
        "pull_request": _normalize_pr,
        "push": _normalize_push,
        "release": _normalize_release,
        "issue_comment": _normalize_issue_comment,
    }.get(event_type)
    if handler is None:
        return None  # event type not ingested
    return handler(raw)
```

Each handler:
- extracts the typed fields per spec §4.3
- looks up actor profile_id via `lookup_profile_by_external_login`
- looks up project_root via `lookup_project_root_for_repo`
- determines `occurred_at` per the table in spec §4.4
- **does NOT** copy the entire raw body into `events.payload`.
  Per spec §4.3 raw goes to a service-only
  `external_webhook_deliveries` table (added in plan §1's
  migration); `events.payload` carries only the compact
  normalised shape so token / DB sizes stay bounded.

Gitea has the same structure; the normalizer functions should be
parameterized by provider where the field names differ.

### 3.3.1 Source-aware judge projection

Per spec §7.1, `bot/agent/decider.py::build_judge_event` is
turn-only today and returns all-None for webhook payloads.
That means the gatekeeper LLM sees no event_type / actor / repo
and almost certainly fails to match subscriptions like
"vibelive merge 告诉我". This is a hard prerequisite for 2.0a
to work — it's not a polish step.

**File**: `bot/agent/decider.py`

Replace `build_judge_event` with a dispatcher keyed on
`payload.event_type`:

```python
def build_judge_event(payload: dict[str, Any]) -> dict[str, Any]:
    et = payload.get("event_type") or "turn"
    if et == "turn":               return _judge_event_for_turn(payload)
    if et == "pull_request":       return _judge_event_for_pr(payload)
    if et == "push":               return _judge_event_for_push(payload)
    if et == "release":            return _judge_event_for_release(payload)
    if et == "issue_comment":      return _judge_event_for_issue_comment(payload)
    return {"event_type": et, "summary": "(unknown source)"}
```

Each `_judge_event_for_*` returns the contract from spec §7.1:
`event_type`, `headline`, `body_excerpt`, `actor_handle`,
`project_root`, `occurred_at`, plus a few source-specific keys
the gatekeeper might key off (e.g. `merged: true`, `pr_number`,
`tag_name`). Truncate body_excerpt to ~400 chars to keep the
gatekeeper budget bounded.

`_judge_event_for_turn` is the existing 1.0c behavior renamed.

Investigator's bundle construction (`InvestigatableJobBundle.events`
list in queries.py's `claim_investigatable_jobs` wrapper) ALSO
runs each event payload through `build_judge_event` before
including it in the prompt. Investigator gets the same compact
shape. If investigator wants more (e.g. file diffs), it calls
`fetch_pr_files` (§6).

**Unit tests** (in `bot/tests/test_proactive_2_0a.py`):
- `test_judge_event_for_pr_extracts_merge_signal` — payload has
  action='closed' + merged=true, projection returns merged=true,
  event_type='pull_request', headline includes PR title.
- `test_judge_event_for_turn_unchanged` — passing a 1.0c-shape
  payload through the new dispatcher yields exactly the previous
  output (regression guard).
- `test_judge_event_for_unknown_event_type_fallback` — payload
  with event_type='workflow_run' returns the generic
  "(unknown source)" projection rather than raising.
- `test_judge_event_excludes_raw` — confirms `payload.raw` (if
  present) is not included in projection output.

### 3.4 Ingest function

`bot/external/ingest.py`:

```python
async def ingest_external_event(
    provider: str,           # 'github' or 'gitea'
    event_type: str,         # 'pull_request', 'push', etc.
    delivery_id: str,        # X-{Provider}-Delivery
    raw_body: dict,
) -> None:
    if provider == "github":
        normalized = normalize_github(event_type, raw_body)
    elif provider == "gitea":
        normalized = normalize_gitea(event_type, raw_body)
    else:
        return
    if normalized is None:
        return  # event type ignored
    queries.upsert_event(
        source=provider,
        source_id=f"{event_type}:{delivery_id}",
        user_id=normalized.get("actor", {}).get("profile_id"),
        project_root=normalized.get("repo", {}).get("project_root"),
        occurred_at=normalized["occurred_at"],
        payload=normalized,
    )
```

Note `queries.upsert_event` is **a new helper** — current 1.0a
trigger writes events directly via the SQL trigger on `turns`.
For external events there's no turn row to trigger from, so we
write events directly via service-role, using the same
`(source, source_id)` unique constraint for idempotency.

**Idempotency contract (CRITICAL)**: GitHub and Gitea will
re-deliver the same `delivery_id` on receiver errors. A naive
"on conflict do update set payload_version + 1" would bump the
version and re-enter `events_needing_decision`, causing duplicate
investigations and notifications. Instead:

```sql
insert into events (source, source_id, user_id, project_root,
                    occurred_at, payload, payload_version,
                    payload_fingerprint)
values (...)
on conflict (source, source_id) do update
    -- Only bump payload_version when the *normalised* payload
    -- actually changed (computed via fingerprint, NOT raw equality
    -- — the raw body has timestamps that drift between
    -- redeliveries). Most redeliveries are byte-identical
    -- normalised; they end up as no-op updates and DO NOT
    -- re-enter events_needing_decision.
    set payload = case
            when excluded.payload_fingerprint
                 is distinct from events.payload_fingerprint
            then excluded.payload
            else events.payload
        end,
        payload_fingerprint = excluded.payload_fingerprint,
        payload_version = case
            when excluded.payload_fingerprint
                 is distinct from events.payload_fingerprint
            then events.payload_version + 1
            else events.payload_version
        end,
        ingested_at = case
            when excluded.payload_fingerprint
                 is distinct from events.payload_fingerprint
            then now()
            else events.ingested_at
        end
returning id;
```

`payload_fingerprint` is a new column on `events` (added in plan
§1's migration if not already present from 1.0c). For webhook
events: `md5(stable_json(normalised_payload_minus_volatile_fields))`
where volatile fields are timestamps the source generates per
delivery. For turn events: same fingerprint is computed in the
trigger from §1.0c's existing fingerprint logic, mapped to this
column.

**Why this matters for re-delivery**: GitHub's webhook delivery
retries are common (any 5xx from our side triggers redelivery).
We MUST NOT treat them as new events. The fingerprint guard
ensures retries are no-ops.

**Why we DON'T just `do nothing`**: a webhook content can
legitimately change between deliveries — e.g. PR description
edited triggers a new `synchronize` event with new content but
sometimes the same delivery_id depending on configuration. The
fingerprint approach handles both: identical retries are no-ops,
content changes bump version (same as turn agent_summary
arriving late in 1.0c).

### 3.5 Files touched in this chunk

- `bot/web/external/webhooks.py` (new)
- `bot/external/normalizer.py` (new)
- `bot/external/ingest.py` (new)
- `bot/db/queries.py` — adds `upsert_event` helper
- `bot/app.py` — register the new routes in lifespan
- `bot/config.py` — add `github_webhook_secret`,
  `gitea_webhook_secret` settings

**Exit criterion**:
- POST a synthetic GitHub `pull_request` payload (with valid
  signature) to `/webhooks/github` — events row appears with
  source='github', payload normalised correctly,
  payload_version=1
- Same delivery_id POSTed AGAIN with byte-identical body → still
  ONE events row, **payload_version still 1**
  (fingerprint-equal → no-op). Critically: events_needing_decision
  view does NOT re-include this row.
- Same delivery_id POSTed with body content edited (e.g. PR title
  changed) → still ONE events row, payload_version=2,
  events_needing_decision DOES re-include this row.
- POST with bad signature → 401, no events row written and no
  external_webhook_deliveries entry.

---

## 4. Identity claim chat tool (~30 min)

**File**: `bot/agent/tools_meta.py`

Add three tools (`link_external_identity`,
`unlink_external_identity`, `list_external_identities`)
following the pattern of the existing subscription tools.

`link_external_identity` validates:
- asker is bound (has feishu_links)
- provider in {'github', 'gitea'}
- external_login matches `^[a-zA-Z0-9-]{1,39}$`
- on conflict (already claimed by another), surface a
  user-readable error including the suggestion "if that's a
  mistake, contact bcc"

Tools added to `build_meta_tools(ctx)` list and to runner.py
SYSTEM_PROMPT (so users know what to ask for).

**Exit criterion**: from Feishu DM with bot, "我的 github 是
billc8128" → bot calls `link_external_identity` →
`external_identities` row exists → "我都绑定了哪些外部账号" →
bot lists them.

---

## 5. Repo mapping bootstrap (~30 min)

For 2.0a we don't ship a UI for repo mapping. Instead, a small
admin script lives at `backend/scripts/register_external_repos.mjs`:

```js
// Usage: node register_external_repos.mjs
// Reads a hardcoded list of (provider, repo, project_root)
// triples and upserts them into external_repos.
const repos = [
  { provider: 'github', repo: 'billc8128/vibelive',
    project_root: '/Users/a/Desktop/vibelive' },
  { provider: 'github', repo: 'billc8128/oneship',
    project_root: '/Users/a/Desktop/oneship' },
  { provider: 'gitea',  repo: 'team/internal-tools',
    project_root: '/Users/a/Desktop/internal-tools' },
];
// ... upsert via supabase service-role client ...
```

Run once after migration applies. Update the file as new repos
get added to the team.

A future user-facing UI can come once we know what shape it
needs.

**Exit criterion**: script runs, `external_repos` populated,
manual SELECT confirms.

---

## 6. Renderer enrichment: `fetch_pr_files` tool (~1.5h)

**File**: `bot/agent/renderer.py` and a new
`bot/external/fetch.py` module.

```python
# bot/external/fetch.py
async def fetch_pr_files(provider: str, repo_full_name: str,
                          pr_number: int,
                          paths_filter: list[str] | None = None) -> dict:
    """Returns up to 30 files with paths + first 200 chars of content."""
    cache_key = f"{repo_full_name}/{pr_number}"
    cached = queries.lookup_external_resource(
        provider, "pr_files", cache_key
    )
    if cached:
        result = cached["content"]
    else:
        result = await _fetch_pr_files_remote(provider,
                                               repo_full_name,
                                               pr_number)
        queries.write_external_resource(
            provider, "pr_files", cache_key, result,
            ttl_seconds=86400
        )
    if paths_filter:
        return {
            "files": [f for f in result.get("files", [])
                      if any(p in f["path"] for p in paths_filter)]
        }
    return result
```

`_fetch_pr_files_remote` calls GitHub's
`/repos/{owner}/{repo}/pulls/{pr_number}/files` endpoint with
`Authorization: token $GITHUB_API_TOKEN`. Pagination capped at
30 files. Each file includes path + first 200 chars of content
(via raw_url fetch, also cached at the file level).

Add the tool to `bot/agent/renderer.py`'s tool subset:

```python
@tool(
    "fetch_pr_files",
    "Fetch the list of files changed in a GitHub or Gitea PR, "
    "optionally narrowed by paths_filter. Use sparingly — costs "
    "an external API call (cached 24h). Only call when the user's "
    "subscription explicitly requests file content (e.g. 'send "
    "spec/plan'), not for every PR notification.",
    {"event_id": int, "paths_filter": list[str] or None},
)
async def fetch_pr_files_tool(args: dict) -> dict:
    event = queries.get_event(args["event_id"])
    if not event or event["source"] not in ("github", "gitea"):
        return err("event is not a PR event")
    payload = event["payload"]
    if payload.get("event_type") != "pull_request":
        return err("event is not a pull request")
    return ok(await fetch_pr_files(
        provider=event["source"],
        repo_full_name=payload["repo"]["full_name"],
        pr_number=payload["pr"]["number"],
        paths_filter=args.get("paths_filter"),
    ))
```

Add to investigator's tool subset too — investigator may need
it for "read enough context" on PR-related subscriptions.

**Files touched**:
- `bot/external/fetch.py` (new)
- `bot/agent/renderer.py` — add tool registration; extend the
  1.0c renderer prompt with a small section about fetch_pr_files
- `bot/agent/investigator.py` — same tool added
- `bot/db/queries.py` — `get_event(event_id)` helper added
- `bot/config.py` — `github_api_token`, `gitea_api_url` settings

**Exit criterion**:
- Insert a synthetic `pull_request` event referencing a real PR
- Call `fetch_pr_files` with the event_id → returns file list
- Repeat call → second call hits cache (no external API call)
- Call with paths_filter=['spec.md'] → only spec.md returned

---

## 7. Renderer / investigator prompt updates (~30 min)

Both prompts get a small extension explaining the new tool:

```
当 brief 里 evidence 包含 GitHub/Gitea PR (events.source in
('github','gitea') AND payload.event_type='pull_request') 时:
- 默认信任 brief 已有的 key_facts，不要每条都拉文件
- 仅当订阅文案明确说"把 spec/plan 发给我"或者类似要求时，
  调用 fetch_pr_files 拿对应文件
- fetch_pr_files 是缓存的，重复调用同一 PR 不会重复花钱
- 永远不要超出 brief.evidence_event_ids 列出的 PR
```

**Files**: `bot/agent/renderer.py` /
`bot/agent/investigator.py`. Update the system prompt
constants. Run existing tests to confirm prompt parsing still
works.

**Exit criterion**: existing 1.0c tests still pass; manual
inspection of prompt text confirms tool description is
self-explanatory.

---

## 8. End-to-end validation (~1h)

Run the validation scripts from spec §9:

1. Identity claim → event ingestion (spec §9.1)
2. Repo mapping → project lockout (spec §9.2)
3. PR merge → spec/plan delivery (spec §9.3)
4. Identity claim conflict (spec §9.4)
5. Webhook signature failure (spec §9.5)
6. Idempotent re-delivery (spec §9.6)

For tests requiring real webhook delivery, configure GitHub
webhook on a test repo pointing at `pmo-bot.up.railway.app/webhooks/github`
with the test secret.

**Exit criterion**: 6/6 validation scripts pass against a
sandbox or production deployment.

---

## 9. Roadmap update (~10 min)

Mark 2.0a done in roadmap §2.0:
- Move 2.0a section to "deployed"
- Update "current state" notes
- Clarify what 2.0b/c gain from 2.0a now being in place

---

## 10. Commit + push

Single commit on `proactive-agent` branch:

```
2.0a: external event sources (GitHub + Gitea)

Adds GitHub and Gitea webhook ingestion alongside turns. New
external_identities and external_repos tables let webhook events
map to existing profiles and project_roots so 1.0c's project
lockout, gatekeeper, investigator, renderer, delivery all work
unchanged.

See docs/specs/2026-05-06-proactive-agent-2.0a-spec.md for the
full behaviour contract; this commit implements §3 ingest, §5
identity claim chat tool, §6 fetch_pr_files renderer enrichment.

Repo mapping is admin-managed via
backend/scripts/register_external_repos.mjs; user-facing UI is
deferred until usage shows what shape it needs.
```

Push, deploy via Railway, run §8 validation.

---

## Cut points

If time-pressured:

- **Skip §6 (fetch_pr_files)** — start with PRs reaching the
  investigator with their title + body only. The "spec/plan"
  delivery story still works if the PR description includes the
  file content; just less rich for diff-heavy PRs. Easy to add
  later.
- **Skip Gitea entirely first** — start with only GitHub. Gitea
  is structurally identical so it's a small follow-up.
- **Skip §4 (chat tool for identity claim)** — bootstrap by
  manually inserting `external_identities` rows via SQL for the
  first few users. Add the tool later when more users need
  self-service.

Don't cut: §1 migration, §2 db layer, §3 webhook ingest, §5
repo bootstrap. That's the irreducible 2.0a.

---

## Risks specific to 2.0a rollout

1. **Webhook secret leakage**. Treat
   `GITHUB_WEBHOOK_SECRET` like a database password. If
   leaked, attacker can forge events → fake notifications. Plan:
   secrets-only-in-env, no logging, rotate if any concern.

2. **Identity claim impersonation**. The self-claim flow lets
   bcc claim `albert_github_login` if albert hasn't claimed it
   yet. Mitigation: spec §5.1 text discourages this; in practice
   we trust the small team. Real defense would be OAuth — that's
   a 2.0a-followup if abuse appears.

3. **PR diff fetch leakage**. fetch_pr_files reads private repo
   content into our DB cache. Only users with bot DMs can
   trigger it — same trust boundary as turn ingestion. Cache
   has 24h TTL + 7d cleanup; PR titles already in events.payload
   are the bigger surface.

4. **Webhook flood**. A noisy repo pushing many events could
   blow past 1.0c's gatekeeper budget. Monitor decision_logs
   row growth post-deploy; if a single repo's events dominate,
   add a per-source rate limiter.

5. **Repo mapping drift**. If `external_repos.project_root` is
   wrong (typo, repo renamed), all events from that repo land
   in the wrong project. Document the bootstrap script + run
   the mapping verification query (see plan §5).
