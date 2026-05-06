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
import json
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agent import decider_loop, delivery_loop, investigator_loop
from agent import runner as agent_runner
from config import settings
from db import queries as db_queries
from feishu import cards
from feishu import events as feishu_events
from feishu import post_format
from feishu.client import feishu_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("pmo-bot starting")

    # Look up our own identity so the @-mention check in groups can
    # match by open_id regardless of what the admin named the app.
    info = await feishu_client.fetch_self_info()
    if info:
        feishu_events.set_self_identity(
            open_id=info.get("open_id"),
            name=info.get("bot_name") or info.get("app_name"),
        )
        logger.info(
            "pmo-bot identity: name=%r open_id=%s…",
            info.get("bot_name") or info.get("app_name"),
            (info.get("open_id") or "")[:10],
        )
    else:
        logger.warning(
            "could not fetch bot self-info; @-mentions in groups may not work",
        )

    gc_task = asyncio.create_task(_gc_loop())
    decider_task = asyncio.create_task(decider_loop.run_forever())
    investigator_task = asyncio.create_task(investigator_loop.run_forever())
    delivery_task = asyncio.create_task(delivery_loop.run_forever())
    try:
        yield
    finally:
        for task in (gc_task, decider_task, investigator_task, delivery_task):
            task.cancel()
        await asyncio.gather(gc_task, decider_task, investigator_task, delivery_task, return_exceptions=True)
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

    # Resolve "who is asking" via feishu_links. None means the user
    # hasn't bound their account yet — agent will fall back to handle-
    # based parsing as before.
    sender_identity: dict | None = None
    try:
        sender_identity = db_queries.lookup_by_feishu_open_id(ev.sender_open_id)
    except Exception as e:
        # Service role missing? Permission issue? Don't kill the request
        # — just log and proceed without identity context.
        logger.warning("feishu_links lookup failed for %s: %s", ev.sender_open_id, e)

    parent_notification = None
    if ev.parent_message_id:
        try:
            parent_notification = db_queries.lookup_notification_by_feishu_msg_id(ev.parent_message_id)
        except Exception as e:
            logger.warning("parent notification lookup failed for %s: %s", ev.parent_message_id, e)

    framed_question = _frame_question(ev.text, sender_identity, parent_notification=parent_notification)

    # 1) ack with reaction (don't await — non-blocking, best-effort).
    asyncio.create_task(feishu_client.add_reaction(ev.message_id, "Get"))

    # 2) send the initial empty progress card.
    initial_card = cards.progress_card(question=ev.text, steps=[])
    card_message_id = await feishu_client.reply_card(ev.message_id, initial_card)

    if card_message_id is None:
        # Card path failed (permissions? rate limit?). Fall back to plain text.
        logger.warning("could not send card; falling back to plain text reply")
        try:
            answer = await asyncio.wait_for(
                agent_runner.answer(
                    conversation_key,
                    framed_question,
                    message_id=ev.message_id,
                    chat_id=ev.chat_id,
                    chat_type=ev.chat_type,
                    sender_open_id=ev.sender_open_id,
                    asker_user_id=(sender_identity or {}).get("user_id"),
                    asker_handle=(sender_identity or {}).get("handle"),
                ),
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
        async with asyncio.timeout(settings.agent_max_duration_seconds):
            async for event in agent_runner.answer_streaming(
                conversation_key,
                framed_question,
                message_id=ev.message_id,
                chat_id=ev.chat_id,
                chat_type=ev.chat_type,
                sender_open_id=ev.sender_open_id,
                asker_user_id=(sender_identity or {}).get("user_id"),
                asker_handle=(sender_identity or {}).get("handle"),
            ):
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
    except TimeoutError:
        logger.warning(
            "agent loop timed out for %s after %ss",
            conversation_key,
            settings.agent_max_duration_seconds,
        )
        await feishu_client.patch_card(
            card_message_id,
            cards.error_card(
                question=ev.text,
                error=f"查询超时（超过 {settings.agent_max_duration_seconds} 秒）",
            ),
        )
        return
    except Exception as e:
        logger.exception("agent loop failed for %s", conversation_key)
        await feishu_client.patch_card(
            card_message_id,
            cards.error_card(question=ev.text, error=f"{type(e).__name__}: {e}"),
        )
        return

    # 4) freeze the progress card — it stays as a record of what the
    #    agent did, header switches to "done" / grey.
    await feishu_client.patch_card(
        card_message_id,
        cards.progress_card(question=ev.text, steps=list(steps), finished=True),
    )

    # 5) Send the answer. The agent may have embedded image markers
    #    like [IMAGE:img_v2_xxx] — we split the text into segments and
    #    send text + image messages in order so the chat reads
    #    naturally.
    final_text = answer_text or "(空回答 — 试试换个问法?)"
    await _send_answer_with_images(parent_message_id=ev.message_id, text=final_text)


def _frame_question(
    text: str,
    sender: dict | None,
    *,
    parent_notification: dict | None = None,
) -> str:
    """Prepend a structured "who is asking" line to the user's text.

    The LLM treats this as ground truth context: when the user says
    "我做了啥", the agent already knows "我" maps to a specific
    handle / user_id and skips the lookup_user dance.

    Format is deliberately machine-readable (`[asker]: ...`) but kept
    in the user message rather than hidden in the system prompt — that
    way the SDK's per-conversation history sees it next to each turn,
    so context for follow-up messages is consistent.
    """
    if sender and sender.get("handle"):
        meta = (
            f"[asker] handle=@{sender['handle']} "
            f"user_id={sender['user_id']} "
            f"display_name={sender.get('display_name') or sender.get('feishu_name') or '-'}"
        )
    else:
        meta = "[asker] (this Feishu user has not bound their pmo_agent account yet)"
    framed = f"{meta}\n\n{text}"
    if parent_notification:
        event_row = parent_notification.get("events") or {}
        snapshot = {
            "notification": {
                "id": parent_notification.get("id"),
                "status": parent_notification.get("status"),
                "decided_at": parent_notification.get("decided_at"),
                "rendered_text": parent_notification.get("rendered_text"),
            },
            "subscription": parent_notification.get("subscriptions"),
            "event": {
                "id": event_row.get("id"),
                "source": event_row.get("source"),
                "source_id": event_row.get("source_id"),
                "user_id": event_row.get("user_id"),
                "project_root": event_row.get("project_root"),
                "occurred_at": event_row.get("occurred_at"),
            },
            "payload_snapshot": parent_notification.get("payload_snapshot") or event_row.get("payload"),
        }
        framed += "\n\n[parent_notification]\n" + json.dumps(
            snapshot,
            ensure_ascii=False,
            default=str,
        )
    return framed


# Pattern: [IMAGE:img_v2_abc...] anywhere in the text. We're loose
# about the key chars — Feishu image keys start with img_v2_ and can
# contain various base64-ish characters.
_IMAGE_MARKER_RE = re.compile(r"\[IMAGE:([A-Za-z0-9_\-]+)\]")


async def _send_answer_with_images(*, parent_message_id: str, text: str) -> None:
    """Split a final answer on [IMAGE:key] markers; send a text post +
    image messages in order. If the parser finds no markers, send
    exactly one post.
    """
    segments: list[tuple[str, str]] = []  # ("text"|"image", payload)
    pos = 0
    for m in _IMAGE_MARKER_RE.finditer(text):
        if m.start() > pos:
            segments.append(("text", text[pos:m.start()].strip()))
        segments.append(("image", m.group(1)))
        pos = m.end()
    if pos < len(text):
        segments.append(("text", text[pos:].strip()))

    # Emit. Empty text segments are skipped — they happen when an
    # image marker is the only content of a paragraph.
    for kind, payload in segments:
        if kind == "text":
            if not payload:
                continue
            post_content = post_format.markdown_to_post(payload)
            sent = await feishu_client.reply_post(parent_message_id, post_content)
            if sent is None:
                await feishu_client.reply_text(parent_message_id, payload)
        elif kind == "image":
            sent = await feishu_client.reply_image(parent_message_id, payload)
            if sent is None:
                logger.warning("could not send image_key=%s; skipping", payload)
