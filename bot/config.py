"""Centralized config — env-driven, validated at startup."""
from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Anthropic-compatible LLM backend (we use 火山方舟 Coding Plan) ──
    anthropic_auth_token: str
    anthropic_base_url: str = "https://ark.cn-beijing.volces.com/api/coding"
    anthropic_model: str = "ark-code-latest"
    anthropic_default_opus_model: str = "ark-code-latest"
    anthropic_default_sonnet_model: str = "ark-code-latest"
    anthropic_default_haiku_model: str = "ark-code-latest"
    api_timeout_ms: str = "300000"
    claude_code_disable_nonessential_traffic: str = "1"

    # ── Feishu (lark) ──
    feishu_app_id: str
    feishu_app_secret: str
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""

    # ── Supabase ──
    # anon_key  — RLS-respecting reads of public tables (profiles, turns).
    # service_role_key — bypasses RLS; used ONLY for feishu_links lookups
    #   so the bot can resolve sender_open_id → linked user_id.
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str = ""

    # ── Web base URL — used in answers to link out to /u/<handle> ──
    web_base_url: str = "https://pmo-agent-sigma.vercel.app"

    # ── Image generation (Volcengine doubao-seedream) ──
    # We reuse the ARK API key (anthropic_auth_token) by default —
    # it's the same Volcengine credential. The exact model ID has to
    # match what's enabled in the Volcengine console for this account.
    image_model: str = "doubao-seedream-5-0-260128"
    image_api_url: str = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    image_max_per_conversation_per_hour: int = 5

    # ── Misc ──
    log_level: str = "INFO"
    agent_max_duration_seconds: int = 120

    @property
    def cors_origins(self) -> List[str]:
        return ["*"]


settings = Settings()  # type: ignore[call-arg]
