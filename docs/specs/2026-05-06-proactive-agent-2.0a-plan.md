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
  - `repo_full_name` is canonical lowercase. Migration 0022
    normalises existing rows and adds a DB CHECK so future
    bypasses fail loudly.
  - constraint `repo_unique unique (provider, repo_full_name)`
  - index `repos_project_root_idx on (project_root)`
- `external_webhook_deliveries` table per spec §3.5:
  - service-role-only archive of redacted webhook bodies
  - `(provider, delivery_id)` unique
  - Migration 0022 adds `ignored_reason` / `ignored_at` plus an
    ignored-delivery index so archive-only deliveries are
    operationally visible.
  - 30-day retention (cleanup outside the migration)
- `external_resource_cache` table per spec §6.2 for fetched
  PR diffs and similar.
- **`events.payload_fingerprint` column** (text, nullable) —
  webhook events compute md5 over the stable normalised payload
  and write it on insert. The hash excludes top-level delivery
  noise, duplicate derived repo identifiers (`project_root`,
  `repo.project_root`), and identity lookup outputs
  (`actor.profile_id`, `mentioned_profile_ids`) so identity
  rebinding does not re-notify old webhook events.
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
    repo mapping is managed through trusted server actions and
    admin scripts).
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
- `register_external_repo(provider, repo_full_name,
  project_root, created_by=None) -> dict` — for admin script /
  bootstrap. Lowercases `repo_full_name` before write.
- `lookup_project_root_for_repo(provider, repo_full_name)
  -> str | None` — lowercases the lookup key.
- `external_repos_for_project_root(project_root) -> list[dict]`
  — reverse lookup for renderer (e.g. "what repos are in this
  project's universe").

Resource cache:
- `lookup_external_resource(provider, resource_kind,
  resource_key) -> dict | None` — returns cached content if not
  expired.
- `write_external_resource(provider, resource_kind,
  resource_key, content, ttl_seconds=86400) -> None`.

External ingest:
- `archive_external_delivery(provider, delivery_id, event_type,
  raw_body, raw_headers) -> int` — idempotent archive helper.
  Uses an insert/upsert with `ignore_duplicates=True` and returns
  the archive row id; duplicate redeliveries must preserve the
  first redacted body and must not raise before `upsert_event` can
  run.
- `link_archive_to_event(archive_id, event_id) -> None` — sets
  `external_webhook_deliveries.event_id` only if it is currently
  NULL; never clobbers an existing successful link.
- `mark_archive_ignored(archive_id, reason) -> None` — writes
  safe audit reasons like `unsupported_event_type`, `bot_actor`,
  `missing_source_identity`, or `ingest_error` for deliveries
  that were archived but did not become events rows.
- `upsert_event(source, source_id, user_id, project_root,
  occurred_at, payload) -> int` — computes
  `payload_fingerprint` for webhook events from a stable subset of
  the normalised payload, then runs the idempotent
  `(source, source_id)` upsert from §3.4. Same fingerprint is a
  no-op; changed fingerprint bumps `payload_version`.
- `get_event(event_id) -> dict | None` — used by `fetch_pr_files`
  to verify the event source/type and extract PR metadata.

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
    content_length_raw = request.headers.get("content-length")
    if not content_length_raw:
        return Response(status_code=411)  # Length Required
    try:
        content_length = int(content_length_raw)
    except ValueError:
        return Response(status_code=400)
    if content_length > _MAX_WEBHOOK_BODY_BYTES:
        return Response(status_code=413)  # Payload Too Large
    raw_body = await request.body()
    if len(raw_body) > _MAX_WEBHOOK_BODY_BYTES:
        # body() ignores Content-Length, double-check on actual bytes
        return Response(status_code=413)

    # 2. Secret and signature first, error responses second. Don't parse JSON
    #    until signature passes — prevents wasting CPU on attacker
    #    bodies.
    if not settings.github_webhook_secret:
        return Response(status_code=500)
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

    await ingest_external_event(
        provider="github",
        event_type=event_type,
        delivery_id=delivery,
        payload=payload,
        raw_bytes=raw_body,
        headers=dict(request.headers),
    )
    return Response(status_code=200)
```

`/webhooks/gitea` mirrors the structure with
`X-Gitea-Signature` / `X-Gitea-Event` / `X-Gitea-Delivery`.

**Failure mode summary** (the public-facing endpoint contract):

| Status | Reason |
|--------|--------|
| 200 | webhook accepted (whether or not it produced an `events` row — duplicate redelivery still 200) |
| 400 | malformed JSON / missing required headers |
| 411 | missing `Content-Length`; chunked uploads are rejected |
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

Same shape for Gitea (HMAC-SHA256 of raw body). Accept both bare
hex signatures and a `sha256=` prefix for newer Gitea versions.

Both secrets live in env: `GITHUB_WEBHOOK_SECRET` /
`GITEA_WEBHOOK_SECRET`. Add to Railway and Vercel env vars
(Vercel only if web also handles webhooks; for 2.0a we keep
webhooks bot-side, so just Railway). 2.0a uses one global secret
per provider per deployment; per-repo secrets are deferred and
documented as a rollout hardening item.

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
  and marks bot senders (`sender.type=Bot` or `login` ending in
  `[bot]`) so ingest can archive-only those deliveries. Do not
  treat arbitrary `-bot` suffixes as bots; those can be legitimate
  user names.
- sets `project_root` to the repo identifier
  `{provider}:{lower(repo_full_name)}`; it does not copy
  `external_repos.project_root` into events
- determines `occurred_at` per the table in spec §4.4, then
  clamps it into `[now - 7d, now + 1h]`
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
    provider: str,                # 'github' or 'gitea'
    event_type: str,              # 'pull_request', 'push', etc.
    delivery_id: str,             # X-{Provider}-Delivery
    payload: dict,                # parsed JSON body (already
                                  # validated by the route)
    raw_bytes: bytes,             # original body bytes — kept
                                  # only for audit/debug archive
    headers: dict[str, str] | None = None,
) -> None:
    # 0. Webhooks do not pass through the local daemon redaction
    #    layer. Redact parsed payload strings server-side before
    #    archive or normalisation. This is best-effort, not a
    #    replacement for provider-side secret hygiene.
    payload, redaction_hits = redact_payload(payload)

    # 1. ALWAYS archive the redacted parsed delivery first, regardless of
    #    whether we'll write an events row. This gives us a full
    #    forensic trail for events we ignore today and might
    #    decide to ingest later. Per spec §3.5, this table is
    #    service-role-only; LLMs never read it.
    # archive_external_delivery is upsert-shaped; same delivery
    # redelivery returns the existing row id without raising on
    # the (provider, delivery_id) unique constraint. Returns the
    # archive row's id so we can write event_id back at the end.
    archive_id = queries.archive_external_delivery(
        provider=provider,
        delivery_id=delivery_id,
        event_type=event_type,
        raw_body=payload,         # parsed dict; row is jsonb
        raw_headers=_safe_headers(headers or {}),
    )
    logger.info("webhook.archived ... redaction_hits=%s",
                redaction_hits)

    try:
        # 2. Normalize. If event_type is one we don't ingest,
        #    return here — raw is already archived above.
        if provider == "github":
            normalized = normalize_github(event_type, payload)
        elif provider == "gitea":
            normalized = normalize_gitea(event_type, payload)
        else:
            queries.mark_archive_ignored(archive_id,
                                         "unsupported_provider")
            return
        if normalized is None:
            queries.mark_archive_ignored(archive_id,
                                         "unsupported_event_type")
            logger.info("webhook.archive_ignored ... reason=unsupported_event_type")
            return

        if normalized.get("actor", {}).get("is_bot"):
            queries.mark_archive_ignored(archive_id, "bot_actor")
            logger.info("webhook.archive_ignored ... reason=bot_actor")
            return

        source_id = source_id_for_event(normalized)
        if source_id is None:
            queries.mark_archive_ignored(archive_id,
                                         "missing_source_identity")
            logger.info("webhook.archive_ignored ... reason=missing_source_identity")
            return

        # 3. Upsert into events with the compact normalised shape.
        #    source_id is a resource-stable identity, not delivery_id:
        #    PR: pull_request:{repo}:{number}
        #    push: push:{repo}:{ref}:{after}
        #    release: release:{repo}:{tag}
        #    comment: issue_comment:{repo}:{comment_id}
        #    Webhook events have no events.user_id subject. The actor
        #    profile stays inside payload.actor.profile_id.
        #    project_root is the repo identifier, e.g.
        #    github:billc8128/vibelive, not external_repos.project_root.
        #    Idempotency via payload_fingerprint (computed from a
        #    stable subset of the normalised payload — see helper
        #    contract in §2). Returns event_id.
        event_id = queries.upsert_event(
            source=provider,
            source_id=source_id,
            user_id=None,
            project_root=normalized.get("repo", {}).get("project_root"),
            occurred_at=normalized["occurred_at"],
            payload=normalized,
        )

        # 4. Cross-link the archive row to the event row so audit
        #    queries can hop "archive → normalized → notification".
        if event_id is not None:
            queries.link_archive_to_event(archive_id, event_id)
        logger.info("webhook.event_upserted ... event_id=%s source_id=%s",
                    event_id, source_id)
    except Exception:
        queries.mark_archive_ignored(archive_id, "ingest_error")
        logger.exception("webhook.archive_ignored ... reason=ingest_error")
        return


def _safe_headers(headers: dict) -> dict:
    """Strip auth/signing headers before archiving."""
    redact = {"x-hub-signature", "x-hub-signature-256",
              "x-gitea-signature", "authorization", "cookie"}
    return {k: v for k, v in headers.items() if k.lower() not in redact}
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

`payload_fingerprint` is a new nullable column on `events` (added
in plan §1's migration if not already present). For webhook
events: `md5(stable_json(normalised_payload_minus_volatile_fields))`
where the stable subset excludes delivery noise, duplicate
derived repo identifiers, and identity lookup outputs
(`actor.profile_id`, `mentioned_profile_ids`). For turn events:
leave this column NULL in 2.0a. Turn events continue through the existing
`on_turn_to_event` trigger and its `payload_version` semantics;
webhook redelivery is the only path that needs the persisted
fingerprint guard.

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

### 3.4.1 Decider current-notification prefetch

`bot/agent/decider_loop.py::process_event` must prefetch current
notification rows for all `(event_id, subscription_id)` candidate
pairs with `fetch_notifications_for_event_subscription_pairs`
before looping candidates. Do not call `get_notification` once per
subscription; webhook fanout can make that N+1 path visible in
production.

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
  ONE archive row and ONE events row, **payload_version still 1**
  (fingerprint-equal → no-op). Critically: events_needing_decision
  view does NOT re-include this row.
- A different delivery for the same resource-stable source_id
  (e.g. same PR number with edited title) → still ONE events row,
  payload_version=2,
  events_needing_decision DOES re-include this row.
- POST with bad signature → 401, no events row written and no
  external_webhook_deliveries entry.
- POST with fake secret-like strings in PR/comment body →
  `external_webhook_deliveries.raw_body` and `events.payload`
  contain `[REDACTED]`, not the original strings.
- INFO logs include `webhook.accepted`, `webhook.archived`, and
  either `webhook.event_upserted` or `webhook.archive_ignored`;
  logs include ids/reasons but not payload text.

---

## 4. Optional actor attribution helpers (~30 min)

**File**: `bot/db/queries.py`

Add service-role helpers for `external_identities`:

Do **not** expose a self-claim chat tool in 2.0a. A user typing
"my GitHub is X" is not proof of ownership. `external_identities`
is optional actor attribution, not source access, and remains
service-role/admin-managed until OAuth or another verification
flow exists.

**Exit criterion**: admin/service helper can map
`github:billc8128 → bcc.profile_id`; webhook normalisation sets
`payload.actor.profile_id` for actor `billc8128` while
`events.user_id` remains NULL for webhook events;
`build_meta_tools` does not expose `link_external_identity`.

---

## 5. Integrations web UI (~2.5h)

2.0a treats GitHub/Gitea as system-level PMO agent data sources,
not personal account bindings. Ship a dedicated `/integrations`
page so the connection is visible and maintainable without
opening SQL.

**Files**:
- `web/app/integrations/page.tsx` — server component loading
  provider summaries, repo mappings, and safe recent deliveries.
- `web/app/integrations/integrations-panel.tsx` — client
  component for forms, copy buttons, and remove actions.
- `web/app/integrations/actions.ts` — server actions for
  adding/upserting and deleting repo mappings.
- `web/lib/integrations.ts` — validation and safe public mapping
  helpers.
- `web/lib/integration-permissions.ts` — maintainer check.
- `web/app/site-header.tsx` — adds `Integrations` nav.

**UX contract**:
- Signed-in users can view connected providers, repo mappings,
  generated webhook URLs, and recent accepted deliveries.
  Anonymous visitors see the setup shell/login CTA only; the page
  must not create a service-role Supabase client before auth.
- Signed-in maintainers can add/remove repo mappings. The page
  fails closed unless `PMO_INTEGRATION_ADMIN_USER_IDS` or
  `PMO_INTEGRATION_ADMIN_HANDLES` names allowed maintainers.
- Provider secrets and tokens are never shown in the browser.
  They remain in the bot deployment. The page shows webhook URLs
  from `BOT_WEBHOOK_BASE_URL` / `NEXT_PUBLIC_BOT_WEBHOOK_BASE_URL`.
- Raw webhook payloads are never exposed; recent deliveries only
  show provider, event type, repo full name, received_at, and
  whether an `events` row was created. Archive-only deliveries
  translate safe ignored reasons such as `bot_actor` and
  `missing_source_identity` into user-readable status text.
- Optional `external_identities` remains an actor-attribution
  feature for "my PRs" semantics, not the source-access flow.
- Repo full names entered in the UI are lowercased before write
  to match webhook normalisation.

**Exit criterion**:
- `/integrations` renders GitHub and Gitea provider blocks.
- Add mapping `github billc8128/vibelive → /Users/a/Desktop/vibelive`
  from the UI; row appears in `external_repos`.
- Copy `/webhooks/github`, configure provider webhook manually,
  send test delivery; page shows a recent delivery row.
- Removing a mapping deletes the `external_repos` row.

The admin script `backend/scripts/register_external_repos.mjs`
remains as bootstrap/automation fallback for initial setup and
bulk updates.

---

## 6. Investigator enrichment: `fetch_pr_files` tool (~1.5h)

**File**: `bot/agent/investigator.py` and a new
`bot/external/fetch.py` module.

```python
# bot/external/fetch.py
async def fetch_pr_files(provider: str, repo_full_name: str,
                          pr_number: int,
                          paths_filter: list[str] | None = None,
                          head_sha: str | None = None) -> dict:
    """Returns up to 30 files with paths + first 200 chars of patch_excerpt."""
    cache_key = (
        f"{repo_full_name}/{pr_number}/{head_sha}"
        if head_sha else f"{repo_full_name}/{pr_number}"
    )
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
30 files. Each file includes path plus `patch_excerpt` from the
provider's PR files API. It does not fetch full file blobs, so
the investigator must not describe the result as complete file
contents. Remote calls retry transient network errors, HTTP 429,
and 5xx responses with bounded backoff; other 4xx responses fail
fast. The
cache key includes `payload.pr.head_sha` when present, so a PR
force-push does not reuse an old file list. Patch excerpts run
through the same server-side redaction helper before cache write
or tool return.

Add the tool to `bot/agent/investigator.py`'s tool subset:

```python
@tool(
    "fetch_pr_files",
    "Fetch the list of files changed in a GitHub or Gitea PR, "
    "optionally narrowed by paths_filter. Use sparingly — costs "
    "an external API call (cached 24h). Only call when the user's "
    "subscription explicitly requests file/patch details (e.g. "
    "'send spec/plan changes'), not for every PR notification.",
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
        head_sha=payload["pr"].get("head_sha"),
    ))
```

Do not add this tool to renderer. Renderer receives the
investigator brief and must not fetch new facts after the final
notify/suppress decision.

**Files touched**:
- `bot/external/fetch.py` (new)
- `bot/agent/investigator.py` — add tool registration and prompt
  guidance
- `bot/db/queries.py` — `get_event(event_id)` helper added
- `bot/config.py` — `github_api_token`, `gitea_api_url` settings

**Exit criterion**:
- Insert a synthetic `pull_request` event referencing a real PR
- Call `fetch_pr_files` with the event_id → returns file list with
  `patch_excerpt`, not `content_excerpt`
- Fake token-like strings in full patches are redacted before
  `patch_excerpt` truncation, cache write, and tool return
- Repeat call → second call hits cache (no external API call)
- Same PR number with a different head_sha → different cache key
- Call with paths_filter=['spec.md'] → only spec.md returned

---

## 7. Investigator / renderer prompt boundary (~30 min)

Investigator prompt gets a small extension explaining the new
tool:

```
当 brief 里 evidence 包含 GitHub/Gitea PR (events.source in
('github','gitea') AND payload.event_type='pull_request') 时:
- 默认信任 brief 已有的 key_facts，不要每条都拉文件
- 仅当订阅文案明确说"把 spec/plan 改动发给我"或者类似要求时，
  调用 fetch_pr_files 拿对应 patch_excerpt
- fetch_pr_files 是缓存的，重复调用同一 PR 不会重复花钱
- 永远不要超出 brief.evidence_event_ids 列出的 PR
```

Renderer prompt keeps the 1.0c invariant: renderer does not get
`fetch_pr_files`, does not add facts, and only renders the
investigator brief.

**Files**: `bot/agent/investigator.py` /
`bot/agent/renderer.py`. Update tool boundary and prompt text.
Run existing tests to confirm prompt parsing still works.

**Exit criterion**: existing 1.0c tests still pass; manual
inspection of prompt text confirms tool description is
self-explanatory.

---

## 8. End-to-end validation (~1h)

Run the validation scripts from spec §9:

1. Identity claim → event ingestion (spec §9.1)
2. Repo mapping → project lockout (spec §9.2)
3. PR merge → spec/plan patch delivery (spec §9.3)
4. Identity attribution conflict (spec §9.4)
5. Webhook signature failure (spec §9.5)
6. Idempotent re-delivery (spec §9.6)
7. Redaction + observability (spec §9.7)

For tests requiring real webhook delivery, configure GitHub
webhook on a test repo pointing at `pmo-bot.up.railway.app/webhooks/github`
with the test secret.

**Exit criterion**: 7/7 validation scripts pass against a
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
map actors to existing profiles while webhook project identity
uses stable repo identifiers so 1.0c's gatekeeper, investigator,
renderer, and delivery paths keep working.

See docs/specs/2026-05-06-proactive-agent-2.0a-spec.md for the
full behaviour contract; this commit implements §3 ingest, §5
integrations UI + optional identity attribution, §6 fetch_pr_files
investigator enrichment.

Repo mapping is managed from /integrations; the
backend/scripts/register_external_repos.mjs script remains a
bootstrap/bulk-update fallback.
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
   secrets-only-in-env, no logging, rotate if any concern. 2.0a
   uses one global provider secret; per-repo secrets can be added
   later by routing `/webhooks/{provider}/{repo_key}` or storing a
   repo secret hash in `external_repos`.

2. **Identity claim impersonation**. 2.0a disables self-claim
   chat tools; `external_identities` remains service-role/admin
   managed until OAuth or challenge verification exists.

3. **PR diff fetch leakage**. fetch_pr_files reads private repo
   content into our DB cache. Only users with bot DMs can
   trigger it — same trust boundary as turn ingestion. Cache
   has 24h TTL + 7d cleanup; PR titles already in events.payload
   are the bigger surface.

4. **Webhook payload secrets**. Webhooks bypass the daemon's
   local redaction layer. The bot applies server-side best-effort
   redaction before archive/normalisation and logs redaction hit
   counts, but provider-side secret hygiene is still required.

5. **Webhook flood**. A noisy repo pushing many events could
   blow past 1.0c's gatekeeper budget. Monitor decision_logs
   row growth post-deploy; if a single repo's events dominate,
   add a per-source rate limiter. 2.0a intentionally does not
   add a subject-index fast-skip layer; broad rules such as
   "all my PRs" still rely on the gatekeeper as first semantic
   filter.

6. **Repo mapping drift**. `external_repos.project_root` is no
   longer copied into webhook events, so a bad display/admin
   mapping does not corrupt lockout. Repo rename drift still
   matters for the `/integrations` registry and provider webhook
   setup; document the bootstrap script + run the mapping
   verification query (see plan §5). `repo_full_name` is
   lowercased at every entrypoint and enforced by DB CHECK to
   avoid case-only mapping misses.

7. **Repeated PR updates**. Stable `source_id` means one PR maps
   to one events row, but changed payloads still bump
   `payload_version` and can re-enter the gatekeeper. If this is
   noisy in production, aggregate by PR/job window before
   gatekeeper or add subscription subject indexing in a later
   phase.
