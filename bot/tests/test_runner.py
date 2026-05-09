from __future__ import annotations

import asyncio

import pytest

from agent import runner


def test_strip_pmo_prefix_handles_all_domain_servers():
    assert runner._strip_pmo_prefix("mcp__pmo_meta__today_iso") == "today_iso"
    assert runner._strip_pmo_prefix("mcp__pmo_calendar__schedule_meeting") == "schedule_meeting"
    assert runner._strip_pmo_prefix("mcp__pmo_bitable__append_action_items") == "append_action_items"
    assert runner._strip_pmo_prefix("mcp__pmo_doc__create_doc") == "create_doc"
    assert runner._strip_pmo_prefix("mcp__pmo_external__read_doc") == "read_doc"


def test_main_agent_options_use_dedicated_turn_limit_and_no_default_tools(monkeypatch):
    monkeypatch.setattr(runner.settings, "agent_max_turns", 80)

    options = runner._build_main_agent_options(runner.RequestContext())

    assert options.max_turns == 80
    assert options.tools == []
    assert options.include_partial_messages is True
    assert "mcp__pmo_meta__get_recent_turns" in options.allowed_tools


@pytest.mark.anyio
async def test_retry_async_retries_transient_boundary_failure(monkeypatch):
    attempts = 0
    sleeps: list[float] = []

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(runner.settings, "agent_api_retry_attempts", 3)
    monkeypatch.setattr(runner.settings, "agent_api_retry_initial_delay_seconds", 0.5)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    result = await runner._retry_async_boundary("sdk_query", "chat:user", flaky)

    assert result == "ok"
    assert attempts == 2
    assert sleeps == [0.5]


@pytest.mark.anyio
async def test_retry_async_does_not_retry_cancellation(monkeypatch):
    attempts = 0

    async def cancelled() -> None:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner.settings, "agent_api_retry_attempts", 3)

    with pytest.raises(asyncio.CancelledError):
        await runner._retry_async_boundary("sdk_query", "chat:user", cancelled)

    assert attempts == 1
