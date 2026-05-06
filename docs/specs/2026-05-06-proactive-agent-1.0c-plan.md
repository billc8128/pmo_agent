# Proactive PMO Agent 1.0c — Implementation Plan

- **Status**: Draft, branch `proactive-agent`
- **Date**: 2026-05-06
- **Spec**: [proactive-agent-1.0c-spec.md](2026-05-06-proactive-agent-1.0c-spec.md)
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Predecessor**: 1.0a + 1.0b are deployed. This plan changes the
  decider/notification semantics; it does NOT touch the delivery
  layer, the renderer's tool surface, or the public rules panel.

This is the practical "how to land 1.0c" plan. Spec is the source
of truth for behaviour. This document is the source of truth for
**build order**.

---

## 0. Pre-flight (~10 min)

- [ ] Confirm current branch is `proactive-agent`
- [ ] Confirm latest migration on production is 0016
      (`subscription_archives`)
- [ ] Confirm no uncommitted state from 1.0b that might mask 1.0c
      changes
- [ ] Confirm 1.0a tests still pass (`python -m pytest bot/tests`)
      so we know the baseline

---

## 1. Migration 0017 (~30 min)

**File**: `backend/supabase/migrations/0017_investigation_jobs.sql`

Creates:

- `investigation_jobs` table per spec §3.1, with the full column
  set (no later alters needed):
  - `id`, `subscription_id`, `status`, `seed_event_ids` array
  - `initial_focus`, `decider_reason`, `investigator_decision`
  - `notification_id`, `claim_id`, `claimed_at`
  - **`attempt_count int default 0`, `last_error text`,
    `last_error_at timestamptz`** — parse-failure budget per
    spec §4.3 / plan §4
  - **`input_tokens int`, `output_tokens int`** — LLM usage
    captured from SDK `ResultMessage.usage`, written by the
    success-path RPC and `mark_job_suppressed_if_claimed`
  - `opened_at`, `updated_at`, `closed_at`, `error`
- Indexes: open jobs by subscription, recent open jobs.
- `notifications.investigation_job_id` column + index.
- `decision_logs.investigation_job_id` column.
- `subscriptions.metadata jsonb` column (default `{}`) for the
  project-name lockout (spec §4.1.1).
- New RPC functions:
  - `append_to_or_open_investigation_job(p_subscription_id,
    p_event_id, p_initial_focus, p_decider_reason,
    p_window_minutes int default 30)`:
    **Concurrency-safe** version. Steps inside one transaction:
    1. Acquire a transaction-level advisory lock keyed by
       hashtext('inv_job:' || p_subscription_id::text). This
       serialises append/open per subscription across decider
       workers without blocking other subscriptions.
    2. SELECT the most recent open job for this subscription with
       `opened_at >= now() - make_interval(mins => p_window_minutes)`
       AND `status='open'` (NOT 'investigating' — once an
       investigator has claimed the job, new events open a fresh
       job).
    3. If found: UPDATE that job, appending p_event_id to
       seed_event_ids if not already present, bumping updated_at.
       Returns existing job id.
    4. If not found: INSERT a new job with seed_event_ids=[p_event_id].
       Returns new job id.
    The advisory lock releases at transaction end.
    Python wrapper passes `settings.aggregation_window_minutes`.
  - `claim_investigatable_jobs(p_claim_id, p_limit,
    p_window_minutes int default 30)`:
    lease-based pickup using `FOR UPDATE SKIP LOCKED`, like
    `claim_pending_notifications`. Eligible:
    `status='open' AND (array_length(seed_event_ids, 1) >= 5
    OR opened_at < now() - make_interval(mins => p_window_minutes))`.
    Flips to status='investigating', stamps claim_id + claimed_at.
    Returns each row as 3 jsonb columns plus the event payloads:
    - `investigation_job` jsonb — the job row (post-claim, so
      status='investigating', claim_id set)
    - `subscription` jsonb — the joined subscription
    - `event_payloads` jsonb array — each element is `{id, user_id,
      payload, payload_version, occurred_at, project_root}` for
      one seed event, in seed_event_ids order. `user_id` is the
      events table's top-level column (NOT inside payload jsonb),
      needed by the investigator to populate brief.subject_user_ids
      without an extra query.
    Python wrapper passes `settings.aggregation_window_minutes`
    and deserialises each row into an `InvestigatableJobBundle`
    dataclass with `.job`, `.subscription`, `.events` attributes
    (rename in the dataclass: `events` field corresponds to the
    SQL column `event_payloads`).
  - `create_notification_for_investigation_job(...)` — single
    atomic RPC that writes the notification row AND flips the job
    to 'notified'. Re-checks lease (`claim_id == p_claim_id AND
    status='investigating'`) inside the transaction, so a lost
    lease returns null instead of producing a notification.
    Replaces the previous draft's compose-two-operations approach.
    Full SQL in spec §4.3.
  - `mark_job_suppressed_if_claimed(p_id, p_claim_id, p_brief,
    p_input_tokens default null, p_output_tokens default null)`:
    lease-conditional UPDATE; flips status to 'suppressed', stores
    brief, captures usage tokens on the row, clears claim columns.
  - `release_job_claim(p_id, p_claim_id)`: lease-conditional;
    flips 'investigating' → 'open' so next iteration can re-claim.
  - `mark_job_failed_if_claimed(p_id, p_claim_id, p_error)`:
    terminal; status → 'failed'.
  - `bump_investigation_parse_failure(p_id, p_claim_id, p_error)`:
    lease-conditional; increments `attempt_count`, stores
    `last_error` / `last_error_at`, returns the new attempt count.
  - `reap_stale_job_claims(p_stale_after_minutes default 10)`:
    flips any 'investigating' row stuck >10min back to 'open'.
  - `index_subscription_metadata(p_subscription_id uuid)`:
    populates `subscriptions.metadata.matched_projects` +
    `project_tokens_hash` + `indexed_at` per spec §4.1.1. Full
    PL/pgSQL body in §2.5.1 below. The single source of truth
    for token matching across bot + web. Called after every
    description-touching mutation (add / update) AND on lazy
    recompute when K's hash shifts.
- ACL block: revoke from public/anon/authenticated, grant to
  service_role only — applies to every new RPC listed above. Set
  search_path on every new function. Mirror the pattern from
  1.0a's 0013. `investigation_parse_failure_count` is a Python
  read helper, not an RPC.

**Apply path**: via Supabase Management API (same pattern as
0005-0016).

**Smoke tests** (in transaction, ROLLBACK at end):

Setup: insert real `events` rows (not just synthetic ids) so
`claim_investigatable_jobs`'s join can return real payloads.
Insert a fake profile + subscription as well.

```sql
-- Setup
insert into profiles (id, handle) values ('aaa...', 'fake_user');
insert into events (source, source_id, user_id, project_root,
                    occurred_at, payload)
values
  ('turn', 'fake-1', 'aaa...', '/Users/.../vibelive',
   now(), '{"agent_summary": "first"}'::jsonb),
  ('turn', 'fake-2', 'aaa...', '/Users/.../vibelive',
   now(), '{"agent_summary": "second"}'::jsonb),
  ('turn', 'fake-3', 'aaa...', '/Users/.../vibelive',
   now(), '{"agent_summary": "third"}'::jsonb)
returning id;  -- capture e1, e2, e3
insert into subscriptions (...) values (...) returning id;  -- s
```

1. Call `append_to_or_open_…(s, e1, ...)` → expect new job J1
   with `seed_event_ids=[e1]`, status='open'.
2. Call same with `e2` → expect same job J1 with
   `seed_event_ids=[e1, e2]` (no duplicate).
3. Call again with `e1` (same event) → expect seed_event_ids
   unchanged (dedup).
4. Mock 31 min elapsed (`update investigation_jobs set
   opened_at = now() - interval '31 min' where id=J1`).
5. Call `append_to_or_open_…(s, e3)` → expect a NEW job J2
   (window expired) with `seed_event_ids=[e3]`, opened_at=now().
   J1 still 'open' but stale.
6. Call `claim_investigatable_jobs(uuid_v4(), 5)` → assert
   **exactly one** row returned, J1 (its 2-event count + 31-min
   age both satisfy the eligibility predicate). J2 is NOT
   returned because J2 has only 1 event AND opened_at=now() <
   now()-30min. Verify J2.status is still 'open' afterwards.
   Assert the J1 returned row has:
   - `investigation_job` jsonb (the job row, with status now
     'investigating' and claim_id set)
   - `subscription` jsonb
   - `event_payloads` jsonb array — length 2 with each entry
     `{id, user_id, payload, payload_version, occurred_at,
     project_root}`, in seed_event_ids order (e1 first, e2 second)
   - status flipped to 'investigating' in the DB row
7. Call `mark_job_suppressed_if_claimed(J1, right_claim_id,
   '{"notify": false, "reason": "test"}')` → 1 row affected,
   J1.status='suppressed'.
8. Call same with wrong claim_id → 0 rows, J1 unchanged.
9. Make J2 eligible-and-claimed for the create_notification step:
   ```sql
   update investigation_jobs
      set opened_at = now() - interval '31 min'
    where id = J2;
   ```
   then call `claim_investigatable_jobs(claim_id_2, 5)` → returns
   J2; status='investigating'; capture this as `j2_claim`.
10. Call `create_notification_for_investigation_job(J2, j2_claim,
    e3, s, version, brief, kind, target, null, null)` while J2 is
    'investigating' → returns notif_id; J2 flipped to 'notified',
    notifications row exists with `investigation_job_id=J2`,
    `payload_snapshot=brief`, `decided_payload_version=version`.
11. Call same after J2 is already notified → returns null (lease
    re-check fails because status is no longer 'investigating').
12. **Concurrency stress** (skip if hard to set up in single
    txn): two parallel transactions both call `append_to_or_open_…
    (s, eX, ...)` for a fresh subscription with no open jobs.
    Expect exactly ONE new job created (advisory lock serialises),
    second call appends to the first.
13. ACL: with anon key, call each new RPC → permission
    denied for every one:
    `append_to_or_open_investigation_job`,
    `claim_investigatable_jobs`,
    `create_notification_for_investigation_job`,
    `mark_job_suppressed_if_claimed`,
    `mark_job_failed_if_claimed`,
    `release_job_claim`,
    `reap_stale_job_claims`,
    `bump_investigation_parse_failure`,
    `index_subscription_metadata`.
    `investigation_parse_failure_count` is a Python read helper,
    not an RPC.

**Exit criterion**: all 13 smoke tests pass; ROLLBACK leaves DB
clean.

---

## 2. Bot DB layer additions (~30 min)

**File**: `bot/db/queries.py`

Add wrappers for the new RPCs (one-line `sb_admin().rpc(...)` each):

Decider-side:
- `append_to_or_open_investigation_job(subscription_id, event_id,
  initial_focus, decider_reason)` → returns int job_id

Investigator-side:
- `claim_investigatable_jobs(claim_id, limit)` →
  `list[InvestigatableJobBundle]`
- `create_notification_for_investigation_job(job_id, claim_id,
  event_id, subscription_id, decided_payload_version,
  payload_snapshot, delivery_kind, delivery_target, input_tokens,
  output_tokens)` → returns int notif_id or None (lost lease /
  delivery_dedup; the RPC marks the job suppressed in that case)
- `mark_job_suppressed_if_claimed(id, claim_id, brief,
  input_tokens, output_tokens)` → returns bool
- `mark_job_failed_if_claimed(id, claim_id, error)` → returns bool
- `release_job_claim(id, claim_id)` → returns bool

Parse-failure budget:
- `bump_investigation_parse_failure(id, claim_id, error)` →
  returns int new_attempt_count
- `investigation_parse_failure_count(id)` → returns int
  (read of `investigation_jobs.attempt_count`)

Maintenance:
- `reap_stale_job_claims(stale_after_minutes default 10)` →
  returns int reaped_count

Indexing:
- `index_subscription_metadata(subscription_id)` → returns None.
  Wraps the SQL function from §2.5.1 (the single source of truth
  for project-token matching). Called by add_subscription /
  update_subscription / lazy recompute / web rules panel.

Total: **10 wrappers**, all thin `.rpc()` calls that match SQL
functions defined in 0017.

New dataclass `InvestigatableJobBundle`:

```python
@dataclass
class InvestigatableJobBundle:
    job: InvestigationJob
    subscription: Subscription
    events: list[dict]  # each = {id, user_id, payload,
                        # payload_version, occurred_at,
                        # project_root}. user_id is required so
                        # the investigator's brief can populate
                        # subject_user_ids and renderer can
                        # @-mention via resolve_subject_mention.
    recent_notifications_for_subscription: list[dict]
```

`InvestigationJob` dataclass mirrors the investigation_jobs table
columns from spec §3.1.

**Subscription dataclass extension** (existing class at
`bot/db/queries.py::Subscription` predates 1.0c — it must be
extended to include the new `metadata` and `archived_at` fields,
otherwise `_dataclass_from_row` will silently drop them and
`sub.metadata` will raise AttributeError in lockout):

```python
@dataclass
class Subscription:
    id: str
    scope_kind: str
    scope_id: str
    description: str
    enabled: bool
    created_by: str | None
    chat_id: str | None
    created_at: str
    updated_at: str
    archived_at: str | None       # 1.0b column (added in 0016)
    metadata: dict[str, Any]      # 1.0c column (added in 0017)
                                  # holds matched_projects,
                                  # project_tokens_hash, indexed_at
```

Both `archived_at` and `metadata` MUST be in this list so
`fetch_subscriptions_for_scope`, `fetch_all_enabled_subscriptions`,
`get_subscription` etc. all return them populated.

Also add helper `recent_notifications_for_subscription(
subscription_id, since_hours=72, limit=20)` so the investigator
prompt can include "what we already told this owner about this
subscription recently".

**Exit criterion**: smoke from Python REPL — call `append_to_or_…`
twice with same sub/different events, then `claim_investigatable_…`,
verify shapes match dataclasses.

---

## 2.5 Project-name lockout module (~45 min)

This is the chunk that delivers spec §4.1.1's deterministic
project lockout — the central anti-misfire mechanism for 1.0c.

**Files**:

- `bot/agent/lockout.py` (new). Exports ONLY these three things —
  no matching logic of any kind:
  - `known_project_tokens() -> tuple[set[str], str]` — 60s in-memory
    TTL cache of `(K, k_hash)`. Backed by
    `queries.distinct_project_root_tokens()`. Computed in Python
    purely so the decider loop avoids re-issuing the cheap query
    once per (event, sub) pair. The hash is computed the same way
    as the SQL function, see "hash equivalence" below.
  - `last_segment(project_root: str | None) -> str` — small
    helper, returns the rightmost path segment lowercased, or `""`
    if input is None / empty / has empty trailing segment.
  - `is_project_mismatch(event, sub) -> bool` — reads cached
    metadata; on cache miss / hash mismatch, calls
    `queries.index_subscription_metadata(sub.id)` and refetches.
    Returns True iff cached `matched_projects` non-empty AND
    `last_segment(event.project_root) ∉ set(cached)`. If
    `last_segment(event.project_root) == ""` returns False (let
    the gatekeeper LLM judge).

  **No exports for matching**: there is no
  `_short_token_in_project_context`, no `matched_projects_for`,
  no `_LONG_TOKEN_MIN_LEN` constant in Python. Those live only
  in the SQL function `index_subscription_metadata` (§2.5.1).
  Anyone reaching for those names in Python is wrong.

- `bot/db/queries.py` add helpers:
  - `distinct_project_root_tokens() -> list[str]` — query:
    ```sql
    with seg as (
        select distinct lower(regexp_replace(project_root, '^.*/', '')) as t
          from public.events
         where project_root is not null and project_root <> ''
    )
    select t from seg where t <> '' order by t;
    ```
    Identical to the K computation inside
    `index_subscription_metadata` (§2.5.1) — both must filter
    empty trailing segments to avoid empty tokens producing
    spurious matches in the regex loop.
  - `get_subscription(subscription_id: str) -> Subscription | None`
    — global get, NOT scope-restricted (the existing
    `get_subscription_in_scope` is scope-restricted and the
    lockout's lazy recompute path doesn't have a scope handy).
    Used after `index_subscription_metadata` to refetch metadata.
  - `index_subscription_metadata(subscription_id: str) -> None` —
    one-line `sb_admin().rpc(...)` wrapper around the §2.5.1 RPC.

**Hash equivalence**: Python's `known_project_tokens` and the SQL
function compute the same k_hash. Recipe (BOTH sides must follow
exactly): take the unique lowercased last-segments of all non-empty
`events.project_root`, sort them lexicographically, join with `|`,
sha256-hex, take first 16 chars. Empty-K case: input string is
`""`, sha256(`""`) → fixed value. The hash equivalence test must
pass: insert known events, compare Python and SQL hashes byte-for-byte.

**Single source of truth for matching**: the SQL function
`index_subscription_metadata(subscription_id)` (§2.5.1) is the
ONLY place the long/short boundary matching logic lives. Both
the bot and the web call it via `sb_admin().rpc(...)` after any
description-touching mutation. Python `bot/agent/lockout.py` does
NOT re-implement matching — only reads cached metadata and triggers
the RPC on cache miss / hash mismatch.

Call sites that MUST invoke `index_subscription_metadata` after
their write:

- `bot/agent/tools_meta.py::add_subscription` — after the row is
  inserted via `queries.add_subscription(...)`.
- `bot/agent/tools_meta.py::update_subscription` — only if
  `description` is in the updated fields. Other field changes
  (enabled, etc.) don't affect the lockout match.
- `web/app/notifications/rules/actions.ts::createNotificationRule`
  — after the insert.
- `web/app/notifications/rules/actions.ts::updateNotificationRule`
  — after a successful description update. (The `enabled` toggle
  and the archive action don't touch description and skip
  reindex.)

```python
# bot/db/queries.py
def index_subscription_metadata(subscription_id: str) -> None:
    sb_admin().rpc("index_subscription_metadata", {
        "p_subscription_id": subscription_id,
    }).execute()
```

```ts
// web/app/notifications/rules/actions.ts
await admin.rpc('index_subscription_metadata',
                { p_subscription_id: id });
```

The lazy-recompute path in `bot/agent/lockout.py::is_project_mismatch`
also calls this RPC (NOT a parallel Python implementation):

```python
def is_project_mismatch(event, sub) -> bool:
    # Defensive: events.project_root is nullable. Empty/None →
    # we don't know what project this event belongs to → can't
    # apply lockout, fall through to gatekeeper LLM. This MUST
    # match spec §4.1.1's is_project_mismatch sketch line-for-line.
    event_token = last_segment(getattr(event, "project_root", None))
    if not event_token:
        return False

    _K, k_hash = known_project_tokens()
    cached = sub.metadata.get("matched_projects")
    cached_hash = sub.metadata.get("project_tokens_hash")
    if cached is None or cached_hash != k_hash:
        # SQL is authoritative — call the RPC, then re-read.
        queries.index_subscription_metadata(sub.id)
        sub = queries.get_subscription(sub.id)  # refetch metadata
        cached = sub.metadata.get("matched_projects") or []
    if not cached:
        return False
    return event_token not in set(cached)
```

This means **zero parallel matching code**: the long/short
boundary rules, the regex set, the hash, all live exclusively in
the PL/pgSQL function. Python and TypeScript are reduced to RPC
clients.

### 2.5.1 `index_subscription_metadata` SQL function

Add to migration 0017:

```sql
create function public.index_subscription_metadata(
    p_subscription_id uuid
) returns void
language plpgsql
security definer
as $$
declare
    sub record;
    desc_lower text;
    k_array text[];
    k_hash text;
    matched text[];
    tok text;
begin
    select id, description into sub
      from public.subscriptions where id = p_subscription_id;
    if not found then return; end if;
    desc_lower := lower(coalesce(sub.description, ''));

    -- K = distinct last-segment of events.project_root, lowercased.
    -- Compute last_segment first in a CTE so we can filter out
    -- empties (e.g. when project_root ends with '/' the regex
    -- replace yields ''), then aggregate. Without this filter, an
    -- empty token would enter the regex loop below and produce
    -- nonsense matches. coalesce → empty array if no events yet.
    with seg as (
        select distinct lower(regexp_replace(project_root, '^.*/', '')) as t
          from public.events
         where project_root is not null and project_root <> ''
    )
    select coalesce(array_agg(t), array[]::text[])
      into k_array
      from seg
     where t <> '';

    -- k_hash = first 16 chars of sha256 of sorted-and-joined K.
    -- coalesce(..., '') guards against the empty-K case where
    -- array_to_string returns null and digest(null) is null.
    -- Empty K → consistent empty-K hash, NOT null.
    k_hash := substr(
        encode(extensions.digest(coalesce(array_to_string(
            (select array_agg(t order by t) from unnest(k_array) t),
            '|'
        ), ''), 'sha256'), 'hex'),
        1, 16
    );

    -- Before matching, remove explicit negative/exclusion clauses
    -- from the text used for project-scope extraction. A description
    -- like "只通知 vibelive ... 不要通知其他项目（如 oneship 等）"
    -- must cache ["vibelive"], not ["oneship","vibelive"].
    --
    -- Match: long tokens via word boundary, short tokens via
    -- explicit project-context patterns (project X / 项目 X / `X` /
    -- /X/ / "X"). Each token is regex-escaped before composition;
    -- a project_root last-segment can legitimately contain '.', '+',
    -- '(', etc., and we MUST NOT let those be interpreted as regex
    -- metacharacters (would either misfire or raise).
    matched := array[]::text[];
    declare
        tok_re text;
    begin
        foreach tok in array k_array loop
            -- Escape Postgres POSIX-regex metacharacters: \ ^ $ . | ?
            -- * + ( ) [ ] { }. The \\\\ is intentional — we're
            -- inside SQL string literal that becomes the regex source.
            tok_re := regexp_replace(tok,
                '([\\^$.|?*+(){}\[\]])',
                E'\\\\\\1', 'g');
            if length(tok) >= 4 then
                -- \m and \M are POSIX word-start / word-end anchors.
                if desc_lower ~ ('\m' || tok_re || '\M') then
                    matched := matched || tok;
                end if;
            else
                -- Short token: only match in explicit project context.
                if desc_lower ~ ('\mproject[\s\-_:]*' || tok_re || '\M')
                   or desc_lower ~ ('项目[\s\-_:''`"]*' || tok_re ||
                                     '($|[\s''`"])')
                   or desc_lower ~ ('`' || tok_re || '`')
                   or desc_lower ~ ('/' || tok_re || '(/|$|[^a-z0-9])')
                   or desc_lower ~ ('"' || tok_re || '"')
                then
                    matched := matched || tok;
                end if;
            end if;
        end loop;
    end;

    -- Always write a JSON array (even if empty) and a real hash
    -- string (even for empty K). `matched_projects = null` would
    -- be read as Python None and trigger lazy recompute every
    -- decider iteration; `[]` is the correct "I checked, nothing
    -- matched" signal.
    update public.subscriptions
       set metadata = coalesce(metadata, '{}'::jsonb) ||
                      jsonb_build_object(
                          'matched_projects',
                              coalesce(
                                  (select to_jsonb(array_agg(t order by t))
                                     from unnest(matched) t),
                                  '[]'::jsonb
                              ),
                          'project_tokens_hash', k_hash,
                          'indexed_at', now()::text
                      )
     where id = p_subscription_id;
end $$;
```

ACL: revoke from public/anon/authenticated, grant to service_role.
search_path pinned to `public, pg_temp`.

The bot's `lockout.py` and web's server action both call
`sb_admin().rpc("index_subscription_metadata", ...)` after every
add_subscription. The bot also calls it on lazy recompute (cache
miss / hash mismatch in `is_project_mismatch`).

**Unit tests** (run against the SQL function via psql / supabase
test harness, since the matching is now SQL-only):
- `test_long_token_word_boundary` — K={"vibelive"}; descriptions
  `vibelive 进展` / `/Users/.../vibelive` / `I built vibelive`
  match; `vibelivexyz` and `pre-vibelivectomy` don't.
- `test_short_token_requires_project_context` —
  K={"c","go","ai"}; verify `项目 C` / `project c` / `/c/` /
  ``\`go\``` / `"ai"` match; bare `bcc`, `again`, `ai 助手` don't.
- `test_short_token_does_not_misfire_on_unrelated_description` —
  K={"c"}, sub "bcc 在做啥" → `matched_projects=[]`. Critical
  regression: a real user handle must not get hard-skipped.
- `test_index_subscription_metadata_idempotent` — calling the SQL
  function twice yields the same `matched_projects` and
  `project_tokens_hash`. `indexed_at` may refresh and is not part
  of this equality assertion.
- `test_index_subscription_metadata_no_events` — when events
  table is empty: `matched_projects=[]` (JSON array, NOT null),
  `project_tokens_hash` is a real 16-char hex string (NOT null).
  Reading metadata from Python: `cached is not None`, so lazy
  recompute does NOT re-fire on every decider iteration.
- `test_token_with_regex_metacharacters` — insert a fake event
  with `project_root='/Users/.../c++.proj'` (period + plus signs
  in the last segment). K contains `c++.proj`. Subscription
  `c++.proj 进展告诉我` should match; subscription `cxx-proj
  进展` should NOT match. Without the regex escape, `+` and `.`
  would corrupt the regex and either raise or misfire.
- `test_negative_project_examples_do_not_expand_scope` — K contains
  `vibelive` and `oneship`; subscription
  `只通知 vibelive 项目的进展，不要通知其他项目（如 oneship 等）`
  should cache `matched_projects=["vibelive"]`. `oneship` is an
  exclusion example, not an allowed project.
- `test_description_update_reindexes` — create sub with
  description "vibelive 进展", verify metadata has
  `matched_projects=["vibelive"]`. Update description to
  "oneship 进展", verify update_subscription / updateNotificationRule
  re-call index_subscription_metadata so metadata becomes
  `["oneship"]`, NOT stuck at `["vibelive"]`.
- `test_empty_last_segment_filtered` — insert events with these
  project_roots: `'/Users/.../vibelive'`, `'/Users/.../'` (trailing
  slash), `'/'`, `''` (empty), and `null`. Assert K computed by
  `index_subscription_metadata` and `distinct_project_root_tokens`
  contains exactly `["vibelive"]` — no empty string token.
- `test_event_with_null_project_root_skips_lockout` — Python-side
  test: `is_project_mismatch(event_with_null_project_root, sub)`
  returns False even when `sub.metadata.matched_projects` is
  non-empty. The event passes through to the gatekeeper LLM
  rather than being hard-skipped.
- `test_payload_project_path_leaf_counts_as_project_token` —
  event has `project_root='/Users/.../vibe'` but payload
  `project_path='/Users/.../vibe/vibelive'`; subscription
  metadata has `matched_projects=["vibelive"]`; lockout must not
  skip. This protects repos whose canonical root is a parent folder.
- `test_python_sql_hash_equivalence` — call
  `lockout.known_project_tokens()` and the SQL function on the
  same data; assert k_hash byte-equals between Python and SQL.
  Catches drift in the hash recipe (sort order, separator,
  algorithm, hex prefix length, empty-K handling).

**Exit criterion**: all 10 unit tests pass; one Python REPL trial
inserting a vibelive event + a sub with description "vibelive 进展
告诉我" + an unrelated sub "bcc 在干嘛" results in
`subscriptions.metadata.matched_projects = ["vibelive"]` for the
first AND `[]` for the second.

---

## 3. New decider behavior — gatekeeper (~45 min)

**Files**:
- `bot/agent/decider.py`: new `gatekeeper_decide(event, candidate,
  siblings)` function. Returns `GatekeeperDecision` dataclass with
  `investigate: bool`, `initial_focus: str`, `reason: str`,
  `raw_input/raw_output/latency_ms/tokens/model`.
- Old `decide()` function deleted (no callers after this slice).
- `bot/agent/decider_loop.py::process_event` rewrite:
  - Replace `decide(...)` call with `gatekeeper_decide(...)`.
  - On `investigate=true`: call
    `queries.append_to_or_open_investigation_job(...)`, log result
    to decision_logs with `investigation_job_id` set.
  - On `investigate=false`: write decision_log only, no other state
    change.
  - Hard preconditions BEFORE the LLM call (each fast-skips the
    pair if it fires; no LLM call, no investigation_jobs row):
    - subscription enabled + not archived (already in 1.0a)
    - `event.ingested_at >= subscription.created_at` (1.0a forward
      semantics)
    - **`lockout.is_project_mismatch(event, sub)` from §2.5**.
      When True, write a decision_log row with sentinel
      `model='deterministic_project_lockout'`,
      `judge_output={"investigate": false, "reason":
      "project_root_lockout"}`, `input_tokens=null,
      output_tokens=null, latency_ms=0`, then `continue` to the
      next pair. This is the chunk that delivers the wrong-project
      regression fix; missing this is what made 1.0a misfire.
  - Remove all references to `upsert_notification_row` from the
    decider's call path.

**Prompt**: paste spec §5.1 verbatim into a module-level constant
`_GATEKEEPER_PROMPT`. Reuse the JSON parsing helper from 1.0a (it
already handles fenced/unfenced JSON).

**Exit criteria** (all must pass in
`bot/tests/test_proactive_1_0c.py`):

1. `test_decider_opens_job`: mock LLM returns `investigate=true`,
   `process_event` opens one investigation_jobs row containing the
   event id in seed_event_ids; one decision_log row with
   `investigation_job_id` set; `events.processed_at` IS set with
   matching processed_version.

2. `test_decider_skips_on_lockout`: subscription has
   `metadata.matched_projects=["vibelive"]`; event has
   `project_root='/Users/.../oneship'`; assert no LLM call was
   made (`decision_logs.input_tokens IS NULL`); no
   investigation_jobs row created; one decision_log with
   `judge_output.reason='project_root_lockout'`; events row IS
   marked processed (this is a settled pair, not a retry).

3. `test_decider_handles_investigate_false`: mock LLM returns
   `investigate=false`; assert decision_log written but no
   investigation_jobs row; events processed.

4. `test_decider_handles_parse_failure_budget`: mock LLM returns
   garbage 3 times for the same (event, sub, version); assert
   first 2 failures DO NOT mark event processed (so retry can
   happen); third failure DOES settle the pair as
   `gatekeeper_parse_error`; assert no infinite retry loop.

5. `test_decider_idempotent_on_existing_job`: pre-create an
   investigation_job for this subscription with seed_event_ids=
   [event_id]; run `process_event` for the same event; assert
   the job's seed_event_ids is unchanged (not duplicated); no new
   job opened.

6. `test_decider_partial_failure_leaves_event_unprocessed`:
   subscription A's LLM call succeeds, subscription B's LLM call
   raises; assert events.processed_at IS NULL (whole event left
   for next iteration); assert A still got its
   investigation_jobs row (we don't roll back successful pairs).

---

## 4. Investigator loop (~1.5h)

**File**: `bot/agent/investigator_loop.py` (new)

Wires spec §4.3 + §5.2.

Skeleton:

```python
async def investigator_loop():
    while True:
        try:
            await asyncio.sleep(settings.investigator_loop_interval_seconds)
            queries.reap_stale_job_claims()
            await process_once(limit=5)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("investigator iteration failed")
            await asyncio.sleep(60)


async def process_once(limit: int = 5) -> int:
    claim_id = str(uuid.uuid4())
    bundles = queries.claim_investigatable_jobs(claim_id, limit)
    for bundle in bundles:
        try:
            # investigate() returns (brief, usage) where usage is
            # {input_tokens, output_tokens} captured from the SDK's
            # ResultMessage. Token capture is wrapper-side, NOT in
            # the LLM's brief output.
            brief, usage = await investigate(bundle)
            if brief.get("notify"):
                # ONE atomic RPC writes the notification AND flips
                # the job to 'notified' inside one txn. Returns the
                # new notif id, or None if the lease was lost OR
                # the (event, sub) pair already had a frozen
                # sent/claimed notification (in which case the RPC
                # marks the job suppressed/delivery_dedup itself).
                notif_id = queries.create_notification_for_investigation_job(
                    job_id=bundle.job.id,
                    claim_id=claim_id,
                    event_id=most_recent_seed_id(bundle),
                    subscription_id=bundle.job.subscription_id,
                    decided_payload_version=most_recent_seed_version(bundle),
                    payload_snapshot=brief,
                    delivery_kind=delivery_kind,
                    delivery_target=delivery_target,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                if notif_id is None:
                    logger.warning("investigator lost claim job=%s", bundle.job.id)
            else:
                queries.mark_job_suppressed_if_claimed(
                    bundle.job.id, claim_id, brief,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
        except asyncio.CancelledError:
            raise
        except DecisionParseError as e:
            # Track parse failures via investigation_jobs.error +
            # decision_logs entries. After 3 consecutive parse
            # failures across investigator runs of the same job,
            # mark the job suppressed with notify=false,
            # reason='investigator_parse_error' and settle.
            queries.bump_investigation_parse_failure(
                bundle.job.id, claim_id, str(e)[:500]
            )
            failures = queries.investigation_parse_failure_count(bundle.job.id)
            if failures >= 3:
                queries.mark_job_suppressed_if_claimed(
                    bundle.job.id, claim_id,
                    {"notify": False,
                     "suppressed_by": "investigator_parse_error",
                     "reason": "investigator output parse failed 3 times"},
                )
            else:
                # Release back to open so a fresh claim retries.
                queries.release_job_claim(bundle.job.id, claim_id)
        except TransientInvestigatorError:
            queries.release_job_claim(bundle.job.id, claim_id)
        except asyncio.TimeoutError:
            # 90s investigator timeout. Treated like a parse
            # failure: bump attempt_count, retry until budget
            # exhausted, then settle as suppressed/timeout. Don't
            # mark_failed on first timeout — a slow LLM call
            # shouldn't permanently terminate the job.
            queries.bump_investigation_parse_failure(
                bundle.job.id, claim_id, "investigator timeout (>90s)"
            )
            failures = queries.investigation_parse_failure_count(
                bundle.job.id
            )
            if failures >= 3:
                queries.mark_job_suppressed_if_claimed(
                    bundle.job.id, claim_id,
                    {"notify": False,
                     "suppressed_by": "investigator_timeout",
                     "reason": "investigator timed out 3 times"},
                    input_tokens=None, output_tokens=None,
                )
            else:
                queries.release_job_claim(bundle.job.id, claim_id)
        except Exception as e:
            # True crash (network panic, etc.) — terminal.
            logger.exception("investigator crashed for job=%s", bundle.job.id)
            queries.mark_job_failed_if_claimed(bundle.job.id, claim_id, str(e))
    return len(bundles)
```

Note: `attempt_count` is shared between parse failures and
timeout failures. Both are "the investigator didn't complete
successfully on this attempt"; either way 3 strikes settles the
pair as suppressed. The `last_error` column distinguishes the
specific cause for audit.

`investigate(bundle)` is the LLM agent call. Same machinery as the
renderer's one-shot agent (see `bot/agent/renderer.py` for pattern):

- ClaudeAgentOptions with read-only tool subset:
  list_users, lookup_user, get_recent_turns, get_project_overview,
  get_activity_stats, today_iso (NO resolve_subject_mention,
  NO renderer-only tools)
- system_prompt = §5.2 verbatim
- max_turns = 6 (enough for 2-3 tool round-trips + final JSON)
- Hard timeout via `asyncio.wait_for(
  settings.investigator_max_duration_seconds=90)`
- Output parsing: same JSON extractor as 1.0a's decider, raises
  `DecisionParseError` on bad JSON. The loop catches this and
  uses the parse-failure budget logic above.

**Tracking parse failures**: the `attempt_count`, `last_error`,
`last_error_at` columns on `investigation_jobs` (defined in the
§3.1 create table, listed above in plan §1's column set) hold the
budget state.

`bump_investigation_parse_failure(job_id, claim_id, error)` is a
small RPC that increments `attempt_count`, stores the latest
error message in `last_error` / `last_error_at`. Lease-checked
(only fires if `claim_id = p_claim_id AND status =
'investigating'`). The Python wrapper truncates `error` to 500
chars before passing — full LLM output stays in decision_logs.
`investigation_parse_failure_count(job_id)` is a plain read of
`attempt_count`.

This replaces the "track via investigator_decision shape" hand-wave
in the previous draft, which was unimplementable because
`investigator_decision` is only set on close.

**Files touched in this chunk**:
- `bot/agent/investigator_loop.py` (new)
- `bot/agent/investigator.py` (new) — the `investigate(bundle)`
  function and dataclass
- `bot/agent/decider_loop.py` — already touched in §3
- `bot/db/queries.py` — wrappers added in §2's list, used here for
  `create_notification_for_investigation_job`,
  `mark_job_suppressed_if_claimed`, `release_job_claim`,
  `mark_job_failed_if_claimed`, `bump_investigation_parse_failure`,
  `investigation_parse_failure_count`
- `bot/config.py` — add:
  - `investigator_loop_interval_seconds: int = 20`
  - `investigator_max_duration_seconds: int = 90`
  - `investigator_max_turns: int = 6`
  - `investigator_max_turns_context: int = 30`
  - `aggregation_window_minutes: int = 30` — read by
    `append_to_or_open_investigation_job`'s "open job within
    window" predicate AND `claim_investigatable_jobs`'s
    "opened_at + window" eligibility predicate. Both Python wrapper
    and the SQL functions take this as a parameter so the value
    lives in one place. Risk-table item §11.1 plans to tune this
    after a week of real usage.

**Exit criterion**:
- `pytest bot/tests/test_proactive_1_0c.py::test_investigator_writes_notification`
  passes.
- Local sandbox: insert one fake job with 5 fake seed events,
  start the loop, observe a notifications row written within 30s
  with `payload_snapshot` containing brief shape.

---

## 5. Renderer dual-mode (~30 min)

**File**: `bot/agent/renderer.py`

The renderer must handle BOTH 1.0a-shape and 1.0c-shape
notifications. Detection:

```python
def _is_1_0c_brief(payload_snapshot: dict | None) -> bool:
    if not payload_snapshot:
        return False
    return (
        "headline" in payload_snapshot
        and "key_facts" in payload_snapshot
        and isinstance(payload_snapshot.get("key_facts"), list)
    )
```

If True: use `_RENDERER_PROMPT_1_0C` (spec §5.3).
If False: use the existing `_RENDERER_PROMPT` (1.0a behavior).

The 1.0c prompt is shorter and forbids changing the brief; the
1.0a prompt is unchanged.

The tool subset is the same in both modes: list_users, lookup_user,
get_recent_turns, get_project_overview, get_activity_stats,
today_iso, resolve_subject_mention.

**Exit criterion**: feed both shapes to the renderer, verify the
right prompt fires, both produce non-empty markdown.

---

## 6. Wire investigator loop into app lifespan (~10 min)

**File**: `bot/app.py`

Import investigator_loop, add `asyncio.create_task(
investigator_loop.run_forever())` to lifespan startup, with the
same cancellation pattern as the existing decider/delivery loops.

**Exit criterion**: `python -m bot.app` (or equivalent local
runner) starts the bot with three loops visible in logs.

---

## 7. why_no_notification 1.0c-aware (~30 min)

**File**: `bot/agent/tools_meta.py::why_no_notification`

Extend the tool to also surface investigation_job records when
the failed pair has them. New return shape includes:

```jsonc
{
  "matches": [{
    "event_id": ...,
    "subscription_id": ...,
    "subscription_description": "vibelive 进展告诉我",
    "investigation_job_id": 42 | null,   // NEW
    "investigator_decision": {...} | null, // NEW (job's brief)
    "current_notification": {...},
    "timeline": [...]   // existing decision_log timeline
  }]
}
```

When `investigation_job_id` is set, the agent can explain to the
user "I opened a job, the investigator looked at 5 events, decided
not to notify because X". For 1.0a-era pairs (no job), behavior
unchanged.

**Exit criterion**: a hand-built scenario where investigator
suppressed a job, asking "why didn't you tell me about X" returns
a coherent timeline including the investigator's reason.

---

## 8. End-to-end validation (~1h)

Run the validation scripts from spec §7 against a real
deployment:

1. **§7.1 wrong-project firing regression** — manually insert turns
   to ensure project mismatch is filtered at gatekeeper layer.
2. **§7.2 narrative subscription positive path** — let albert run
   real vibelive turns or simulate them, observe one consolidated
   notification.
3. **§7.3 single weak turn does not fire** — verify 35-min wait
   produces a suppressed job, no notification.
4. **§7.4 sibling exclusion** — same as 1.0a but verify it's
   enforced at gatekeeper.
5. **§7.5 renderer faithfulness** — manual eyeballing of one
   rendered notification: does it contain only `key_facts`
   content?

If §7.2 fails (the core narrative case), this is a prompt issue;
iterate the investigator prompt before considering 1.0c done.

**Exit criterion**: 5/5 validation scripts pass. Any failure on
§7.1 or §7.4 is a hard blocker (spec violation). §7.2/3/5 failures
mean iterating prompts, not architecture.

---

## 9. Roadmap update (~10 min)

Mark 1.0c done in the roadmap §2:
- Move 1.0c bullet from "future" to "deployed"
- Update validation criteria to point at this plan's §8
- Add a "lessons learned" subsection if there were any prompt
  iterations

---

## 10. Commit + push

Single commit on `proactive-agent` branch:

```
1.0c: investigation-driven proactive PMO

Replaces the 1.0a single-event judge with a two-stage decision
pipeline: a cheap gatekeeper opens investigation jobs, and a PMO
investigator agent reads enough context across multiple seed
events before deciding whether to notify. The renderer becomes
prose-only and consumes the investigator's structured brief.

See docs/specs/2026-05-06-proactive-agent-1.0c-spec.md for the
full behavior contract; this commit implements §3-§5 end-to-end
plus the migration in §6.
```

Push, deploy via Railway, run §8 validation against production,
update roadmap.

---

## Cut points (if time-pressured)

- **Skip §7 (why_no_notification 1.0c-aware)**: legacy 1.0a
  behavior keeps working, just doesn't surface investigation
  decisions yet. Add later.
- **Skip §5 dual-mode renderer fallback**: but only if you're
  willing to invalidate every in-flight 1.0a notification. Risky;
  not recommended.
- **Skip §3 hard precondition checks**: revert to 1.0a's "let the
  LLM judge it all". This re-introduces the wrong-project firing
  bug. Don't cut this.

Don't cut: 0017 migration, gatekeeper rewrite, investigator loop,
notifications.investigation_job_id link, renderer dual-mode.
That's the irreducible 1.0c.

---

## Risks specific to 1.0c rollout

1. **Aggregation window starves**: if 30 min is too long, narrative
   subs feel slow. If too short, multi-turn stories don't form.
   Plan: make `aggregation_window_minutes` a config setting; start
   at 30, observe for a week, adjust.
2. **Investigator hallucinates evidence**: the `key_facts` list
   contains things not actually in the cited events. Plan: §8 step
   5 is the manual check. If it happens regularly, add a
   post-investigation verifier in 1.0d.
3. **Investigator timeouts**: 90s budget is tight if the agent
   does many tool calls. Plan: log latency per investigation;
   if >50% hit timeout, raise the budget; if <10% hit it, narrow
   the budget to save money.
4. **Cost spike**: aggregation is supposed to reduce cost (one
   investigation per thread, not one decision per event), but if
   threads form too easily, total invocations could rise. Plan:
   `decision_logs` and the new `investigation_jobs` rows let us
   compute cost per day; alert if >2× pre-1.0c baseline for >24h.
