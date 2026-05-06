from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import settings
from db import queries
from external.redaction import redact_text

logger = logging.getLogger(__name__)


def _auth_headers(provider: str) -> dict[str, str]:
    if provider == "github" and settings.github_api_token:
        return {"Authorization": f"token {settings.github_api_token}"}
    if provider == "gitea" and settings.gitea_api_token:
        return {"Authorization": f"token {settings.gitea_api_token}"}
    return {}


def _api_base(provider: str) -> str:
    if provider == "gitea":
        return (settings.gitea_api_url or "").rstrip("/") or "https://gitea.com/api/v1"
    return "https://api.github.com"


async def _fetch_pr_files_remote(provider: str, repo_full_name: str, pr_number: int) -> dict[str, Any]:
    base = _api_base(provider)
    url = f"{base}/repos/{repo_full_name}/pulls/{pr_number}/files"
    headers = _auth_headers(provider)
    async with httpx.AsyncClient(timeout=20) as client:
        res = await _get_with_retries(client, url, headers=headers, params={"per_page": 30})
        res.raise_for_status()
        rows = res.json()
    files = []
    redaction_hits_total = 0
    for row in rows[:30] if isinstance(rows, list) else []:
        patch = row.get("patch") or ""
        patch_excerpt, redaction_hits = redact_text(patch[:200]) if patch else ("", 0)
        redaction_hits_total += redaction_hits
        files.append(
            {
                "path": row.get("filename") or row.get("path") or "",
                "status": row.get("status"),
                "additions": row.get("additions"),
                "deletions": row.get("deletions"),
                "changes": row.get("changes"),
                "patch_excerpt": patch_excerpt,
            }
        )
    result = {"files": files, "count": len(files)}
    logger.info(
        "external.fetch_pr_files_remote provider=%s repo=%s pr=%s count=%s redaction_hits=%s",
        provider,
        repo_full_name,
        pr_number,
        len(files),
        redaction_hits_total,
    )
    return result


async def _get_with_retries(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            res = await client.get(url, **kwargs)
            status = int(getattr(res, "status_code", 200))
            if status not in (429,) and status < 500:
                return res
            try:
                res.raise_for_status()
            except httpx.HTTPError as e:
                last_error = e
        except httpx.HTTPError as e:
            last_error = e
        if attempt == 2:
            if last_error:
                raise last_error
            return res
        logger.info(
            "external.fetch_retry url=%s attempt=%s reason=%s",
            url,
            attempt + 1,
            type(last_error).__name__ if last_error else f"status_{status}",
        )
        await asyncio.sleep(0.5 * (2 ** attempt))
    if last_error:
        raise last_error
    raise RuntimeError("unreachable retry state")


async def fetch_pr_files(
    provider: str,
    repo_full_name: str,
    pr_number: int,
    paths_filter: list[str] | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    cache_key = f"{repo_full_name}/{pr_number}/{head_sha}" if head_sha else f"{repo_full_name}/{pr_number}"
    cached = queries.lookup_external_resource(provider, "pr_files", cache_key)
    if cached:
        logger.info("external.fetch_pr_files_cache_hit provider=%s key=%s", provider, cache_key)
        result = cached["content"]
    else:
        logger.info("external.fetch_pr_files_cache_miss provider=%s key=%s", provider, cache_key)
        result = await _fetch_pr_files_remote(provider, repo_full_name, pr_number)
        queries.write_external_resource(provider, "pr_files", cache_key, result, ttl_seconds=86400)
    if paths_filter:
        filters = [p for p in paths_filter if p]
        return {"files": [f for f in result.get("files", []) if any(p in f.get("path", "") for p in filters)]}
    return result
