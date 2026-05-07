from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from config import settings
from db import queries
from external.logging import safe_log_value
from external.redaction import redact_text

logger = logging.getLogger(__name__)
_REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TEXT_FILE_RE = re.compile(
    r"\.(c|cc|cpp|cs|css|go|h|hpp|html|js|jsx|json|md|mjs|py|rs|sh|sql|swift|toml|ts|tsx|txt|vue|xml|yaml|yml)$"
)


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


def _contents_url(provider: str, repo_full_name: str, path: str = "") -> str:
    path = (path or "").strip().lstrip("/")
    encoded_path = quote(path, safe="/")
    suffix = f"/contents/{encoded_path}" if encoded_path else "/contents/"
    return f"{_api_base(provider)}/repos/{repo_full_name}{suffix}"


def _repo_url(provider: str, repo_full_name: str, suffix: str = "") -> str:
    return f"{_api_base(provider)}/repos/{repo_full_name}{suffix}"


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


async def query_repo(
    provider: str,
    repo_full_name: str,
    *,
    kind: str,
    ref: str | None = None,
    path: str | None = None,
    query: str | None = None,
    state: str | None = None,
    limit: int = 10,
    max_chars: int = 12000,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    repo_full_name = _validate_repo_full_name(repo_full_name)
    kind = (kind or "").strip().lower()
    limit = max(1, min(int(limit or 10), 100))
    max_chars = max(200, min(int(max_chars or 12000), 20000))
    async with httpx.AsyncClient(timeout=20) as client:
        if kind == "overview":
            repo = await _fetch_repo_overview(client, provider, repo_full_name)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, "repo": repo}
        if kind == "prs":
            return {"kind": kind, **await list_pull_requests(provider, repo_full_name, state=state or "all", limit=limit)}
        if kind == "commits":
            commits = await _fetch_commits(client, provider, repo_full_name, ref=ref, limit=limit)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, "commits": commits, "count": len(commits)}
        if kind == "branches":
            branches = await _fetch_branches(client, provider, repo_full_name, limit=limit)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, "branches": branches, "count": len(branches)}
        if kind == "releases":
            releases = await _fetch_releases(client, provider, repo_full_name, limit=limit)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, "releases": releases, "count": len(releases)}
        if kind == "tree":
            entries = await _fetch_tree(client, provider, repo_full_name, ref=ref or "main", path=path or "", limit=limit)
            return {
                "kind": kind,
                "provider": provider,
                "repo_full_name": repo_full_name,
                "ref": ref or "main",
                **entries,
            }
        if kind == "file":
            file_path = (path or "").strip().lstrip("/")
            if not file_path:
                raise ValueError("path is required for kind=file")
            file_result = await _fetch_file(client, provider, repo_full_name, file_path, ref=ref, max_chars=max_chars)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, **file_result}
        if kind == "search":
            search_result = await _search_repo(client, provider, repo_full_name, query=query or "", ref=ref or "main", limit=limit)
            return {"kind": kind, "provider": provider, "repo_full_name": repo_full_name, "ref": ref or "main", **search_result}
    raise ValueError("kind must be one of overview, prs, commits, branches, releases, tree, file, search")


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


async def _fetch_json(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    res = await _get_with_retries(client, url, headers=_auth_headers(provider), params=params)
    res.raise_for_status()
    return res.json()


async def _fetch_repo_overview(client: httpx.AsyncClient, provider: str, repo_full_name: str) -> dict[str, Any]:
    row = await _fetch_json(client, provider, _repo_url(provider, repo_full_name))
    return {
        "full_name": row.get("full_name") or repo_full_name,
        "description": row.get("description"),
        "private": row.get("private"),
        "default_branch": row.get("default_branch"),
        "html_url": row.get("html_url") or row.get("html_url"),
        "updated_at": row.get("updated_at"),
        "stars_count": row.get("stars_count") or row.get("stargazers_count"),
        "forks_count": row.get("forks_count") or row.get("forks"),
        "open_issues_count": row.get("open_issues_count"),
    }


async def _fetch_commits(
    client: httpx.AsyncClient,
    provider: str,
    repo_full_name: str,
    *,
    ref: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit" if provider == "gitea" else "per_page": limit}
    if ref:
        params["sha"] = ref
    rows = await _fetch_json(client, provider, _repo_url(provider, repo_full_name, "/commits"), params=params)
    return [_normalize_commit_row(row) for row in (rows[:limit] if isinstance(rows, list) else [])]


def _normalize_commit_row(row: dict[str, Any]) -> dict[str, Any]:
    commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else row.get("author") if isinstance(row.get("author"), dict) else {}
    return {
        "sha": row.get("sha") or row.get("id"),
        "message": (commit.get("message") or row.get("message") or "").splitlines()[0],
        "created_at": row.get("created") or commit.get("committer", {}).get("date") if isinstance(commit.get("committer"), dict) else row.get("created"),
        "author_login": (row.get("author") or {}).get("login") if isinstance(row.get("author"), dict) else None,
        "author_name": author.get("name") or author.get("login"),
        "url": row.get("html_url") or row.get("url"),
    }


async def _fetch_branches(client: httpx.AsyncClient, provider: str, repo_full_name: str, *, limit: int) -> list[dict[str, Any]]:
    rows = await _fetch_json(client, provider, _repo_url(provider, repo_full_name, "/branches"))
    branches = []
    for row in rows[:limit] if isinstance(rows, list) else []:
        commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
        branches.append(
            {
                "name": row.get("name"),
                "commit_sha": commit.get("id") or commit.get("sha"),
                "commit_message": (commit.get("message") or "").splitlines()[0],
            }
        )
    return branches


async def _fetch_releases(client: httpx.AsyncClient, provider: str, repo_full_name: str, *, limit: int) -> list[dict[str, Any]]:
    params = {"limit" if provider == "gitea" else "per_page": limit}
    rows = await _fetch_json(client, provider, _repo_url(provider, repo_full_name, "/releases"), params=params)
    return [
        {
            "tag_name": row.get("tag_name"),
            "name": row.get("name"),
            "draft": row.get("draft"),
            "prerelease": row.get("prerelease"),
            "published_at": row.get("published_at"),
            "url": row.get("html_url") or row.get("url"),
        }
        for row in (rows[:limit] if isinstance(rows, list) else [])
    ]


async def _fetch_tree(
    client: httpx.AsyncClient,
    provider: str,
    repo_full_name: str,
    *,
    ref: str,
    path: str,
    limit: int,
) -> dict[str, Any]:
    ref = quote(ref or "main", safe="")
    row = await _fetch_json(
        client,
        provider,
        _repo_url(provider, repo_full_name, f"/git/trees/{ref}"),
        params={"recursive": 1},
    )
    prefix = (path or "").strip().strip("/")
    entries = []
    for item in row.get("tree", []) if isinstance(row, dict) else []:
        item_path = str(item.get("path") or "")
        if prefix and item_path != prefix and not item_path.startswith(prefix + "/"):
            continue
        entries.append({"path": item_path, "type": item.get("type"), "size": item.get("size")})
    return {"entries": entries[:limit], "count": min(len(entries), limit), "truncated": len(entries) > limit}


async def _fetch_file(
    client: httpx.AsyncClient,
    provider: str,
    repo_full_name: str,
    path: str,
    *,
    ref: str | None,
    max_chars: int,
) -> dict[str, Any]:
    params = {"ref": ref} if ref else None
    row = await _fetch_json(client, provider, _contents_url(provider, repo_full_name, path), params=params)
    if isinstance(row, list):
        raise ValueError("path is a directory, use kind=tree")
    content = _decode_content_blob(row)
    redacted, redaction_hits = redact_text(content)
    truncated = len(redacted) > max_chars
    if truncated:
        redacted = redacted[:max_chars] + f"\n\n[... file truncated, {len(redacted) - max_chars} chars omitted]"
    return {
        "path": row.get("path") or path,
        "sha": row.get("sha"),
        "size": row.get("size"),
        "content": redacted,
        "truncated": truncated,
        "redaction_hits": redaction_hits,
    }


def _decode_content_blob(row: dict[str, Any]) -> str:
    content = row.get("content") or ""
    if row.get("encoding") == "base64":
        return base64.b64decode(str(content).encode("utf-8"), validate=False).decode("utf-8", "replace")
    return str(content)


async def _search_repo(
    client: httpx.AsyncClient,
    provider: str,
    repo_full_name: str,
    *,
    query: str,
    ref: str,
    limit: int,
) -> dict[str, Any]:
    needle = (query or "").strip()
    if len(needle) < 2:
        raise ValueError("query must be at least 2 characters")
    tree = await _fetch_tree(client, provider, repo_full_name, ref=ref, path="", limit=2000)
    matches: list[dict[str, Any]] = []
    lowered = needle.lower()
    candidate_files = []
    for entry in tree["entries"]:
        path = entry["path"]
        if lowered in path.lower():
            matches.append({"path": path, "line": None, "excerpt": path, "match_type": "path"})
            if len(matches) >= limit:
                return {"query": needle, "matches": matches, "count": len(matches), "truncated": tree["truncated"]}
        if entry.get("type") == "blob" and _looks_text_path(path) and int(entry.get("size") or 0) <= 300_000:
            candidate_files.append(path)
    scanned = 0
    for path in candidate_files[:80]:
        if len(matches) >= limit:
            break
        scanned += 1
        try:
            file_result = await _fetch_file(client, provider, repo_full_name, path, ref=ref, max_chars=20000)
        except Exception:
            continue
        for line_no, line in enumerate(str(file_result.get("content") or "").splitlines(), start=1):
            if lowered in line.lower():
                matches.append({"path": path, "line": line_no, "excerpt": line[:300], "match_type": "content"})
                break
    return {"query": needle, "matches": matches[:limit], "count": len(matches[:limit]), "files_scanned": scanned, "truncated": tree["truncated"]}


def _looks_text_path(path: str) -> bool:
    return bool(_TEXT_FILE_RE.search(path.lower())) or "/" not in path and "." not in path


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
