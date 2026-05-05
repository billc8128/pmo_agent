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

- `investigation_jobs` table per spec §3.1, with status enum,
  seed_event_ids array, lease columns (claimed_by/claimed_at).
- Indexes: open jobs by subscription, recent open jobs.
- `notifications.investigation_job_id` column + index.
- `decision_logs.investigation_job_id` column.
- New RPC functions:
  - `append_to_or_open_investigation_job(p_subscription_id,
    p_event_id, p_initial_focus, p_decider_reason)`:
    finds an open job in the 30-min aggregation window for this
    subscription; if found, appends event id to seed_event_ids
    (deduplicating); if not, opens a new job with this event as
    the only seed. Returns job id.
  - `claim_investigatable_jobs(p_claim_id, p_limit)`:
    lease-based pickup, like `claim_pending_notifications`.
    Eligible: status='open' AND (5+ seed events OR opened_at <
    now() - 30 min). Flips to status='investigating', stamps
    claim_id + claimed_at. Returns rows joined with subscription
    description and array of event payloads.
  - `mark_job_notified_if_claimed(p_id, p_claim_id, p_brief,
    p_notification_id)`: lease-conditional UPDATE.
  - `mark_job_suppressed_if_claimed(p_id, p_claim_id, p_brief)`.
  - `release_job_claim(p_id, p_claim_id)`.
  - `mark_job_failed_if_claimed(p_id, p_claim_id, p_error)`.
  - `reap_stale_job_claims(p_stale_after_minutes default 10)`.
- ACL block: revoke from public/anon/authenticated, grant to
  service_role only. Set search_path on every new function.
  Mirror the pattern from 1.0a's 0013.

**Apply path**: via Supabase Management API (same pattern as
0005-0016).

**Smoke tests** (in transaction, ROLLBACK at end):

1. Insert fake subscription. Call `append_to_or_open_…` with fake
   event 1 → expect new job, seed_event_ids=[1].
2. Call again with event 2 → expect same job, seed_event_ids=[1,2].
3. Wait-skip 31 min (mock by setting opened_at backwards) → call
   again with event 3 → expect new job, seed_event_ids=[3].
4. Call `claim_investigatable_jobs` on first job (which now has 2
   seeds, opened 31 min ago) → expect it returned, status flipped
   to 'investigating'.
5. Call `mark_job_suppressed_if_claimed` with right claim_id →
   row updated to 'suppressed'. Call again with wrong claim_id →
   no-op.
6. ACL: with anon key, call any of these RPCs → permission denied.

**Exit criterion**: all 6 smoke tests pass; ROLLBACK leaves DB
clean.

---

## 2. Bot DB layer additions (~30 min)

**File**: `bot/db/queries.py`

Add wrappers for the 6 new RPCs (one-line `sb_admin().rpc(...)`):

- `append_to_or_open_investigation_job(...)` returns int job_id
- `claim_investigatable_jobs(claim_id, limit)` returns
  `list[InvestigatableJobBundle]`
- `mark_job_notified_if_claimed(...)` returns bool (lease ok)
- `mark_job_suppressed_if_claimed(...)` returns bool
- `release_job_claim(...)` returns bool
- `mark_job_failed_if_claimed(...)` returns bool
- `reap_stale_job_claims()` returns int

New dataclass `InvestigatableJobBundle`:

```python
@dataclass
class InvestigatableJobBundle:
    job: InvestigationJob
    subscription: Subscription
    events: list[dict]  # event payload dicts with id+payload+
                        # payload_version+occurred_at+project_root
    recent_notifications_for_subscription: list[dict]
```

`InvestigationJob` dataclass mirrors the table columns.

Also add helper `recent_notifications_for_subscription(
subscription_id, since_hours=72, limit=20)` so the investigator
prompt can include "what we already told this owner about this
subscription recently".

**Exit criterion**: smoke from Python REPL — call `append_to_or_…`
twice with same sub/different events, then `claim_investigatable_…`,
verify shapes match dataclasses.

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
  - Hard preconditions BEFORE the LLM call:
    - subscription enabled+not archived (already in 1.0a)
    - `event.occurred_at >= subscription.created_at` (1.0a forward
      semantics)
  - Remove all references to `upsert_notification_row` from the
    decider's call path.

**Prompt**: paste spec §5.1 verbatim into a module-level constant
`_GATEKEEPER_PROMPT`. Reuse the JSON parsing helper from 1.0a (it
already handles fenced/unfenced JSON).

**Exit criterion**: tests in
`bot/tests/test_proactive_1_0c.py::test_decider_opens_job` —
inject a mock LLM that returns `{"investigate": true}`, run
`process_event` against a fake event + sub, assert one
`investigation_jobs` row exists with the event in seed_event_ids.

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
            brief = await investigate(bundle)  # §5.2 prompt
            if brief.get("notify"):
                notif_id = create_notification_for_job(bundle, brief)
                queries.mark_job_notified_if_claimed(
                    bundle.job.id, claim_id, brief, notif_id,
                )
            else:
                queries.mark_job_suppressed_if_claimed(
                    bundle.job.id, claim_id, brief,
                )
        except asyncio.CancelledError:
            raise
        except TransientInvestigatorError:
            queries.release_job_claim(bundle.job.id, claim_id)
        except Exception as e:
            logger.exception("investigator crashed for job=%s", bundle.job.id)
            queries.mark_job_failed_if_claimed(bundle.job.id, claim_id, str(e))
    return len(bundles)
```

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
- Output parsing: same JSON extractor as decider; if parse fails
  3 times for the same job (track via `investigator_decision`
  shape on suppressed-with-error rows), suppress with
  `notify=false, reason="judge_parse_error"`.

`create_notification_for_job(bundle, brief)` writes a notifications
row directly via `upsert_notification_row` (existing 1.0a RPC) with:
- event_id = max(bundle.events.id) — the most recent seed
- subscription_id = bundle.job.subscription_id
- decision shape: `{send: True, suppressed_by: None}` (status will
  be 'pending')
- payload_snapshot = brief jsonb (this is what renderer reads)
- delivery_kind/target = derived from bundle.subscription
  (call existing `_delivery_for_subscription`)
- decided_payload_version = corresponding event's payload_version

Important: also need to set `notifications.investigation_job_id`.
Either extend `upsert_notification_row` RPC to take it (preferred,
matches 1.0a pattern) OR follow up with an UPDATE in the same
transaction. The cleanest is to extend the RPC.

**Files touched in this chunk**:
- `bot/agent/investigator_loop.py` (new)
- `bot/agent/investigator.py` (new) — the `investigate(bundle)`
  function and dataclass
- `bot/agent/decider_loop.py` — already touched in §3
- `bot/db/queries.py` — extend `upsert_notification_row` to accept
  `investigation_job_id`; pass through to RPC; minor schema bump
  for the SQL function in 0017
- `bot/config.py` — add `investigator_loop_interval_seconds: int =
  20`, `investigator_max_duration_seconds: int = 90`,
  `investigator_max_turns: int = 6`,
  `investigator_max_turns_context: int = 30`

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
