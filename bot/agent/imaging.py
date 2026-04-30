"""Image generation + Feishu upload pipeline.

The flow we expose to the agent:

  prompt → doubao-seedream-5.0-lite → image url
                                   → download bytes
                                   → upload to Feishu /im/v1/images
                                   → image_key

The agent embeds the resulting image_key in its final answer using
a marker like [IMAGE:img_v2_xxxxx]. app.py parses those markers and
sends the images as separate Feishu messages alongside the post answer.

Why the marker dance instead of returning rich content directly?
  Claude Agent SDK tools return text. The LLM uses that text in its
  natural-language reply. The marker lets the LLM say e.g. "这是 bcc
  的样子: [IMAGE:img_v2_abc] 他主要做后端" and we can split that into
  two Feishu messages without coupling the agent to Feishu.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ── per-conversation rate limit ────────────────────────────────────────


@dataclass
class _ConversationRate:
    timestamps: list[float] = field(default_factory=list)


_rate: dict[str, _ConversationRate] = {}
_rate_lock = asyncio.Lock()


async def _check_and_record_rate(conversation_key: str) -> Optional[str]:
    """Returns an error message if the conversation has hit its quota,
    else None and records this attempt against the bucket.
    """
    cap = settings.image_max_per_conversation_per_hour
    now = time.monotonic()
    one_hour_ago = now - 3600
    async with _rate_lock:
        bucket = _rate.setdefault(conversation_key, _ConversationRate())
        # prune older entries
        bucket.timestamps = [t for t in bucket.timestamps if t > one_hour_ago]
        if len(bucket.timestamps) >= cap:
            oldest = bucket.timestamps[0]
            wait_s = int(3600 - (now - oldest))
            return (
                f"image quota reached: {cap}/hour for this conversation. "
                f"try again in ~{wait_s // 60} min."
            )
        bucket.timestamps.append(now)
    return None


# ── ARK image generation ──────────────────────────────────────────────


async def _generate_via_ark(prompt: str, size: str) -> str:
    """Call doubao-seedream and return the image URL.

    The ARK image endpoint is OpenAI-compatible. Empirically it returns
    {"data":[{"url": "..."}]} synchronously.
    """
    body = {
        "model": settings.image_model,
        "prompt": prompt[:500],
        "size": size,
        # Doubao supports response_format="url" (default) or "b64_json".
        # URL is friendlier for downstream Feishu upload.
        "response_format": "url",
    }
    async with httpx.AsyncClient(timeout=60.0) as ac:
        resp = await ac.post(
            settings.image_api_url,
            headers={
                "Authorization": f"Bearer {settings.anthropic_auth_token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    items = (data or {}).get("data") or []
    if not items:
        raise RuntimeError(f"ARK image: no data in response: {data}")
    url = items[0].get("url")
    if not url:
        # b64 fallback path (shouldn't happen with response_format=url)
        b64 = items[0].get("b64_json")
        if b64:
            raise RuntimeError("ARK returned b64; URL path expected. Inspect response_format support.")
        raise RuntimeError(f"ARK image: no url in item: {items[0]}")
    return url


# ── Feishu upload ─────────────────────────────────────────────────────


async def _feishu_tenant_token() -> str:
    """Fetch (uncached) tenant_access_token. Tokens last ~2h; for the
    low rate we expect (≤5/hour/conv) re-fetching per call is fine."""
    async with httpx.AsyncClient(timeout=10.0) as ac:
        r = await ac.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        r.raise_for_status()
        d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {d}")
    return d["tenant_access_token"]


async def _upload_to_feishu(image_bytes: bytes) -> str:
    """Upload to Feishu /im/v1/images, return image_key.

    Feishu requires multipart with image_type="message".
    """
    token = await _feishu_tenant_token()
    files = {
        "image": ("generated.png", image_bytes, "image/png"),
    }
    data = {"image_type": "message"}
    async with httpx.AsyncClient(timeout=30.0) as ac:
        r = await ac.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
        )
        r.raise_for_status()
        d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"feishu image upload failed: {d}")
    return d["data"]["image_key"]


# ── public entry point ────────────────────────────────────────────────


async def generate_and_upload(
    *,
    conversation_key: str,
    prompt: str,
    size: str = "1024x1024",
) -> dict:
    """Generate an image via doubao and upload it to Feishu.

    Returns:
      {"image_key": str, "image_url": str}  on success
      {"error": str}                         on rate-limit or failure
    """
    rate_err = await _check_and_record_rate(conversation_key)
    if rate_err:
        return {"error": rate_err}

    try:
        url = await _generate_via_ark(prompt, size)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        return {"error": f"image generation failed: HTTP {e.response.status_code}: {body}"}
    except Exception as e:
        return {"error": f"image generation failed: {type(e).__name__}: {e}"}

    # Download the image bytes (ARK URLs are time-limited so we want
    # to upload to Feishu before the URL expires).
    try:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            img_resp = await ac.get(url)
            img_resp.raise_for_status()
            image_bytes = img_resp.content
    except Exception as e:
        return {"error": f"image download failed: {type(e).__name__}: {e}"}

    try:
        image_key = await _upload_to_feishu(image_bytes)
    except Exception as e:
        # We have the URL but couldn't upload. Returning just the URL is
        # graceful degradation — the LLM can mention it as a link.
        logger.warning("feishu upload failed, returning ARK url only: %s", e)
        return {"image_url": url, "error": f"feishu upload failed: {e}"}

    return {"image_key": image_key, "image_url": url}
