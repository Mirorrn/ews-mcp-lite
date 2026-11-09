"""Tier registration, the send kill-switch and soft-delete defaults."""

from unittest.mock import MagicMock

import pytest

from tests.conftest import call, confirmed

pytestmark = pytest.mark.anyio

READ = {"list_folders", "search_messages", "get_message", "get_thread", "get_attachment",
        "get_mailbox_overview", "list_events", "get_event", "check_availability", "find_people",
        "get_contact", "get_oof_settings", "list_tasks", "waiting_on", "get_server_status"}
DRAFT = {"create_draft", "update_draft", "delete_draft", "update_messages", "move_messages",
         "create_event", "update_event", "update_task"}
FULL = {"send_draft", "respond_to_event", "cancel_event", "delete_messages", "set_oof"}


@pytest.mark.parametrize("tier,expected", [("read", READ), ("draft", READ | DRAFT),
                                           ("full", READ | DRAFT | FULL)])
async def test_tier_registers_exactly_its_tools(server_factory, tier, expected):
    mcp, ctx, _ = server_factory(ews_capability_tier=tier)
    names = {t.name for t in await mcp.list_tools()}
    assert names == expected
    assert set(ctx.registered) == expected


def test_tool_counts():
    assert (len(READ), len(DRAFT), len(FULL)) == (15, 8, 5)


async def test_default_tier_is_draft_and_sending_off(server_factory):
    mcp, ctx, _ = server_factory()
    assert ctx.settings.ews_capability_tier == "draft"
    assert ctx.settings.send_enabled is False
    assert "send_draft" not in {t.name for t in await mcp.list_tools()}


@pytest.mark.parametrize("tool,args", [
    ("send_draft", {"draft_id": "AAMkRAWDRAFTID"}),
    ("respond_to_event", {"event_id": "AAMkRAWEVENT", "response": "accept"}),
    ("cancel_event", {"event_id": "AAMkRAWEVENT"}),
    ("set_oof", {"state": "enabled", "internal_reply": "away"}),
])
async def test_send_class_refused_when_kill_switch_off(server_factory, tool, args):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=False)
    ok, err = await call(mcp, tool, args)
    assert not ok and err["code"] == "kill_switch"
    assert gw.calls == 0  # refused before touching Exchange


async def test_meeting_invitations_need_send_enabled(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="draft", send_enabled=False)
    ok, err = await call(mcp, "create_event", {
        "subject": "x", "start": "2030-01-01T10:00", "end": "2030-01-01T11:00",
        "attendees": ["a@example.com"], "send_invitations": True})
    assert not ok and err["code"] == "kill_switch" and gw.calls == 0


async def test_update_event_notify_needs_send_enabled(server_factory):
    mcp, _, gw = server_factory(send_enabled=False)
    ok, err = await call(mcp, "update_event", {"event_id": "AAMkRAWEVENT", "location": "x",
                                               "notify_attendees": True})
    assert not ok and err["code"] == "kill_switch" and gw.calls == 0


def _fake_item(account):
    item = MagicMock(name="item")
    account.fetch.return_value = [item]
    return item


async def test_delete_messages_defaults_to_recoverable_move(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=False)
    item = _fake_item(gw.mock_account)
    res = await confirmed(mcp, "delete_messages", {"ids": ["AAMkRAWMESSAGE"]})
    assert res["recoverable"] is True and res["deleted"] == ["AAMkRAWMESSAGE"]
    item.move_to_trash.assert_called_once()
    item.delete.assert_not_called()


async def test_delete_messages_permanent_is_explicit(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full")
    item = _fake_item(gw.mock_account)
    res = await confirmed(mcp, "delete_messages", {"ids": ["AAMkRAWMESSAGE"], "permanent": True})
    assert res["recoverable"] is False
    item.delete.assert_called_once()
    item.move_to_trash.assert_not_called()


async def test_delete_messages_caps_bulk_before_preview(server_factory):
    mcp, _, _ = server_factory(ews_capability_tier="full")
    ok, err = await call(mcp, "delete_messages", {"ids": [f"AAMk{i}" for i in range(51)]})
    assert not ok and err["code"] == "validation"


def _draft(account, to=("a@example.com",), body="Hello", subject="Hi"):
    item = MagicMock(name="draft")
    item.is_draft = True
    item.subject = subject
    item.to_recipients = [MagicMock(email_address=a) for a in to]
    item.cc_recipients = item.bcc_recipients = None
    item.text_body = body
    account.fetch.return_value = [item]
    return item


async def test_send_draft_allowed_when_enabled(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = _draft(gw.mock_account)
    res = await confirmed(mcp, "send_draft", {"draft_id": "AAMkRAWDRAFTID"})
    assert res["sent"] is True
    item.send.assert_called_once_with(save_copy=True)


async def test_stale_alias_is_clean_validation_error(server_factory):
    mcp, _, gw = server_factory()
    ok, err = await call(mcp, "get_message", {"id": "m99"})
    assert not ok and err["code"] == "validation" and "Re-run" in err["message"]
    assert gw.calls == 0


async def test_unexpected_exception_never_leaks_traceback(server_factory):
    mcp, _, gw = server_factory()
    gw.mock_account.fetch.side_effect = RuntimeError("boom")
    ok, err = await call(mcp, "get_message", {"id": "AAMkRAWMESSAGE"})
    assert not ok and err["code"] == "upstream_error" and "Traceback" not in err["message"]
