from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response

from config import settings
from external.ingest import ingest_external_event

router = APIRouter()
logger = logging.getLogger(__name__)
_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


def _verify_github_signature(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _verify_gitea_signature(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header:
        return False
    if header.startswith("sha256="):
        header = header.removeprefix("sha256=")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _read_limited_body(request: Request) -> tuple[bytes | None, int | None]:
    content_length_raw = request.headers.get("content-length")
    if not content_length_raw:
        return None, 411
    try:
        content_length = int(content_length_raw)
    except ValueError:
        return None, 400
    if content_length > _MAX_WEBHOOK_BODY_BYTES:
        return None, 413
    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BODY_BYTES:
        return None, 413
    return body, None


async def _handle_webhook(request: Request, provider: str) -> Response:
    body, body_error = await _read_limited_body(request)
    if body_error is not None or body is None:
        logger.info("webhook.rejected provider=%s reason=body_limit status=%s", provider, body_error or 400)
        return Response(status_code=body_error or 400)
    if provider == "github":
        secret = settings.github_webhook_secret
        if not secret:
            logger.info("webhook.rejected provider=%s reason=missing_secret status=500", provider)
            return Response(status_code=500)
        signature = request.headers.get("x-hub-signature-256", "")
        verified = _verify_github_signature(body, signature, secret)
        event_type = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
    else:
        secret = settings.gitea_webhook_secret
        if not secret:
            logger.info("webhook.rejected provider=%s reason=missing_secret status=500", provider)
            return Response(status_code=500)
        signature = request.headers.get("x-gitea-signature", "")
        verified = _verify_gitea_signature(body, signature, secret)
        event_type = request.headers.get("x-gitea-event", "")
        delivery_id = request.headers.get("x-gitea-delivery", "")
    if not verified:
        logger.info("webhook.rejected provider=%s event_type=%s delivery_id=%s reason=bad_signature status=401", provider, event_type, delivery_id)
        return Response(status_code=401)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.info("webhook.rejected provider=%s event_type=%s delivery_id=%s reason=bad_json status=400", provider, event_type, delivery_id)
        return Response(status_code=400)
    if not isinstance(payload, dict) or not event_type or not delivery_id:
        logger.info("webhook.rejected provider=%s event_type=%s delivery_id=%s reason=bad_shape status=400", provider, event_type, delivery_id)
        return Response(status_code=400)
    logger.info(
        "webhook.accepted provider=%s event_type=%s delivery_id=%s bytes=%s",
        provider,
        event_type,
        delivery_id,
        len(body),
    )
    await ingest_external_event(
        provider=provider,
        event_type=event_type,
        delivery_id=delivery_id,
        payload=payload,
        raw_bytes=body,
        headers=dict(request.headers),
    )
    logger.info("webhook.completed provider=%s event_type=%s delivery_id=%s", provider, event_type, delivery_id)
    return Response(status_code=200)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    return await _handle_webhook(request, "github")


@router.post("/webhooks/gitea")
async def gitea_webhook(request: Request) -> Response:
    return await _handle_webhook(request, "gitea")
