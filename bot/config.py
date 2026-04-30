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

    # ── Supabase (read-only via anon key; RLS allows public select) ──
    supabase_url: str
    supabase_anon_key: str

    # ── Web base URL — used in answers to link out to /u/<handle> ──
    web_base_url: str = "https://pmo-agent-sigma.vercel.app"

    # ── Misc ──
    log_level: str = "INFO"
    agent_max_duration_seconds: int = 120

    @property
    def cors_origins(self) -> List[str]:
        return ["*"]


settings = Settings()  # type: ignore[call-arg]
