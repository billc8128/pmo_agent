"""FastAPI entry point — Feishu webhook + agent dispatch.

Webhook flow:
  1. Decrypt + dedup + parse the event.
  2. Decide whether to engage (always in p2p; only on @mention in groups).
  3. ACK with a 👀-style "I see you" reaction on the user's message.
     This replaces the old "正在查询…" placeholder, which polluted
     the chat with an extra message slot.
  4. Send a progress card as a threaded reply, then patch it as the
     agent calls tools.
  5. When the agent's done, patch the card one final time with the
     answer rendered as Feishu markdown.

We patch at most once per second to stay well under Feishu's
PatchMessage rate limit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agent import runner as agent_runner
from config import settings
from feishu import cards
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
            await asyncio.sleep(300)
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

    if feishu_events.is_url_verification(body):
        return JSONResponse(feishu_events.url_verification_response(body))

    eid = feishu_events.event_id_of(body)
    if eid and feishu_events.already_seen(eid):
        return PlainTextResponse("duplicate")

    parsed = feishu_events.parse_message_event(body)
    if parsed is None:
        return PlainTextResponse("ignored")

    if parsed.chat_type == "group" and not parsed.is_at_bot:
        return PlainTextResponse("group not addressed")

    asyncio.create_task(_handle_message(parsed))
    return PlainTextResponse("ok")


# ──────────────────────────────────────────────────────────────────────
# Message handler
# ──────────────────────────────────────────────────────────────────────

# Throttle: don't patch the card more than once per CARD_PATCH_INTERVAL.
# Feishu's PatchMessage limit is roughly 5/sec per app; staying at 1/sec
# means we can run several conversations concurrently without bumping
# into it.
CARD_PATCH_INTERVAL = 1.0


async def _handle_message(ev: feishu_events.ParsedMessageEvent) -> None:
    conversation_key = f"{ev.chat_id}:{ev.sender_open_id}"
    logger.info(
        "incoming: chat=%s/%s sender=%s text=%r",
        ev.chat_type, ev.chat_id, ev.sender_open_id, ev.text[:80],
    )

    # 1) ack with reaction (don't await — non-blocking, best-effort).
    asyncio.create_task(feishu_client.add_reaction(ev.message_id, "GET"))

    # 2) send the initial empty progress card.
    initial_card = cards.progress_card(question=ev.text, steps=[])
    card_message_id = await feishu_client.reply_card(ev.message_id, initial_card)

    if card_message_id is None:
        # Card path failed (permissions? rate limit?). Fall back to plain text.
        logger.warning("could not send card; falling back to plain text reply")
        try:
            answer = await asyncio.wait_for(
                agent_runner.answer(conversation_key, ev.text),
                timeout=settings.agent_max_duration_seconds,
            )
        except Exception as e:
            answer = f"(出错了: {type(e).__name__})"
        await feishu_client.reply_text(ev.message_id, answer)
        return

    # 3) drive the agent, patch the card as tool calls happen.
    steps: list[dict] = []
    last_patch_at = 0.0
    answer_text = ""

    async def maybe_patch():
        nonlocal last_patch_at
        now = time.monotonic()
        if now - last_patch_at < CARD_PATCH_INTERVAL:
            return
        last_patch_at = now
        await feishu_client.patch_card(
            card_message_id,
            cards.progress_card(question=ev.text, steps=list(steps)),
        )

    try:
        async for event in agent_runner.answer_streaming(conversation_key, ev.text):
            if event["kind"] == "tool":
                # Mark previous tool as done — the LLM has moved on.
                if steps and not steps[-1].get("done"):
                    steps[-1]["done"] = True
                steps.append({
                    "tool": event["name"],
                    "args_hint": event.get("args_hint", ""),
                    "done": False,
                })
                await maybe_patch()
            elif event["kind"] == "final":
                # Mark the trailing tool as done now that the agent is
                # writing its answer.
                if steps and not steps[-1].get("done"):
                    steps[-1]["done"] = True
                answer_text = event["text"]
            elif event["kind"] == "error":
                await feishu_client.patch_card(
                    card_message_id,
                    cards.error_card(question=ev.text, error=event["message"]),
                )
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("agent loop failed for %s", conversation_key)
        await feishu_client.patch_card(
            card_message_id,
            cards.error_card(question=ev.text, error=f"{type(e).__name__}: {e}"),
        )
        return

    # 4) final patch — render answer markdown + tool-count footer.
    await feishu_client.patch_card(
        card_message_id,
        cards.final_card(
            question=ev.text,
            answer_markdown=answer_text or "(空回答 — 试试换个问法?)",
            tool_count=len(steps),
        ),
    )
