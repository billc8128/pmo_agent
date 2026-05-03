# pmo-bot

Feishu chat bot that answers PMO questions about the team's
AI-coding activity recorded in pmo_agent.

## Architecture

```
飞书消息  →  /feishu/webhook  →  agent runner (Claude Agent SDK)  →  supabase tools
                                                  ↓
                                            生成回答 → 飞书 reply
```

- **Entry**: FastAPI on Railway, single endpoint `/feishu/webhook`
- **Brain**: Claude Agent SDK (Python) talking to 火山方舟 Coding Plan
  via the Anthropic-compatible protocol
- **Memory**: a `ClaudeSDKClient` per `(chat_id, sender_id)`,
  garbage-collected after 30 min idle
- **Data**: Supabase anon key for public read tools, plus server-only
  service-role key for Feishu identity lookup, `bot_workspace`, and
  `bot_actions` write-tool state

## Tools the agent can call

- `list_users` — all known handles
- `lookup_user(handle)` — handle → user_id
- `get_recent_turns(user_id, since, until, project_root, limit)` — raw turns
- `get_project_overview(user_id)` — cached per-project narrative summaries
- `get_activity_stats(user_id, days)` — aggregate counts
- `today_iso()` — current time anchors

These wrap `supabase/queries.py`, which is itself a thin layer over
the Supabase client.

## Local dev

```bash
cp .env.example .env
# fill in real credentials, including server-only SUPABASE_SERVICE_ROLE_KEY
pip install -r requirements.txt
uvicorn app:app --reload --port 8080
```

Then expose locally for Feishu to reach (e.g. `ngrok http 8080`) and
paste the URL into the Feishu app's event subscription.

## Feishu Permission Scopes

Before running `python -m scripts.bootstrap_bot_workspace`, enable the
bot app's tenant-token permissions in Feishu Open Platform. The write
tools need calendar, Bitable, Docx/Drive, and contact scopes. At a
minimum, bootstrap currently requires:

- `calendar:calendar` or `calendar:calendar:create`

The full PMO write-tool surface also uses calendar event/freebusy,
Bitable app/table/record, Drive file/folder/import, Docx block, Wiki
resolve, and contact directory APIs. If a bootstrap or smoke-test call
returns Feishu `99991672 Access denied`, open the authorization URL in
the error message, grant the listed scope, publish/enable the app
permission, and rerun the failed command.

## Deploy

Railway:

```bash
railway link            # connect this dir to a project
railway up              # deploy
railway domain          # get the public URL → paste into Feishu webhook
```

Or via the dashboard: connect the GitHub repo, point at the `bot/`
directory, set the env vars from `.env.example`.
