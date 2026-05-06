from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Request, Response

from config import settings
from external.ingest import ingest_external_event

router = APIRouter()
_MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


def _verify_github_signature(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _verify_gitea_signature(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _read_limited_body(request: Request) -> bytes | None:
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > _MAX_WEBHOOK_BODY_BYTES:
        return None
    body = await request.body()
    if len(body) > _MAX_WEBHOOK_BODY_BYTES:
        return None
    return body


async def _handle_webhook(request: Request, provider: str) -> Response:
    body = await _read_limited_body(request)
    if body is None:
        return Response(status_code=413)
    if provider == "github":
        secret = settings.github_webhook_secret
        signature = request.headers.get("x-hub-signature-256", "")
        verified = _verify_github_signature(body, signature, secret)
        event_type = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
    else:
        secret = settings.gitea_webhook_secret
        signature = request.headers.get("x-gitea-signature", "")
        verified = _verify_gitea_signature(body, signature, secret)
        event_type = request.headers.get("x-gitea-event", "")
        delivery_id = request.headers.get("x-gitea-delivery", "")
    if not secret:
        return Response(status_code=500)
    if not verified:
        return Response(status_code=401)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=400)
    if not isinstance(payload, dict) or not event_type or not delivery_id:
        return Response(status_code=400)
    await ingest_external_event(
        provider=provider,
        event_type=event_type,
        delivery_id=delivery_id,
        payload=payload,
        raw_bytes=body,
        headers=dict(request.headers),
    )
    return Response(status_code=200)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    return await _handle_webhook(request, "github")


@router.post("/webhooks/gitea")
async def gitea_webhook(request: Request) -> Response:
    return await _handle_webhook(request, "gitea")
