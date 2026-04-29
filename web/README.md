# pmo_agent — web

The Next.js front-end for `pmo_agent`. Renders the public timeline at
`/u/:handle`. See `../docs/specs/2026-04-29-mvp-design.md` §6 for the
product spec.

## Local development

```bash
npm install        # once
npm run dev        # http://localhost:3000
```

You need `web/.env.local` with:

```
NEXT_PUBLIC_SUPABASE_URL=https://<project-ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon-key>
```

Both are also configured as Vercel env vars (`vercel env ls`).

## Deployment

The live site is `https://pmo-agent-sigma.vercel.app`, deployed to a
Vercel project named `pmo-agent` under team `superlion8s-projects`.

To deploy from a fresh checkout:

```bash
vercel link --project pmo-agent     # connects this dir to the project
vercel deploy --prod                # builds and ships
```

The `.vercel/` directory created by `vercel link` is gitignored on
purpose — every contributor links their own checkout.

## Stack

- Next.js 16 (App Router, Turbopack default)
- React 19.2 canary
- Tailwind CSS v4 (no config; theme in `app/globals.css`)
- `@supabase/supabase-js` for both server- and client-side data
  fetching (anon key only; RLS enforces public-read / authenticated-write)
- `react-markdown` + `remark-gfm` + `rehype-highlight` for rendering
  `agent_response_full` safely (no raw HTML)

## Architecture

```
/                      — landing page (static)
/u/[handle]            — Server Component: SSR fetches profile + turns
   ├ TurnCard          — Client Component: expand toggle
   │   └ ResponseMarkdown — Client Component: react-markdown render
   └ TimelineClient    — Client Component: 10s polling for new turns
```

Per spec §6.2: no realtime/websockets in MVP. Polling is intentional.
