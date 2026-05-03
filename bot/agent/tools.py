"""Compatibility shim; v21 split tools into per-domain MCP modules."""
from __future__ import annotations

from agent.request_context import RequestContext
from agent.tools_meta import build_meta_mcp, build_meta_tools


def set_current_conversation(conversation_key: str) -> None:
    """No-op kept for old imports during rolling deploys."""


def build_pmo_mcp():
    return build_meta_mcp(RequestContext())
