from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent.request_context import RequestContext
from db import queries
from feishu import contact

_CHAT_MEMBER_CACHE_TTL_SECONDS = 6 * 60 * 60
_chat_member_cache: dict[str, tuple[float, set[str]]] = {}


class RoutingPermissionError(ValueError):
    pass


@dataclass
class SubscriptionTarget:
    target_kind: str
    target_id: str
    target_user_open_id: str | None = None
    consent_anchor: str | None = None

    @property
    def delivery_kind(self) -> str | None:
        if self.target_kind == "user_dm":
            return "feishu_user"
        if self.target_kind in {"chat", "mention_in_chat"}:
            return "feishu_chat"
        return None

    @property
    def mention_open_id(self) -> str | None:
        return self.target_user_open_id if self.target_kind == "mention_in_chat" else None


@dataclass
class DeliveryRoute:
    allowed: bool
    delivery_kind: str | None = None
    delivery_target: str | None = None
    mention_open_id: str | None = None
    suppressed_by: str | None = None
    reason: str | None = None


def _profile_label(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "这个用户"
    if profile.get("display_name") and profile.get("handle"):
        return f"{profile['display_name']} / @{profile['handle']}"
    if profile.get("handle"):
        return f"@{profile['handle']}"
    return profile.get("display_name") or "这个用户"


def _subscription_value(sub: Any, field: str) -> Any:
    if isinstance(sub, dict):
        return sub.get(field)
    return getattr(sub, field, None)


def _default_target(scope_kind: str, scope_id: str) -> SubscriptionTarget:
    if scope_kind == "chat":
        return SubscriptionTarget(target_kind="chat", target_id=scope_id)
    return SubscriptionTarget(target_kind="user_dm", target_id=scope_id)


def _resolve_profile(handle_or_id: str) -> dict[str, Any] | None:
    raw = (handle_or_id or "").strip()
    if not raw:
        return None
    if raw.count("-") >= 4 and len(raw) >= 32:
        return queries.lookup_profile_by_user_id(raw)
    return queries.lookup_profile_by_handle_or_display(raw)


def _feishu_open_id_for_profile(user_id: str) -> str | None:
    linked = queries.feishu_link_for_user_id(user_id)
    return (linked or {}).get("open_id")


async def _chat_member_open_ids(chat_id: str, *, use_cache: bool = True) -> set[str]:
    now = time.time()
    cached = _chat_member_cache.get(chat_id)
    if use_cache and cached and now - cached[0] < _CHAT_MEMBER_CACHE_TTL_SECONDS:
        return set(cached[1])
    members = set(await contact.list_chat_member_open_ids(chat_id))
    _chat_member_cache[chat_id] = (now, members)
    return members


def invalidate_chat_member_cache(chat_id: str | None = None) -> None:
    if chat_id:
        _chat_member_cache.pop(chat_id, None)
    else:
        _chat_member_cache.clear()


async def _is_chat_member(chat_id: str, user_id: str, *, use_cache: bool = True) -> bool:
    open_id = _feishu_open_id_for_profile(user_id)
    if not open_id:
        return False
    return open_id in await _chat_member_open_ids(chat_id, use_cache=use_cache)


async def _both_chat_members(chat_id: str, user_a: str, user_b: str, *, use_cache: bool = True) -> bool:
    open_a = _feishu_open_id_for_profile(user_a)
    open_b = _feishu_open_id_for_profile(user_b)
    if not open_a or not open_b:
        return False
    members = await _chat_member_open_ids(chat_id, use_cache=use_cache)
    return open_a in members and open_b in members


def _explicit_consent_anchor(target_user_id: str, source_user_id: str) -> str | None:
    consent = queries.get_active_target_consent(target_user_id, source_user_id)
    return f"explicit:{consent['id']}" if consent and consent.get("id") else None


async def resolve_subscription_target(
    ctx: RequestContext,
    *,
    scope_kind: str,
    scope_id: str,
    args: dict[str, Any],
) -> SubscriptionTarget:
    target_kind = str(args.get("target_kind") or "").strip().lower()
    if not target_kind:
        return _default_target(scope_kind, scope_id)
    if target_kind not in {"user_dm", "chat", "mention_in_chat"}:
        raise RoutingPermissionError("target_kind must be user_dm, chat, or mention_in_chat")

    source_user_id = ctx.asker_user_id
    if not source_user_id:
        raise RoutingPermissionError("你还没绑定 pmo_agent 账号，不能创建带路由目标的规则")

    if target_kind == "chat":
        target_chat_id = str(args.get("target_chat_id") or ctx.chat_id or "").strip()
        if not target_chat_id:
            raise RoutingPermissionError("target_kind=chat 需要 target_chat_id，或在群里创建这条规则")
        if scope_kind == "chat" and target_chat_id != scope_id:
            raise RoutingPermissionError("群规则只能发回当前群，不能跨群路由")
        if scope_kind == "user" and not await _is_chat_member(target_chat_id, source_user_id):
            raise RoutingPermissionError("你不是这个群的当前成员，不能把个人规则发到这个群")
        return SubscriptionTarget(target_kind="chat", target_id=target_chat_id)

    target_profile: dict[str, Any] | None
    target_handle = str(args.get("target_handle") or args.get("target_user") or "").strip()
    if target_handle:
        target_profile = _resolve_profile(target_handle)
        if not target_profile:
            raise RoutingPermissionError(f"找不到目标用户 {target_handle!r}")
    else:
        if target_kind == "user_dm" and scope_kind == "user":
            target_profile = queries.lookup_profile_by_user_id(scope_id)
        else:
            target_profile = queries.lookup_profile_by_user_id(source_user_id)
    if not target_profile or not target_profile.get("id"):
        raise RoutingPermissionError("找不到目标用户")
    target_user_id = str(target_profile["id"])

    if target_kind == "user_dm":
        if target_user_id == source_user_id:
            return SubscriptionTarget(target_kind="user_dm", target_id=target_user_id)
        if scope_kind == "chat" and await _both_chat_members(scope_id, source_user_id, target_user_id):
            return SubscriptionTarget(
                target_kind="user_dm",
                target_id=target_user_id,
                consent_anchor=f"chat:{scope_id}",
            )
        anchor = _explicit_consent_anchor(target_user_id, source_user_id)
        if anchor:
            return SubscriptionTarget(target_kind="user_dm", target_id=target_user_id, consent_anchor=anchor)
        raise RoutingPermissionError(f"{_profile_label(target_profile)} 还没同意接收你创建的私聊通知")

    target_chat_id = str(args.get("target_chat_id") or ctx.chat_id or "").strip()
    if not target_chat_id:
        raise RoutingPermissionError("target_kind=mention_in_chat 需要 target_chat_id，或在群里创建这条规则")
    target_open_id = _feishu_open_id_for_profile(target_user_id)
    if not target_open_id:
        raise RoutingPermissionError(f"{_profile_label(target_profile)} 还没绑定飞书，不能 @ta")
    if scope_kind == "chat" and target_chat_id != scope_id:
        raise RoutingPermissionError("群规则只能在当前群里 @ 人，不能跨群路由")
    if not await _is_chat_member(target_chat_id, target_user_id):
        raise RoutingPermissionError(f"{_profile_label(target_profile)} 不是这个群的当前成员，不能 @ta")
    if scope_kind == "user" and not await _is_chat_member(target_chat_id, source_user_id):
        raise RoutingPermissionError("你不是这个群的当前成员，不能把个人规则发到这个群")
    if target_user_id == source_user_id:
        anchor = None
    elif scope_kind == "chat":
        anchor = f"chat:{scope_id}"
    else:
        anchor = _explicit_consent_anchor(target_user_id, source_user_id)
        if not anchor:
            raise RoutingPermissionError(f"{_profile_label(target_profile)} 还没同意接收你创建的 @ 通知")
    return SubscriptionTarget(
        target_kind="mention_in_chat",
        target_id=target_chat_id,
        target_user_open_id=target_open_id,
        consent_anchor=anchor,
    )


def _owner_user_for_subscription(sub: Any) -> str | None:
    scope_kind = _subscription_value(sub, "scope_kind")
    if scope_kind == "user":
        return str(_subscription_value(sub, "scope_id") or "") or None
    return str(_subscription_value(sub, "created_by") or "") or None


async def _consent_still_valid(sub: Any, target_user_id: str) -> bool:
    source_user_id = _owner_user_for_subscription(sub)
    if not source_user_id or source_user_id == target_user_id:
        return True
    anchor = str(_subscription_value(sub, "consent_anchor") or "")
    if anchor.startswith("explicit:"):
        consent = queries.get_target_consent(anchor.split(":", 1)[1])
        return bool(
            consent
            and not consent.get("revoked_at")
            and str(consent.get("target_user_id")) == target_user_id
            and str(consent.get("source_user_id")) == source_user_id
        )
    if anchor.startswith("chat:"):
        return await _both_chat_members(anchor.split(":", 1)[1], source_user_id, target_user_id, use_cache=False)
    return False


async def route_for_subscription_delivery(sub: Any) -> DeliveryRoute:
    target_kind = _subscription_value(sub, "target_kind")
    target_id = _subscription_value(sub, "target_id")
    if not target_kind or not target_id:
        default = _default_target(str(_subscription_value(sub, "scope_kind")), str(_subscription_value(sub, "scope_id")))
        target_kind = default.target_kind
        target_id = default.target_id

    if target_kind == "chat":
        return DeliveryRoute(True, delivery_kind="feishu_chat", delivery_target=str(target_id))

    if target_kind == "user_dm":
        target_user_id = str(target_id)
        if not await _consent_still_valid(sub, target_user_id):
            return DeliveryRoute(False, suppressed_by="permission_revoked", reason="target consent or chat membership is no longer valid")
        open_id = _feishu_open_id_for_profile(target_user_id)
        if not open_id:
            return DeliveryRoute(False, suppressed_by="no_delivery_target", reason="target user has no Feishu binding")
        return DeliveryRoute(True, delivery_kind="feishu_user", delivery_target=open_id)

    if target_kind == "mention_in_chat":
        mention_open_id = str(_subscription_value(sub, "target_user_open_id") or "")
        target_profile = queries.lookup_by_feishu_open_id(mention_open_id) if mention_open_id else None
        target_user_id = str((target_profile or {}).get("user_id") or "")
        if not mention_open_id or not target_user_id:
            return DeliveryRoute(False, suppressed_by="no_delivery_target", reason="mention target has no Feishu binding")
        if mention_open_id not in await _chat_member_open_ids(str(target_id), use_cache=False):
            return DeliveryRoute(False, suppressed_by="permission_revoked", reason="mention target is no longer a chat member")
        if target_user_id and not await _consent_still_valid(sub, target_user_id):
            return DeliveryRoute(False, suppressed_by="permission_revoked", reason="target consent or chat membership is no longer valid")
        return DeliveryRoute(
            True,
            delivery_kind="feishu_chat",
            delivery_target=str(target_id),
            mention_open_id=mention_open_id or None,
        )

    return DeliveryRoute(False, suppressed_by="no_delivery_target", reason=f"unsupported target_kind={target_kind!r}")
