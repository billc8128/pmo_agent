# Public Notification Rules Panel Design

## Goal

Build the 1.0b notification rules UI as a public rules directory. Anyone can see the active public notification rules people have created, while signed-in users can quickly add and manage their own user-scope rules.

## Product Shape

- Add a public page at `/notifications/rules`.
- Show active user-scope rules from all users.
- Display only safe fields: rule text, enabled status, owner handle/display name, and timestamps.
- Never expose `scope_id`, `created_by`, `chat_id`, Feishu IDs, or delivery targets to the browser.
- Signed-out visitors can browse only.
- Signed-in users can add a user-scope rule from this page.
- Signed-in users can pause/resume, edit, or archive only their own rules.

## Data Boundary

The page uses server-side Supabase service-role reads and maps database rows into a safe presentation model before rendering. The browser never receives raw `subscriptions` rows.

Writes use server actions. Each action authenticates the current session, then uses the service-role client only after checking the target rule is `scope_kind='user'` and `scope_id=<current user id>`.

## Deletion Semantics

Existing `enabled=false` rows came from the chat bot's "remove subscription" behavior. 1.0b needs pause/resume and archive to be distinct, so add `subscriptions.archived_at`.

- Public directory filters `archived_at is null` and `enabled=true`.
- Pause/resume toggles `enabled`.
- Archive sets `archived_at` and `enabled=false`, preserving notification history.
- Existing disabled rows are migrated to archived rows because there was no pause feature before 1.0b.

## Out of Scope

- Group/chat rule management.
- Quiet-hour and daily-cap preference tables.
- Notification decision logic changes.
