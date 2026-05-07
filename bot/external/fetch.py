from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from config import settings
from db import queries
from external.logging import safe_log_value
from external.redaction import redact_text

logger = logging.getLogger(__name__)
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _auth_headers(provider: str) -> dict[str, str]:
    if provider == "github" and settings.github_api_token:
        return {"Authorization": f"token {settings.github_api_token}"}
    if provider == "gitea" and settings.gitea_api_token:
        return {"Authorization": f"token {settings.gitea_api_token}"}
    return {}


def _normalize_provider(provider: str) -> str:
    provider = (provider or "").strip().lower()
    if provider not in {"github", "gitea"}:
        raise ValueError("provider must be github or gitea")
    return provider


def _api_base(provider: str) -> str:
    if provider == "gitea":
        return (settings.gitea_api_url or "").rstrip("/") or "https://gitea.com/api/v1"
    return "https://api.github.com"


def _validate_repo_full_name(repo_full_name: str) -> str:
    repo = repo_full_name.strip().lower()
    if not _REPO_FULL_NAME_RE.fullmatch(repo):
        raise ValueError("repo_full_name must look like owner/name")
    return repo


async def _fetch_pr_files_remote(provider: str, repo_full_name: str, pr_number: int) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    repo_full_name = _validate_repo_full_name(repo_full_name)
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
        redacted_patch, redaction_hits = redact_text(patch) if patch else ("", 0)
        patch_excerpt = redacted_patch[:200]
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
        safe_log_value(provider),
        safe_log_value(repo_full_name),
        pr_number,
        len(files),
        redaction_hits_total,
    )
    return result


async def list_pull_requests(
    provider: str,
    repo_full_name: str,
    *,
    state: str = "all",
    limit: int = 10,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    repo_full_name = _validate_repo_full_name(repo_full_name)
    state = (state or "all").strip().lower()
    if state not in {"open", "closed", "all"}:
        state = "all"
    limit = max(1, min(int(limit or 10), 30))
    base = _api_base(provider)
    url = f"{base}/repos/{repo_full_name}/pulls"
    params: dict[str, Any] = {
        "state": state,
        "sort": "updated",
        "direction": "desc",
    }
    if provider == "github":
        params["per_page"] = limit
    else:
        params["limit"] = limit
    async with httpx.AsyncClient(timeout=20) as client:
        res = await _get_with_retries(client, url, headers=_auth_headers(provider), params=params)
        res.raise_for_status()
        rows = res.json()
    pull_requests = [_normalize_pr_row(row) for row in (rows[:limit] if isinstance(rows, list) else [])]
    logger.info(
        "external.list_pull_requests provider=%s repo=%s state=%s count=%s",
        safe_log_value(provider),
        safe_log_value(repo_full_name),
        safe_log_value(state),
        len(pull_requests),
    )
    return {
        "provider": provider,
        "repo_full_name": repo_full_name,
        "pull_requests": pull_requests,
        "count": len(pull_requests),
    }


def _normalize_pr_row(row: dict[str, Any]) -> dict[str, Any]:
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    head = row.get("head") if isinstance(row.get("head"), dict) else {}
    base = row.get("base") if isinstance(row.get("base"), dict) else {}
    return {
        "number": row.get("number"),
        "title": row.get("title"),
        "state": row.get("state"),
        "url": row.get("html_url") or row.get("url"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "merged_at": row.get("merged_at"),
        "closed_at": row.get("closed_at"),
        "actor_login": user.get("login"),
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "base_ref": base.get("ref"),
    }


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
    provider = _normalize_provider(provider)
    repo_full_name = _validate_repo_full_name(repo_full_name)
    cache_key = f"{repo_full_name}/{pr_number}/{head_sha}" if head_sha else f"{repo_full_name}/{pr_number}"
    cached = queries.lookup_external_resource(provider, "pr_files", cache_key)
    if cached:
        logger.info("external.fetch_pr_files_cache_hit provider=%s key=%s", safe_log_value(provider), safe_log_value(cache_key))
        result = cached["content"]
    else:
        logger.info("external.fetch_pr_files_cache_miss provider=%s key=%s", safe_log_value(provider), safe_log_value(cache_key))
        result = await _fetch_pr_files_remote(provider, repo_full_name, pr_number)
        queries.write_external_resource(provider, "pr_files", cache_key, result, ttl_seconds=86400)
    if paths_filter:
        filters = [p for p in paths_filter if p]
        return {"files": [f for f in result.get("files", []) if any(p in f.get("path", "") for p in filters)]}
    return result
