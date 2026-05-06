from __future__ import annotations

import re
from typing import Any

from db import queries

_MENTION_RE = re.compile(r"(?<![\w.-])@([A-Za-z0-9][A-Za-z0-9_.-]{0,78})")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _login(value: Any) -> str:
    return str(value or "").strip().lower()


def _actor(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    sender = _as_dict(raw.get("sender") or raw.get("user"))
    login = _login(sender.get("login") or sender.get("username"))
    external_id = sender.get("id")
    external_id_str = str(external_id) if external_id is not None else None
    profile_id = queries.lookup_profile_by_external_login(provider, login, external_id=external_id_str) if login else None
    return {"login": login, "id": external_id_str, "profile_id": profile_id}


def _repo(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    repo = _as_dict(raw.get("repository") or raw.get("repo"))
    full_name = str(repo.get("full_name") or repo.get("fullName") or "").strip()
    project_root = queries.lookup_project_root_for_repo(provider, full_name) if full_name else None
    return {
        "full_name": full_name,
        "default_branch": repo.get("default_branch") or repo.get("default_branch_name"),
        "project_root": project_root,
    }


def _mentioned_profile_ids(provider: str, text: str) -> list[str]:
    profile_ids: list[str] = []
    seen: set[str] = set()
    for raw_login in _MENTION_RE.findall(text or ""):
        login = _login(raw_login)
        profile_id = queries.lookup_profile_by_external_login(provider, login)
        if profile_id and profile_id not in seen:
            profile_ids.append(profile_id)
            seen.add(profile_id)
    return profile_ids


def _normalize_pull_request(provider: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    action_raw = str(raw.get("action") or "").lower()
    pr = _as_dict(raw.get("pull_request"))
    merged = bool(pr.get("merged"))
    if action_raw == "closed" and merged:
        action = "merged"
    elif action_raw in {"opened", "synchronize"}:
        action = action_raw
    else:
        return None

    repo = _repo(provider, raw)
    occurred_at = (
        pr.get("merged_at")
        if action == "merged"
        else pr.get("updated_at") or pr.get("created_at")
    )
    return {
        "event_type": "pull_request",
        "action": action,
        "occurred_at": occurred_at,
        "project_root": repo.get("project_root"),
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "html_url": pr.get("html_url") or pr.get("url") or "",
            "diff_url": pr.get("diff_url") or "",
            "base_branch": (_as_dict(pr.get("base")).get("ref") or ""),
            "head_branch": (_as_dict(pr.get("head")).get("ref") or ""),
            "merged": merged,
            "merged_at": pr.get("merged_at"),
            "files_changed_count": pr.get("changed_files") or pr.get("files_changed"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
        },
        "repo": repo,
        "actor": _actor(provider, raw),
    }


def _normalize_push(provider: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    repo = _repo(provider, raw)
    commits = raw.get("commits") or []
    if not isinstance(commits, list):
        commits = []
    head_commit = _as_dict(raw.get("head_commit"))
    return {
        "event_type": "push",
        "ref": raw.get("ref") or "",
        "before": raw.get("before"),
        "after": raw.get("after"),
        "occurred_at": head_commit.get("timestamp") or raw.get("updated_at"),
        "project_root": repo.get("project_root"),
        "commits_count": len(commits),
        "commit_summaries": [str(c.get("message") or "").splitlines()[0][:160] for c in commits[:20] if isinstance(c, dict)],
        "repo": repo,
        "actor": _actor(provider, raw),
    }


def _normalize_release(provider: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    if str(raw.get("action") or "").lower() != "published":
        return None
    rel = _as_dict(raw.get("release"))
    repo = _repo(provider, raw)
    return {
        "event_type": "release",
        "action": "published",
        "occurred_at": rel.get("published_at") or rel.get("created_at"),
        "project_root": repo.get("project_root"),
        "release": {
            "tag_name": rel.get("tag_name") or "",
            "name": rel.get("name") or "",
            "body": rel.get("body") or "",
            "html_url": rel.get("html_url") or "",
        },
        "repo": repo,
        "actor": _actor(provider, raw),
    }


def _normalize_issue_comment(provider: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    if str(raw.get("action") or "").lower() != "created":
        return None
    comment = _as_dict(raw.get("comment"))
    issue = _as_dict(raw.get("issue"))
    repo = _repo(provider, raw)
    return {
        "event_type": "issue_comment",
        "action": "created",
        "occurred_at": comment.get("created_at"),
        "project_root": repo.get("project_root"),
        "comment": {
            "id": comment.get("id"),
            "body": comment.get("body") or "",
            "html_url": comment.get("html_url") or "",
        },
        "issue": {"number": issue.get("number"), "title": issue.get("title") or ""},
        "repo": repo,
        "actor": _actor(provider, raw),
        "mentioned_profile_ids": _mentioned_profile_ids(provider, comment.get("body") or ""),
    }


def normalize_github(event_type: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    handlers = {
        "pull_request": _normalize_pull_request,
        "push": _normalize_push,
        "release": _normalize_release,
        "issue_comment": _normalize_issue_comment,
    }
    handler = handlers.get(event_type)
    return handler("github", raw) if handler else None


def normalize_gitea(event_type: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    handlers = {
        "pull_request": _normalize_pull_request,
        "push": _normalize_push,
        "release": _normalize_release,
        "issue_comment": _normalize_issue_comment,
    }
    handler = handlers.get(event_type)
    return handler("gitea", raw) if handler else None
