"""FastAPI entry point — receives Feishu webhooks, dispatches to the agent.

Design notes:
  - Webhook ack must be fast (<3s) or Feishu retries. We do the minimum
    parsing to dedupe + decide if it's relevant, then spawn the agent
    work as an asyncio task and return 200 immediately.
  - The agent's reply lands as a NEW Feishu message via the bot's
    create-message API, threaded as a reply to the user's message
    (preserves context in groups).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agent import runner as agent_runner
from config import settings
from feishu import events as feishu_events
from feishu.client import feishu_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("pmo-bot starting")
    # Periodic GC of idle SDK clients — light task, fire-and-forget.
    gc_task = asyncio.create_task(_gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        await agent_runner.shutdown_all()
        logger.info("pmo-bot stopped")


async def _gc_loop() -> None:
    while True:
        try:
            await asyncio.sleep(300)  # every 5 min
            await agent_runner.gc_idle_clients()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("gc loop error: %s", e)


app = FastAPI(title="pmo-bot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/feishu/webhook")
async def feishu_webhook(request: Request):
    body = await request.json()
    body = feishu_events.decrypt_if_needed(body)

    # 1) URL verification handshake
    if feishu_events.is_url_verification(body):
        return JSONResponse(feishu_events.url_verification_response(body))

    # 2) Dedup by event_id (Feishu retries on slow ACKs)
    eid = feishu_events.event_id_of(body)
    if eid and feishu_events.already_seen(eid):
        return PlainTextResponse("duplicate")

    # 3) Try to parse as a user message
    parsed = feishu_events.parse_message_event(body)
    if parsed is None:
        return PlainTextResponse("ignored")

    # 4) Decide whether to engage:
    #     - p2p: always
    #     - group: only when @-mentioned
    if parsed.chat_type == "group" and not parsed.is_at_bot:
        return PlainTextResponse("group not addressed")

    # 5) Spawn the agent work as a background task. The webhook returns
    #    immediately; the agent's reply will go via a fresh API call.
    asyncio.create_task(_handle_message(parsed))

    return PlainTextResponse("ok")


async def _handle_message(ev: feishu_events.ParsedMessageEvent) -> None:
    conversation_key = f"{ev.chat_id}:{ev.sender_open_id}"
    logger.info(
        "incoming: chat=%s/%s sender=%s text=%r",
        ev.chat_type, ev.chat_id, ev.sender_open_id, ev.text[:80],
    )

    # Fire a "thinking..." placeholder we'll patch with the final answer.
    placeholder_id = await feishu_client.reply_text(
        ev.message_id,
        "正在查询…",
    )

    try:
        answer = await asyncio.wait_for(
            agent_runner.answer(conversation_key, ev.text),
            timeout=settings.agent_max_duration_seconds,
        )
    except asyncio.TimeoutError:
        answer = "(查询超时,试试问得更具体一点?)"
    except Exception as e:
        logger.exception("agent failed for %s", conversation_key)
        answer = f"(出错了: {type(e).__name__})"

    if placeholder_id:
        ok = await feishu_client.patch_text(placeholder_id, answer)
        if not ok:
            # Patch failed — fall back to a fresh reply.
            await feishu_client.reply_text(ev.message_id, answer)
    else:
        await feishu_client.reply_text(ev.message_id, answer)
