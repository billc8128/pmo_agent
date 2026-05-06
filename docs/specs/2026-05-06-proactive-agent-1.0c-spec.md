# Proactive PMO Agent 1.0c — Spec

- **Status**: Draft for implementation
- **Date**: 2026-05-06
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Plan**: [proactive-agent-1.0c-plan.md](2026-05-06-proactive-agent-1.0c-plan.md)
- **Supersedes (partially)**: 1.0a's `decider → notification` direct
  pipeline is replaced; 1.0a's `notifications`, `subscriptions`,
  `feishu_links`, `events`, RPC functions, delivery loop, renderer,
  feishu client, OAuth callback all carry forward unchanged.

This spec describes the third stage of the proactive PMO bot. It is
the **source of truth** for 1.0c's data model changes, decision
authority, and tool contracts. When implementation diverges, update
this file.

---

## 1. Why 1.0c

1.0a's central assumption is "one event = one decision = one
notification". That assumption breaks for two real cases observed
in production:

- **Wrong-project firing**: a turn whose `project_root` is `oneship`
  fires a "vibelive" subscription because the LLM judge confuses
  the subscription's literal project name with the event's actual
  project. The judge sees `description="vibelive 进展告诉我"` and
  doesn't enforce `event.project_root == ".../vibelive"` as a hard
  precondition.
- **Narrative subscriptions are unrepresentable**: subscriptions
  like "监控 vibelive 的播放器方案，有阶段性变化再告诉我" or "如果
  连续几轮都在绕同一个坑告诉我" describe a topic across multiple
  turns. The 1.0a judge sees one turn at a time and can never form
  a "multi-turn story" verdict.

Both stem from the same root: **the decider lacks context**. 1.0c
solves it by separating the decision into two stages with different
amounts of context:

```
event(s) → DECIDER (cheap gatekeeper, single event)
        → investigation_job (the candidate "thread")
        → INVESTIGATOR (PMO agent, reads enough turns/notifications)
        → notification (with structured brief + evidence)
        → RENDERER (prose only, no decisions)
        → Feishu
```

The investigator is the new owner of "should we tell the user?". The
decider only opens jobs. The renderer only writes prose.

---

## 2. Decision authority (architecture invariant #4 made concrete)

| Stage | Decides | Does NOT decide |
|---|---|---|
| Decider | Should we *investigate* this (event, subscription) pair? | Whether the user gets notified |
| Investigator | Whether the user gets notified, what the headline is, what evidence supports it | The exact prose of the message |
| Renderer | The exact prose of the message in Feishu markdown | Whether to send, who to mention, what evidence to use |

The renderer **must not** alter the investigator's structured brief
in ways that change semantics:

- Cannot drop or add evidence event ids
- Cannot change the topic or recommended subject mentions
- Cannot add facts not present in the brief or the cited evidence
- May reorder, condense, translate, beautify

This is enforced at code review and (in 1.0d) by a post-render
verifier that diffs evidence/topic between brief and rendered text.

---

## 3. Data model changes

### 3.1 New table: `investigation_jobs`

```sql
create table public.investigation_jobs (
    id              bigserial primary key,
    subscription_id uuid not null references public.subscriptions(id) on delete cascade,
    status          text not null check (
                        status in (
                          'open',           -- decider has opened, investigator hasn't run
                          'investigating',  -- investigator claimed, running
                          'notified',       -- investigator decided notify=true; notification row created
                          'suppressed',     -- investigator decided notify=false
                          'failed'          -- investigator crashed or timed out, terminal
                        )
                    ),
    seed_event_ids  bigint[] not null default '{}',
    initial_focus   text,                          -- decider's hint for investigator
    decider_reason  text,                          -- decider's reason for opening this job
    investigator_decision jsonb,                   -- structured brief on close (notified/suppressed)
    notification_id bigint references public.notifications(id) on delete set null,
    claim_id        uuid,                          -- investigator lease, mirrors notifications.claim_id
    claimed_at      timestamptz,
    -- Per-job parse-failure budget (mirrors 1.0a's per-pair pattern;
    -- see plan §4 / spec §4.3 for the bump-then-settle logic).
    -- Incremented by `bump_investigation_parse_failure` on each
    -- DecisionParseError. Settles to 'suppressed' at attempt_count=3.
    attempt_count   int  not null default 0,
    last_error      text,
    last_error_at   timestamptz,
    -- LLM cost tracked at the job level (the brief jsonb does NOT
    -- carry usage — it's wrapper-side capture from ResultMessage).
    -- Populated by create_notification_for_investigation_job and
    -- mark_job_suppressed_if_claimed via p_input_tokens / p_output_tokens.
    input_tokens    int,
    output_tokens   int,
    opened_at       timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    closed_at       timestamptz,
    error           text
);

create index investigation_jobs_open_idx
    on public.investigation_jobs (subscription_id, opened_at desc)
    where status in ('open', 'investigating');

create index investigation_jobs_aggregate_window_idx
    on public.investigation_jobs (subscription_id)
    where status = 'open';
```

`seed_event_ids` is an array because **multiple events can append
to one open job** during the aggregation window (§4.2). When the
investigator runs, it sees all of them.

`investigator_decision` is the structured brief. Schema:

```jsonc
{
  "notify": true | false,
  "topic": "vibelive 播放器缓冲策略",
  "evidence_event_ids": [57, 61, 64],
  "subject_user_ids": ["uuid-of-albert"],   // who to @ in groups
  "key_facts": [                            // grounded statements only
    "buffer 从 2MB 提到 5MB",
    "为了首帧延迟问题",
    "配套调了 prefetch 策略"
  ],
  "headline": "albert 把 vibelive 播放器 buffer 调优到 5MB",
  "reason": "连续三轮聚焦同一文件 buffer.ts; 方案从参数调到策略层"
}
```

### 3.2 Changes to `notifications`

Add column linking to the job that produced this notification:

```sql
alter table public.notifications
    add column if not exists investigation_job_id bigint
        references public.investigation_jobs(id) on delete set null;

create index notif_investigation_job_idx
    on public.notifications (investigation_job_id)
    where investigation_job_id is not null;
```

`payload_snapshot` semantics shift slightly: in 1.0a it was the
single event's payload at decision time. In 1.0c it is the
**investigation_decision jsonb** — the structured brief. Renderer
reads this. Old 1.0a notifications keep their old `payload_snapshot`
shape; renderer detects 1.0c shape via the conjunction
`"headline" in payload_snapshot AND "key_facts" in payload_snapshot
AND isinstance(payload_snapshot["key_facts"], list)` (the
canonical predicate, mirrored 1:1 in plan §5's
`_is_1_0c_brief`). Anything else is treated as a legacy 1.0a
payload and rendered with the 1.0a prompt. Using both `headline`
AND `key_facts` (not either-or) avoids false positives on 1.0a
payloads that happen to contain a `headline` field for some
unrelated reason.

### 3.3 Changes to `decision_logs`

Add `investigation_job_id` (nullable for 1.0a-era rows) so the
"why didn't you tell me about X" tool can group decision logs by
job:

```sql
alter table public.decision_logs
    add column if not exists investigation_job_id bigint
        references public.investigation_jobs(id) on delete set null;
```

The decider in 1.0c writes one decision_log row per (event, sub)
pair as before, but now also stamps which job the event was added
to (or "no job opened" if it didn't open one).

### 3.4 Carry-forward (no schema change beyond what's listed above)

- `events` schema unchanged. Trigger unchanged.
- `subscriptions` carries forward 1.0b's `archived_at` column
  (added in 0016). 1.0c adds **one** column to subscriptions:
  `metadata jsonb` (§4.1.1) for the project-name lockout. No
  other subscription column changes.
- `feishu_links` unchanged.
- All RLS policies and security-definer functions from 1.0a/1.0b
  remain valid.
- All RPC functions from 1.0a remain (claim_pending_notifications,
  mark_sent_if_claimed, etc.). They still operate on
  `notifications` and run unchanged in the delivery layer.
  `upsert_notification_row` is **not used by 1.0c's investigator
  path** — that path uses the new
  `create_notification_for_investigation_job` RPC instead.
  `upsert_notification_row` is kept for any straggler 1.0a-shape
  notifications that might get re-decided during the rollover
  window; it can be deleted in a 1.0d cleanup migration once we
  confirm no caller exists.

---

## 4. Pipeline

### 4.1 Decider — gatekeeper

Same loop as 1.0a (`bot/agent/decider_loop.py::process_once`), with
a different decision contract.

For each (event, candidate subscription) pair:

1. **Hard precondition checks** (no LLM, fast skip):
   - Subscription is enabled and not archived (already in 1.0a).
   - `event.ingested_at >= subscription.created_at` (forward-only,
     already in 1.0a per `1223082`). Note: 1.0a uses `ingested_at`,
     not `occurred_at`. We carry that semantics forward unchanged.
   - **Project-name lockout (deterministic, code-level)**:
     this is a synchronous, no-LLM check that runs on every
     decider pair. The check has TWO sources, both required to
     produce a hit before lockout fires:

     1. The set of "known project tokens" K, derived from
        `events.project_root` (rightmost path segment of every
        distinct project_root in the events table, lowercased,
        cached in memory for 60s). This is the universe of real
        project names this deployment has actually seen.
     2. For each subscription, the set of "mentioned tokens" M_sub
        = `K ∩ {tokens that the description names as a project}`.
        "Names as a project" is NOT a naive substring match —
        short tokens (≤3 chars like `c`, `go`, `ai`) would
        otherwise misfire inside words like `bcc`, `again`,
        `campaign`. The matching rules:

        - **Long tokens (≥4 chars, e.g. `vibelive`, `oneship`,
          `pmo_agent`, `feishu`)**: word-boundary match
          (`\bvibelive\b`). Substring-inside-word doesn't hit.
        - **Short tokens (≤3 chars)**: only match when in an
          explicit project-context phrase: `project c`,
          `project-c`, `项目 C`, `项目"C"`, `项目\`c\``, ``\`c\```
          (backtick literal), `"c"` (quoted), or `/c/`/`/c`
          (path segment). Pure occurrence of the letter inside
          another word is NOT a match.

        Implementation in PL/pgSQL function
        `index_subscription_metadata` (plan §2.5.1) — single source
        of truth, called from both bot and web
        below. Cached on `subscriptions.metadata.matched_projects`.

     Lockout rule: if `M_sub` is non-empty AND
     `event.project_root.last_segment.lower() ∉ M_sub`, skip with
     `investigate=false, reason="project_root_lockout"`. No LLM
     call is made for this pair.

     **decision_logs row written for the lockout** uses the
     sentinel `model='deterministic_project_lockout'` (NOT a real
     model name) with `input_tokens=null, output_tokens=null,
     latency_ms=0`. `decision_logs.model` is `NOT NULL` per 1.0a
     schema, so the sentinel is required to make the insert
     succeed. The sentinel is also what the why_no_notification
     tool uses to render "this was filtered by the project lockout,
     no LLM was consulted" in the timeline.

     If `M_sub` is empty (subscription doesn't mention any known
     project), the event passes through to the gatekeeper LLM
     normally — this is the "albert 在干嘛" case, where matching
     by project name doesn't apply.

     **Why this is deterministic and not best-effort**: the
     intersection step only uses real project_root values from
     events the deployment has already seen — no LLM extraction
     needed. The cache on `subscriptions.metadata.matched_projects`
     is just a performance optimisation; if missing, the decider
     re-computes it inline against current K and writes the cache.
     A subscription added "five seconds ago" gets the same lockout
     guarantee as one added "five days ago".
2. **LLM gatekeeper call**: prompt §5.1. Output:
   ```json
   {
     "investigate": true | false,
     "initial_focus": "what the investigator should look at",
     "reason": "why this event might relate"
   }
   ```
   The LLM should err toward `investigate=true` when in doubt
   (false negatives are worse than false positives — the
   investigator can suppress later after reading more context).
3. **If `investigate=true`**: append the event id to an open
   `investigation_job` for this subscription, OR open a new job if
   none exists in the aggregation window (see §4.2).
4. **If `investigate=false`**: write decision_log row only, do
   nothing else. No notification row.
5. **If LLM output won't parse as JSON** (3rd consecutive failure
   for the same `(event_id, subscription_id, payload_version)`):
   write decision_log with `judge_output =
   {"investigate": false, "suppressed_by": "gatekeeper_parse_error",
   "reason": "gatekeeper output parse failed 3 times"}` and
   settle the pair (mark event processed for this version, just
   like a successful `investigate=false`). First two failures: log
   the parse error in decision_logs but leave the pair unprocessed
   so the next loop iteration retries. This mirrors 1.0a's parse
   failure budget pattern and prevents the 1.0a-class infinite
   retry / API-cost-burn bug from coming back.

The decider does NOT write a `notifications` row in 1.0c. That's
the investigator's job.

### 4.1.1 Subscription metadata caching

The `subscriptions.metadata` jsonb column exists ONLY as a cache
of `M_sub` (the intersection of known project tokens with the
subscription description). It is NOT populated by any LLM call;
it is populated by a small string-intersection function in
`bot/db/queries.py`:

```sql
alter table public.subscriptions
    add column if not exists metadata jsonb not null default '{}';
```

```python
# bot/agent/lockout.py (sketch)
def known_project_tokens() -> tuple[set[str], str]:
    """Returns (K, k_hash). K is the set of distinct
    events.project_root last-segments, lowercased, with empty
    tokens filtered out (matching the SQL function exactly —
    plan §2.5 enforces this on both sides via the same query).
    k_hash is a stable digest of sorted(K). TTL 60s.
    """
    if _cache_age() < 60:
        return _cache_K, _cache_hash
    # IMPORTANT: queries.distinct_project_root_tokens() already
    # returns the lowercased, empty-filtered, deduplicated tokens
    # (it runs the same CTE the SQL `index_subscription_metadata`
    # function uses internally — see plan §2.5). DO NOT do a raw
    # `select distinct project_root` and then last_segment() it
    # in Python; that would re-introduce empty tokens for trailing-
    # slash project_roots and break Python/SQL hash equivalence.
    _cache_K = set(queries.distinct_project_root_tokens())
    _cache_hash = sha256("|".join(sorted(_cache_K)).encode()).hexdigest()[:16]
    return _cache_K, _cache_hash

def last_segment(project_root: str | None) -> str:
    """Rightmost path segment, lowercased. Returns '' when input is
    None / empty / has empty trailing segment (e.g. trailing /).
    Always-empty inputs let the lockout fall through to the
    gatekeeper LLM rather than treating them as mismatch.
    """
    if not project_root:
        return ""
    parts = [p for p in project_root.strip().split("/") if p]
    return parts[-1].lower() if parts else ""


def is_project_mismatch(event, sub) -> bool:
    """Lockout check. Matching logic lives in PL/pgSQL function
    `index_subscription_metadata` (plan §2.5.1), the single source
    of truth. Python only reads cached metadata, triggers re-index
    on cache miss / hash mismatch, and applies the membership test.
    """
    # Defensive: events.project_root is nullable. If we don't know
    # what project the event belongs to, we can't apply the lockout
    # — let the gatekeeper LLM judge based on payload content.
    event_token = last_segment(getattr(event, "project_root", None))
    if not event_token:
        return False

    _K, k_hash = known_project_tokens()
    cached = sub.metadata.get("matched_projects")
    cached_hash = sub.metadata.get("project_tokens_hash")
    # Recompute when:
    #  - metadata never written (cached is None)
    #  - K has changed since metadata was written (hash mismatch)
    # In both cases we call the SQL RPC and refetch — there is NO
    # parallel Python matching implementation.
    if cached is None or cached_hash != k_hash:
        queries.index_subscription_metadata(sub.id)
        sub = queries.get_subscription(sub.id)  # refetch
        cached = sub.metadata.get("matched_projects") or []
    if not cached:
        return False
    return event_token not in set(cached)
```

**Single source of truth for matching**: The long token / short
token boundary rules described above in §4.1 step 1 are
implemented in **one place only**: the PL/pgSQL function
`index_subscription_metadata` defined in plan §2.5.1. Python's
`bot/agent/lockout.py` reads cached metadata; web's
`createNotificationRule` server action calls the RPC; both go
through the same SQL. There is no parallel Python or TypeScript
matching implementation — drift is impossible because there's
only one implementation.

**Reindex triggers** (every site that mutates `description`):
- `add_subscription` (chat tool + web rules panel) — call
  `index_subscription_metadata` after the insert
- `update_subscription` (chat tool + web rules panel) — call
  `index_subscription_metadata` after a successful description
  update. `enabled` toggle and `archived_at` set don't change
  description and skip the reindex
- Lazy recompute in `is_project_mismatch` — when K's hash has
  shifted underneath an existing subscription

**Subscription is fully active the moment it's inserted**. No
"async indexing window" during which lockout doesn't apply.
If the K cache is empty (fresh deployment with zero events),
`matched_projects=[]` and lockout doesn't fire — but in that
state there are no events to misfire against, so the safety
property holds vacuously.

**Stale-cache handling**: when a new `project_root` first appears
in events, the 60s K cache expires, k_hash changes, every
subscription's metadata becomes "stale" (hash mismatch). On the
next decider iteration that sees each subscription, lockout calls
the RPC against the new K and refetches. Subscriptions whose
description contains the new token gain lockout protection within
at most one decider iteration of the new project_root showing up.

### 4.2 Aggregation window — when to share a job

Without aggregation, narrative subscriptions can't form. With too
much aggregation, fast-moving subjects feel slow.

Rules:

- For a given `subscription_id`, the decider looks for any
  `investigation_jobs` row with `status = 'open'` and
  `opened_at >= now() - interval '30 minutes'`. If found, append
  the event id to its `seed_event_ids`, update `updated_at`. The
  job stays `open`.
- If no eligible open job exists, open a new one with this event
  as the only seed.
- An open job becomes investigatable when EITHER:
  - it has accumulated 5+ seed events, OR
  - 30 minutes have elapsed since `opened_at`, AND it has at least
    1 seed event.
- A separate "flush" pass (in the same decider loop or a sibling
  loop running every 60s) marks investigatable jobs by either
  leaving them at `status='open'` for the investigator loop to
  pick up, or by simply letting the investigator loop's claim
  query enforce the 30-min OR 5-events condition.

**Concurrency**: `append_to_or_open_investigation_job` is the
only place that opens jobs, and it must be safe under multiple
decider workers running in parallel. The RPC does:

1. `pg_advisory_xact_lock(hashtext('inv_job:' || subscription_id))`
   — serialises this RPC's body per subscription, but doesn't
   block other subscriptions or other RPCs. Released at txn end.
2. SELECT the most recent open job for this subscription within
   the 30-min window. With the advisory lock held, this read +
   subsequent UPDATE/INSERT is a serialisable critical section
   for this one subscription.
3. UPDATE (append to seed_event_ids, dedupe) OR INSERT new job.

The investigator's `claim_investigatable_jobs` uses
`FOR UPDATE SKIP LOCKED` so it doesn't block decider appends; if
an append RPC and a claim RPC race, one of them sees the row
locked and either skips (claim) or waits briefly (append, until
claim's UPDATE commits). The append RPC also re-checks
`status = 'open'` before its UPDATE — once a claim has flipped
the row to 'investigating', the append falls through to opening
a NEW job for the next batch, which is the desired behavior
(events arriving DURING an investigation form the next batch).

We do **NOT** aggregate across subscriptions. Each subscription has
its own independent job stream. (Cross-subscription dedup is a
1.0d concern.)

### 4.3 Investigator — final decision

New loop in `bot/agent/investigator_loop.py`. Polls every **20
seconds**.

Algorithm:

```
async def investigator_loop():
    while True:
        await asyncio.sleep(20)
        # 1. Claim ready-to-investigate jobs (lease pattern, like delivery loop).
        claim_id = uuid4()
        jobs = claim_investigatable_jobs(claim_id, limit=5)
        for job in jobs:
            try:
                brief = await investigate(job)  # LLM agent call, §5.2
                if brief["notify"]:
                    # Single atomic RPC: writes notification AND
                    # flips job to 'notified' in one transaction.
                    # Returns notif_id, or null if lease lost.
                    notif_id = create_notification_for_investigation_job(
                        job_id=job.id, claim_id=claim_id,
                        event_id=most_recent_seed(job),
                        subscription_id=job.subscription_id,
                        decided_payload_version=event_version_at_claim,
                        payload_snapshot=brief,
                        delivery_kind=delivery_kind,
                        delivery_target=delivery_target,
                    )
                    if notif_id is None:
                        log.warning("investigator lost claim on job %s", job.id)
                else:
                    mark_job_suppressed_if_claimed(job.id, claim_id, brief)
            except TransientError:
                release_job_claim(job.id, claim_id)
            except PermanentError as e:
                mark_job_failed_if_claimed(job.id, claim_id, str(e))
```

`claim_investigatable_jobs` is a new RPC, lease-based like
`claim_pending_notifications`. It picks rows where:

```sql
status = 'open'
AND (
  array_length(seed_event_ids, 1) >= 5
  OR opened_at < now() - interval '30 minutes'
)
```

Once claimed, status flips to `'investigating'`, claim_id +
claimed_at stamped. Stale claims (>10 min) reaped each iteration.

**`create_notification_for_investigation_job`** is a new
single-transaction RPC defined in 0017. Both the notification
INSERT and the job state transition (`investigating → notified`)
happen atomically with a lease re-check at the top:

```sql
create function create_notification_for_investigation_job(
    p_job_id          bigint,
    p_claim_id        uuid,
    p_event_id        bigint,
    p_subscription_id uuid,
    p_decided_payload_version int,
    p_payload_snapshot jsonb,    -- the investigator brief
    p_delivery_kind   text,
    p_delivery_target text,
    p_input_tokens    int default null,
    p_output_tokens   int default null
) returns bigint
language plpgsql
security definer
as $$
declare
    new_notif_id bigint;
    locked_job_id bigint;
begin
    -- Lease acquisition + row lock in one step. SELECT FOR UPDATE
    -- on the job row guarantees that between this check and the
    -- final UPDATE below, no concurrent reaper / claim / suppress
    -- can flip the row underneath us. If we don't get the row
    -- (lease lost OR row reaped to pending), return null cleanly
    -- without writing any notification.
    select id into locked_job_id
      from public.investigation_jobs
     where id = p_job_id
       and claim_id = p_claim_id
       and status = 'investigating'
     for update;
    if not found then
        return null;
    end if;

    insert into public.notifications (
        event_id, subscription_id, status,
        delivery_kind, delivery_target,
        decided_payload_version, decided_at,
        payload_snapshot, investigation_job_id
    ) values (
        p_event_id, p_subscription_id, 'pending',
        p_delivery_kind, p_delivery_target,
        p_decided_payload_version, now(),
        p_payload_snapshot, p_job_id
    )
    on conflict (event_id, subscription_id) do update
        set status = excluded.status,
            delivery_kind = excluded.delivery_kind,
            delivery_target = excluded.delivery_target,
            decided_payload_version = excluded.decided_payload_version,
            decided_at = excluded.decided_at,
            payload_snapshot = excluded.payload_snapshot,
            investigation_job_id = excluded.investigation_job_id,
            rendered_text = null, feishu_msg_id = null,
            sent_at = null, error = null,
            claim_id = null, claimed_at = null
        -- Same predicate as 1.0a's upsert_notification_row: never
        -- overwrite a frozen sent/claimed row, AND never let an
        -- older payload_version's investigator decision overwrite
        -- a newer one (could happen if a slow investigation_v1
        -- finishes after a fresher investigation_v2 has already
        -- written a pending notification).
        where public.notifications.status not in ('sent', 'claimed')
          and excluded.decided_payload_version
              > public.notifications.decided_payload_version
    returning id into new_notif_id;

    -- ON CONFLICT ... WHERE may filter the update entirely. Reasons:
    --   (a) existing row is sent/claimed (frozen)
    --   (b) existing row's decided_payload_version >= ours (stale
    --       investigator decision lost the race against a newer one)
    -- In both cases RETURNING yields zero rows and new_notif_id is
    -- null. We must NOT close the job as 'notified' — there's no
    -- notification we own. Instead mark the job suppressed with
    -- reason='delivery_dedup'; the audit trail shows the investigator
    -- decided to send but a frozen-or-newer notification was already
    -- in place.
    if new_notif_id is null then
        update public.investigation_jobs
           set status = 'suppressed',
               investigator_decision =
                   jsonb_set(
                       coalesce(p_payload_snapshot, '{}'::jsonb),
                       '{suppressed_by}', '"delivery_dedup"'::jsonb
                   ) ||
                   jsonb_build_object(
                       'reason',
                       'investigator decided notify=true but the ' ||
                       'notification slot was either frozen ' ||
                       '(sent/claimed) or held a newer version'
                   ),
               input_tokens = p_input_tokens,
               output_tokens = p_output_tokens,
               closed_at = now(),
               updated_at = now(),
               claim_id = null,
               claimed_at = null
         where id = p_job_id
           and claim_id = p_claim_id;
        return null;
    end if;

    update public.investigation_jobs
       set status = 'notified',
           investigator_decision = p_payload_snapshot,
           notification_id = new_notif_id,
           input_tokens = p_input_tokens,
           output_tokens = p_output_tokens,
           closed_at = now(),
           updated_at = now(),
           claim_id = null,
           claimed_at = null
     where id = p_job_id
       and claim_id = p_claim_id;

    return new_notif_id;
end $$;
```

Field semantics:
- `event_id` = the **most recent** seed_event_id (so existing
  delivery-loop dedupe semantics still work)
- `payload_snapshot` = the brief jsonb
- `investigation_job_id` is the new column on `notifications` that
  links back to the job for audit / why_no_notification

`mark_job_suppressed_if_claimed` is similar but only updates the
job; no notifications row is written.

The existing 1.0a delivery loop then claims this row, calls the
renderer, sends to Feishu. **No changes to the delivery layer.**

### 4.4 Renderer — prose only

Same agent invocation pattern as 1.0a's `bot/agent/renderer.py`,
but:

- System prompt rewritten (§5.3) to enforce: "you receive an
  investigator brief, your job is to write 200-400 chars of Feishu
  markdown that conveys exactly the brief's content."
- Renderer no longer "decides" what's relevant. Brief.evidence_event_ids
  is what the prose mentions. Brief.subject_user_ids is who gets
  @-mentioned.
- The `resolve_subject_mention` tool stays — for converting
  brief.subject_user_ids to Feishu open_ids.
- `get_recent_turns` and other read tools stay available but are
  rarely needed; the brief should already contain the key facts.
  (The investigator did the deep-context reading.)

For backward compatibility, if `payload_snapshot` lacks the brief
shape (1.0a-era row), renderer falls back to the 1.0a prompt.

---

## 5. LLM prompts

### 5.1 Decider prompt (gatekeeper)

```
你是 pmo_agent 的事件分流器。给你一条事件、一条候选订阅和它的所有
sibling rules（同 owner 的其他订阅）。

你的任务：判断这条事件是否值得 PMO 助理花时间调查这条订阅。

你不是在判断"是否通知用户"。最终决定权在 investigator 那一步。
你只回答："这件事 plausibly 跟订阅相关吗？"

宁可 false positive 也不要 false negative。如果有合理可能相关，
就 investigate=true，让 investigator 读完更多 context 后自己决定。

但是有几条硬约束必须 false：
1. 订阅 description 里明确写了项目名（vibelive / oneship 等），
   而 event.project_root 完全不沾边 → investigate=false,
   reason="project_root mismatch"。
   注意：如果订阅没写项目名（"albert 在干嘛"），不适用此规则。
2. sibling rules 里有"项目 X 不要"或"凌晨别打扰"且当前命中
   → investigate=false。

输出 JSON：
{
  "investigate": true | false,
  "initial_focus": "建议 investigator 关注什么；不投资就空字符串",
  "reason": "一句话 audit 理由"
}
```

Cost: ~1-1.5k input + 50 output tokens. Same model as 1.0a judge
(ARK Coding Plan).

### 5.2 Investigator prompt

```
你是 pmo_agent 的 PMO 调查员。一条订阅触发了一组事件需要你判断和
撰写。你有完整的只读 PMO 工具集，可以读 turn 详情、项目概览、最近
活动统计、最近通知历史等。

输入：
- subscription.description: 订阅的原始自然语言
- subscription.created_at: 订阅创建时间（早于此的事件不要算证据）
- seed_events: 触发这次调查的事件列表（已经 plausibly 相关）。
  每条 event 都带 user_id（pmo_agent profile UUID），投资人写 brief
  时要把当事人的 user_id 放进 subject_user_ids，渲染器才能 @ 到对应
  飞书人。如果 user_id 为 null（事件主体未绑定），就不要加进
  subject_user_ids，让渲染器降级为文字 @handle。
- recent_notifications_for_this_subscription: 这条订阅最近发过的
  通知（避免短时间内重复发同主题）

你的任务是综合判断：
1. seed_events 加起来够不够"值得通知用户的事"
2. 如果够，topic 是什么、关键事实是什么、谁是事件主体
3. 是否最近已经发过同主题的通知，避免重复

工具使用建议：
- get_recent_turns 拉同 project / 同 user 最近 turns，但**总
  context 不要超过 30 条 turns**
- get_project_overview 拿叙事级摘要
- 历史通知不要调工具：上面输入里的
  `recent_notifications_for_this_subscription` 已经把这条订阅最近
  发过的通知摘要附给你了，直接读那个字段判断重复。
- resolve_people 不可用（这是 read-only investigator 不需要）

输出严格 JSON（schema 见 spec §3.1）：
{
  "notify": bool,
  "topic": "一句话主题",
  "evidence_event_ids": [int],
  "subject_user_ids": [uuid string],
  "key_facts": [string, ...],
  "headline": "用户在飞书看到的开头一句",
  "reason": "为什么这个 notify 决定，包括为什么不是去重，audit 用"
}

如果 notify=false，evidence_event_ids 和 key_facts 仍然填，让
why_no_notification 工具能复盘。

不能：
- 编造没有工具支持的事实
- 在 key_facts 里输出文学化叙述（"美丽地解决了"），只放可验证事实
- 在最终 brief 里包含 user_id (UUID) 之外的内部 ID
```

Cost: ~5-10k input + 500 output tokens per investigation.
Investigations happen far less often than events (one per
aggregated job, not one per event), so total cost is bounded.

Context budget enforcement:
- Max 30 turns from `get_recent_turns` (sum across all calls
  during one investigation)
- Max 10 recent notifications
- Hard timeout: `investigator_max_duration_seconds` (default 90s)

### 5.3 Renderer prompt

```
你是 pmo_agent 的通知 renderer。投资人已经决定要发通知，并写好了
结构化的 brief。你的工作是把 brief 翻译成飞书 markdown 文案，长度
200-400 字。

约束：
- 只用 brief.key_facts 里有的事实。不要补充工具没说的内容。
- evidence_event_ids 不要 echo 给用户（那是给 audit 看的）。
- subject_user_ids: 调 resolve_subject_mention 把 user_id 转成
  Feishu open_id，群通知用 `<at user_id="ou_xxx"></at>`，私聊用
  @display_name 文字。
- headline 作为开头第一句。然后用 1-3 段说明 key_facts。
- reason 不 echo 给用户。
- 不要加 [IMAGE:] 标记。
- 不要输出 JSON，输出 markdown。
```

---

## 6. Migration from 1.0a → 1.0c

This is the trickiest part. Codex must do these in order, in one
deployment:

1. **Schema migration 0017** applies cleanly: adds
   `investigation_jobs` table, the new columns on `notifications`
   and `decision_logs`, the new RPCs.

2. **Decider behavior changes**: `process_event` no longer calls
   `upsert_notification_row` directly. Instead it calls a new
   `append_to_or_open_investigation_job(subscription_id, event_id,
   initial_focus, decider_reason)` RPC. Old in-flight `notifications`
   rows from 1.0a deployment continue through the delivery loop
   normally — they have null `investigation_job_id`, renderer
   detects shape and uses old prompt.

3. **Investigator loop starts**. New background task in `app.py
   lifespan`.

4. **Renderer detects shape**: legacy 1.0a notifications use old
   prompt; new 1.0c notifications use new prompt.

5. **Old decider judge prompt deleted**, replaced with gatekeeper
   prompt. `bot/agent/decider.py::decide` signature changes:
   returns `GatekeeperDecision` instead of `Decision`.

6. **`why_no_notification` tool extended**: in 1.0a it grouped
   decision_logs by `(event_id, subscription_id)`. In 1.0c it
   should ALSO group by `investigation_job_id` when present, and
   surface the investigator's brief as part of the timeline.

After migration is live, 1.0a-shape rows in flight finish through
the existing pipeline. New events fan into investigation_jobs.
Any `notifications` row with `investigation_job_id IS NULL` is
treated as legacy.

---

## 7. Validation criteria (concrete e2e scripts)

### 7.1 Wrong-project firing regression

This is the test that proves the project-name lockout (§4.1) works
**without** depending on LLM behavior. The whole point is that a
prompt-based check is what 1.0a had and it was insufficient.

Setup:
1. User bcc subscribes: "vibelive 项目有进展告诉我". Wait for
   `subscriptions.metadata.matched_projects` to be populated
   (should equal `["vibelive"]` after indexing).
2. albert pushes a turn with `project_root='/Users/.../oneship'`,
   `agent_summary='调整 OneShip workspace 选择器'`.

Assertion:
- One `decision_logs` row exists with
  `judge_output.investigate=false,
   reason="project_root_lockout"`.
- The decider did NOT call the LLM gatekeeper for this pair (we
  can verify by asserting `decision_logs.input_tokens IS NULL`,
  since the lockout short-circuits before any LLM call).
- No `investigation_jobs` row created.
- No `notifications` row created.
- bcc's Feishu DM has no new bot message.

**This test MUST run end-to-end against a real LLM-backed
deployment**. A unit test with a mocked LLM does NOT prove the
fix, because the bug class is "LLM ignores instructions about
literal project name matching" — mocking the LLM out of the test
defeats the test's purpose. The test goes in
`bot/tests/test_proactive_1_0c_e2e.py` and runs against a sandbox
Supabase + the real ARK Coding Plan LLM, OR it's a manual check
in the §8 deploy validation script.

Unit tests are still useful as a faster signal:
- `test_lockout_fires_when_metadata_has_project` — given a
  subscription row with `metadata.matched_projects=["vibelive"]`
  and an event with `project_root='/Users/.../oneship'`, the
  decider's precondition function returns `(skip=True,
  reason="project_root_lockout")` without making any LLM call.
- `test_lockout_does_not_fire_when_metadata_empty` — given
  `metadata.matched_projects=[]`, precondition function lets the
  event through to the gatekeeper (which is then mocked).
- `test_long_token_word_boundary` — K={"vibelive"}; descriptions
  "vibelive 进展告诉我", "/Users/.../vibelive 项目", "I built
  vibelive" all match; descriptions "vibelivexyz progress" and
  "anti-vibelivectomy" do NOT match (would require `\b` violation).
- `test_short_token_requires_project_context` —
  K={"c", "go", "ai"};
  - "项目 C 不要发了" → matched_projects=["c"]
  - "project c notes" → matched_projects=["c"]
  - "/Users/.../c 改了 README" → matched_projects=["c"] (path)
  - "`go` rewrite" → matched_projects=["go"] (backtick)
  - "bcc 在做啥" → matched_projects=[] (no project context for "c")
  - "again 测试" → matched_projects=[] (no project context for "go")
  - "ai 助手" → matched_projects=[] (no project context for "ai")
- `test_short_token_does_not_misfire_on_unrelated_description` —
  K={"c"}; subscription "bcc 在做啥" → matched_projects=[]; an
  event with `project_root='/Users/.../oneship'` arrives → lockout
  does NOT fire (M_sub empty); event passes to LLM gatekeeper.

These unit tests prove the code path AND specifically guard
against the bcc/c, again/go, ai-misfire regressions; the e2e test
proves the indexing actually populates `matched_projects`
correctly against real data.

### 7.2 Narrative subscription positive path

Setup:
1. User bcc subscribes: "监控 vibelive 的播放器方案，有阶段性变化
   再告诉我".
2. albert pushes 5 vibelive turns over 10 minutes:
   - turn 1: "调 buffer 大小"
   - turn 2: "测试 buffer=5MB 效果"
   - turn 3: "buffer 不够，加 prefetch"
   - turn 4: "调试 prefetch race"
   - turn 5: "ship 完成"

Assertions after ≤90s past last turn:
- ONE `investigation_jobs` row in `status=notified`, with
  `seed_event_ids` containing all 5.
- ONE `notifications` row at `status='sent'`, with
  `investigation_job_id` set.
- Feishu DM contains one (not five) message.
- The message mentions buffer + prefetch (multi-turn synthesis).
- albert is `<at>`-mentioned.
- `investigator_decision.evidence_event_ids` ⊇ at least 3 of the
  5 turn ids.

### 7.3 Single weak turn does not fire

Setup:
1. Same subscription as 7.2.
2. albert pushes ONE vibelive turn: "改了一个 typo in README".

Assertions after 35 min:
- ONE `investigation_jobs` row, `status='suppressed'`.
- `investigator_decision.notify=false` and reason mentions weak
  signal / not enough context.
- No `notifications` row created.

### 7.4 Sibling exclusion still works

Setup:
1. bcc has TWO subscriptions:
   - "vibelive 进展告诉我"
   - "项目 C 不要"
2. albert pushes a turn `project_root='/Users/.../C'`.

Assertions:
- Decider sees the C exclusion as a sibling rule and
  `investigate=false`.

### 7.5 Renderer doesn't hallucinate evidence

Setup:
1. Investigator brief has `evidence_event_ids=[57]` and
   `key_facts=["调了 buffer 大小"]`.
2. Run renderer.

Assertions:
- Rendered text contains "buffer".
- Rendered text does NOT mention any other turn_id by id.
- Rendered text does NOT add facts not in `key_facts`.

This is hard to assert automatically (LLM creativity); plan §10 has
a manual review step.

---

## 8. Cost / latency budget

Updated for 1.0c (vs 1.0a §7):

- Daily turn volume: still ~200
- Active subscriptions per person: 3-5
- Active group subscriptions: ~2-3
- **Decider calls/day**: ~200 × 25 = 5000 (same as 1.0a)
- **Decider tokens**: 1k input + 50 output (slightly cheaper than
  1.0a's 1.5k+100, since gatekeeper output is smaller)
- **Investigation jobs/day**: ~50-100 (factor 50× reduction from
  events, due to aggregation + early-stage gatekeeping)
- **Investigation tokens**: 5-10k input + 500 output per call
- **Daily totals**: ~6M decider input + ~700k investigator input
  + ~250k decider output + ~50k investigator output

At ARK Coding Plan rates this is 2-3× more than 1.0a (because
investigations are expensive even though fewer), still well within
plan caps. Cost actively logged in:

- `decision_logs.input_tokens` / `decision_logs.output_tokens` for
  every decider call (already exists in 1.0a schema, populated by
  the gatekeeper code).
- `investigation_jobs.input_tokens` / `output_tokens` columns
  defined in §3.1's create table (not separate alter). Populated
  by the investigator code from the SDK's `ResultMessage.usage`
  (mirroring what 1.0a's `_usage_from_result_message` does in
  `bot/agent/decider.py`). These are NOT part of the LLM's brief
  output — the LLM never sees or writes a `usage` field; the
  wrapper code captures usage from the SDK transport layer and
  the create-notification RPC
  takes them as separate parameters.

The brief schema in §3.1 stays usage-free. Cost lives on the job
row.

Latency target:
- Decider: 30s loop + ~1s/decision = ≤2 min from event to job
- Investigator: 20s loop + 30-60s investigation = ≤3 min from job
  ready to notification pending
- End-to-end (slow path): turn → notification ≤5 min

5 min is acceptable for the proactive use case (this is async by
nature). For breaking-news urgency we'd add a "high-priority"
subscription tier, deferred to 2.0.

---

## 9. Out of scope (1.0c)

Everything from 1.0a §9 still out, plus:

- **Cross-subscription investigation dedup**: if bcc has two subs
  ("vibelive 进展" and "albert 在干嘛") and one event matches both,
  we open two jobs and run two investigations. May produce two
  similar notifications. 1.0d if it becomes a real problem.
- **Investigation chains**: investigator says "I want to look more,
  give me 5 more minutes" and continues. 2.0 idea.
- **User-driven investigation**: "go look into vibelive harder".
  Different feature; 2.0.
- **Renderer verification**: post-render check that brief and prose
  haven't drifted semantically. 1.0d if drift is observed.
- **Aggregation across event sources**: when GitHub webhooks land,
  whether a turn-event and a push-event in the same window can
  share a job. 1.0d (or whatever ships the GitHub webhook).
