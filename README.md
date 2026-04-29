# pmo_agent

A public timeline of your AI coding sessions.

`pmo_agent` watches your local conversations with coding agents
(Claude Code, Codex, ...), summarizes each turn, and publishes them
to a public web profile — so anyone can glance at what you're working
on with AI, in real time.

> **Status**: MVP / demo. Validating whether a public AI-workflow
> timeline is something people want — to read, and to share.

---

## Architecture

```
┌─────── User's machine ───────┐         ┌────── Supabase ───────┐
│                               │         │                       │
│  pmo-agent daemon  (Go)       │         │  Postgres             │
│   ├─ watch ~/.claude/...      │ ─POST─► │   - users / turns     │
│   ├─ watch ~/.codex/...       │         │  Auth (PAT)           │
│   ├─ parse → redact (regex)   │         │  Edge Function        │
│   └─ HTTPS upload             │         │   - summarize via     │
│                               │         │     OpenRouter        │
└───────────────────────────────┘         └───────────────────────┘
                                                    │
                                                    ▼
                                          ┌─── Vercel ──────────┐
                                          │  Next.js (SSR)      │
                                          │   /u/:handle        │
                                          │   /me               │
                                          │   /discover         │
                                          └─────────────────────┘
```

Three loosely coupled pieces:

- **`daemon/`** — Go single binary. Watches local agent transcripts,
  detects complete `(user_message, agent_response)` turns, redacts
  secrets locally, uploads to Supabase.
- **`backend/`** — Supabase project (Postgres + Auth + Edge Function
  for OpenRouter-backed summarization). Mostly schema + one function;
  no traditional backend service.
- **`web/`** — Next.js app on Vercel. Public timelines at `/u/:handle`,
  personal dashboard at `/me`, community feed at `/discover`.

---

## Why this exists

Vibe coders spend hours in conversation with coding agents. Today
that activity is invisible to teammates, mentors, and the wider
community. `pmo_agent` makes it a glanceable, shareable artifact:

- a teammate on a call can see what you're currently asking the AI
- a follower can browse how you tackled a bug last week
- the community gets a feed of real AI-coding workflows in the wild

Closest analogue: **GitHub profile + Twitter, but for your AI pair-
programming session**.

---

## Tech stack

| Layer  | Choice                 | Reason                                |
|--------|------------------------|---------------------------------------|
| Daemon | Go (single binary)     | Cross-platform, zero deps, easy install |
| DB / Auth | Supabase             | Demo speed; Postgres + auth + edge fn |
| LLM    | OpenRouter             | Model-agnostic, demo-friendly billing |
| Web    | Next.js (SSR) + Vercel | Familiar, fast deploy                 |

This stack is chosen for **demo iteration speed**. Migration to
self-hosted Postgres + ECS is straightforward later if needed: the
data layer is wrapped behind a thin repo abstraction.

---

## Relationship to other projects

`pmo_agent` is an **independent project**. It is not part of, and
does not depend on, vibelive or any other repo. The original idea
came up while discussing a collaboration feature for vibelive's
desktop app, but the value of the AI-workflow timeline turned out
to stand on its own — so it got its own home.

---

## Documentation

- Design specs: [`docs/specs/`](docs/specs/)
- AI agent guidelines: [`CLAUDE.md`](CLAUDE.md)

---

## Status

Pre-MVP. See `docs/specs/2026-04-29-mvp-design.md` for the full
plan and `CLAUDE.md` for working conventions.
