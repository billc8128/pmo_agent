# CLAUDE.md — pmo_agent

Conventions for AI agents (Claude Code, Cursor, Codex, ...) working
in this repository.

## What this project is

`pmo_agent` is an independent project that publishes a public
timeline of a user's local AI-coding sessions. See [README.md](README.md)
for the product summary and architecture diagram.

It is **not** related to and **not** constrained by any other
project on this machine (e.g. `~/Desktop/vibelive`). Conventions
from other repos do **not** apply here unless explicitly restated
in this file.

## Tech stack (locked in for MVP)

- **Daemon**: Go (single static binary, cross-platform)
- **Backend**: Supabase (Postgres + Auth + one Edge Function)
- **LLM**: OpenRouter (called from the Edge Function only)
- **Web**: Next.js + Vercel (SSR)

Deliberate choices, do not "improve" without discussion:

- ✅ Supabase and Vercel are intentional. The "no Vercel / no
  Supabase" rule that exists in some other repos on this machine
  does **not** apply here. Demo iteration speed wins.
- ✅ OpenRouter, **not** the Anthropic SDK. The model is selected
  via OpenRouter's model string. We may switch models often.
- ✅ Daemon authenticates with a long-lived Personal Access Token
  (PAT). No OAuth, no refresh tokens, no device management for v0.
- ✅ Public-by-default. Every user has a public profile page at
  `/u/:handle`. Privacy controls (private / unlisted) come later.

## Repo layout

```
pmo_agent/
├── daemon/        Go agent that runs on user's machine
├── backend/       Supabase project (schema, migrations, edge fn)
├── web/           Next.js app deployed to Vercel
└── docs/specs/    Design documents (markdown)
```

## Working conventions

### Where to put things

- Schema changes go in `backend/supabase/migrations/`. Forward-only.
- Edge functions go in `backend/supabase/functions/<name>/`.
- Daemon code is one Go module: `module github.com/<owner>/pmo_agent/daemon`.
- Web is a standard Next.js app; pages in `web/app/`, components in
  `web/components/`.

### Secrets

- **Never** put the OpenRouter API key in the daemon. The daemon
  uploads raw turns to Supabase; the Edge Function holds the key
  and calls OpenRouter server-side.
- Daemon's only secret is the user's PAT, stored in
  `~/.pmo-agent/config.toml` (mode 0600).
- Local dev: `.env.local` files, never committed. Production keys
  live in Vercel project env + Supabase project secrets.

### Privacy / redaction

This project's value depends on users trusting the redaction layer.
Take it seriously:

- All redaction happens **in the daemon, before upload**. Never
  rely on server-side redaction as the only line of defense.
- Redaction rules live in `daemon/internal/redact/`. Rules are
  unit-tested with real-world fixtures.
- When adding a new redaction rule, add a fixture in
  `daemon/internal/redact/testdata/` first (TDD).

### Public-by-default UX

Every UI surface that shows a turn must make it visually obvious
whether that turn is public or not. (For MVP: everything is public,
so a simple banner saying so is enough.) Do not ship a UI that
silently turns "draft" content into "public" without a clear signal.

### Don't do

- ❌ Don't add a "self-hosted backend service" between daemon and
  Supabase. Daemon → Supabase REST is the architecture. If logic
  needs to run server-side, it goes in an Edge Function.
- ❌ Don't add realtime / websockets in MVP. Web polls every 10s.
- ❌ Don't add features the user didn't ask for "while you're in
  there." This is a demo; surface area should stay small.
- ❌ Don't suggest moving to ECS / RDS / self-hosted Postgres
  unless explicitly asked. Migration plan exists; don't pre-migrate.

## Reading order for new contributors (human or AI)

1. [README.md](README.md) — what this is and why
2. This file — how to work in the repo
3. [docs/specs/2026-04-29-mvp-design.md](docs/specs/2026-04-29-mvp-design.md)
   — the full MVP design spec, the source of truth for all
   decisions made during brainstorming
