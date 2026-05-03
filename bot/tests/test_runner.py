from __future__ import annotations

from agent import runner


def test_strip_pmo_prefix_handles_all_domain_servers():
    assert runner._strip_pmo_prefix("mcp__pmo_meta__today_iso") == "today_iso"
    assert runner._strip_pmo_prefix("mcp__pmo_calendar__schedule_meeting") == "schedule_meeting"
    assert runner._strip_pmo_prefix("mcp__pmo_bitable__append_action_items") == "append_action_items"
    assert runner._strip_pmo_prefix("mcp__pmo_doc__create_doc") == "create_doc"
    assert runner._strip_pmo_prefix("mcp__pmo_external__read_doc") == "read_doc"
