# Public Notification Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public notification rules panel where everyone can browse active user rules, and signed-in users can add/manage their own rules.

**Architecture:** Keep raw subscription rows server-only. Introduce a small pure mapper/validator for safe public rule objects, a migration for `archived_at`, server actions for authenticated owner-only writes, and a public Next.js route at `/notifications/rules`.

**Tech Stack:** Next.js App Router, Server Components, Server Actions, Supabase service-role client, Tailwind CSS, Node test runner.

---

### Task 1: Safe Rule Model

**Files:**
- Create: `web/lib/notification-rules.ts`
- Test: `web/lib/notification-rules.test.ts`

- [ ] Write tests for sanitizing raw subscription rows into public rule objects.
- [ ] Run `node --test --experimental-strip-types lib/notification-rules.test.ts` from `web/` and verify it fails.
- [ ] Implement validation and mapping.
- [ ] Re-run the focused test.

### Task 2: Archive Semantics

**Files:**
- Create: `backend/supabase/migrations/0016_subscription_archives.sql`
- Modify: `bot/db/queries.py`
- Test: `bot/tests/test_proactive_notifications.py`

- [ ] Add a failing Python test that archived subscriptions are excluded.
- [ ] Add `archived_at` migration and update query filters.
- [ ] Change remove subscription to set `archived_at` and `enabled=false`.
- [ ] Run the focused bot tests.

### Task 3: Public Page + Actions

**Files:**
- Create: `web/app/notifications/rules/page.tsx`
- Create: `web/app/notifications/rules/rule-actions.ts`
- Create: `web/app/notifications/rules/rules-panel.tsx`
- Modify: `web/app/site-header.tsx`
- Modify: `web/app/me/page.tsx`

- [ ] Add server actions for add/edit/toggle/archive with session auth and owner checks.
- [ ] Add the public page using service-role reads and safe mapping.
- [ ] Add client UI for add/manage interactions.
- [ ] Link the page from the header and `/me`.
- [ ] Run `pnpm lint` and `pnpm build` in `web/`.

### Task 4: Final Verification

**Files:**
- Existing test suites.

- [ ] Run `python -m pytest bot/tests -v`.
- [ ] Run `git diff --check`.
- [ ] Commit the 1.0b changes.
