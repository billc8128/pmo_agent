from __future__ import annotations

from config import Settings


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_auth_token="token",
        feishu_app_id="app",
        feishu_app_secret="secret",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
    )


def test_agent_watchdog_and_turn_defaults_are_separate():
    settings = _settings()

    assert settings.agent_idle_timeout_seconds == 120
    assert settings.agent_max_wall_seconds == 1800
    assert settings.agent_max_turns == 120


def test_small_agent_turn_defaults_are_not_single_digit_for_research():
    settings = _settings()

    assert settings.renderer_max_turns == 12
    assert settings.investigator_max_turns == 20


def test_agent_api_retry_defaults_are_local_to_boundaries():
    settings = _settings()

    assert settings.agent_api_retry_attempts == 3
    assert settings.agent_api_retry_initial_delay_seconds == 0.5
