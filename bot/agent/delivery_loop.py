from __future__ import annotations

import asyncio
import logging
import uuid

from agent import renderer
from config import settings
from db import queries
from feishu import post_format
from feishu.client import FeishuSendError, feishu_client, stable_uuid_from_notif

logger = logging.getLogger(__name__)


class PermanentDeliveryError(Exception):
    pass


class TransientDeliveryError(Exception):
    pass


async def _send_notification(
    notif: queries.Notification,
    text: str,
    *,
    idempotency_uuid: str,
) -> str:
    post_content = post_format.markdown_to_post(text)
    if notif.delivery_kind == "feishu_user":
        msg_id = await feishu_client.send_to_user(
            notif.delivery_target or "",
            post_content,
            idempotency_uuid=idempotency_uuid,
        )
    elif notif.delivery_kind == "feishu_chat":
        msg_id = await feishu_client.send_to_chat(
            notif.delivery_target or "",
            post_content,
            idempotency_uuid=idempotency_uuid,
        )
    else:
        raise PermanentDeliveryError(f"unsupported delivery_kind={notif.delivery_kind!r}")
    if not msg_id:
        raise PermanentDeliveryError("feishu send returned no message_id")
    return msg_id


async def _send_once(notif: queries.Notification, text: str) -> str:
    idempotency_uuid = stable_uuid_from_notif(notif.id, notif.decided_payload_version)
    try:
        return await _send_notification(notif, text, idempotency_uuid=idempotency_uuid)
    except FeishuSendError as e:
        if e.transient:
            raise TransientDeliveryError(str(e)) from e
        raise PermanentDeliveryError(str(e)) from e


async def process_once(limit: int = 20) -> int:
    queries.reap_stale_claims(stale_after_minutes=5)
    claim_id = str(uuid.uuid4())
    bundles = queries.claim_pending_notifications(claim_id, limit)
    for bundle in bundles:
        notif = bundle.notification
        try:
            text = await renderer.render_notification(
                notif_row=notif,
                event_payload=bundle.notif_payload_snapshot,
                subscription=bundle.subscription,
            )
            msg_id = await _send_once(notif, text)
            ok = queries.mark_sent_if_claimed(
                notif.id,
                claim_id,
                msg_id=msg_id,
                rendered_text=text,
            )
            if not ok:
                logger.warning("notification claim lost before mark_sent: id=%s", notif.id)
        except asyncio.CancelledError:
            raise
        except TransientDeliveryError as e:
            logger.warning("transient delivery error notification=%s: %s", notif.id, e)
            queries.release_claim(notif.id, claim_id)
        except PermanentDeliveryError as e:
            logger.warning("permanent delivery error notification=%s: %s", notif.id, e)
            queries.mark_failed_if_claimed(notif.id, claim_id, str(e))
        except renderer.RenderError as e:
            logger.warning("permanent renderer error notification=%s: %s", notif.id, e)
            queries.mark_failed_if_claimed(notif.id, claim_id, str(e))
        except asyncio.TimeoutError as e:
            error = str(e) or "renderer timed out"
            logger.warning("renderer timeout notification=%s: %s", notif.id, error)
            queries.mark_failed_if_claimed(notif.id, claim_id, error)
        except Exception:
            logger.exception("delivery crashed for notification=%s", notif.id)
            queries.release_claim(notif.id, claim_id)
    return len(bundles)


async def run_forever() -> None:
    while True:
        try:
            await process_once(limit=20)
            await asyncio.sleep(settings.delivery_loop_interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("delivery loop iteration failed")
            await asyncio.sleep(60)
