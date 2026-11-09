"""Dispatcher checks: ordering, circuit breaker, startup gate, recipient guard, two-phase confirm."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from ews_mcp_lite.app import build_app
from ews_mcp_lite.config import Settings
from ews_mcp_lite.errors import Failure
from ews_mcp_lite.ews import Gateway
from ews_mcp_lite.policy import CircuitBreaker, Confirmer, check_recipients
from ews_mcp_lite.registry import ToolSpec, dispatch
from tests.conftest import call, confirmed, make_settings

pytestmark = pytest.mark.anyio


def _draft(account, to=("a@example.com",), body="Hello", subject="Hi", cc=(), bcc=()):
    item = MagicMock(name="draft")
    item.is_draft = True
    item.subject = subject
    item.to_recipients = [MagicMock(email_address=a) for a in to]
    item.cc_recipients = [MagicMock(email_address=a) for a in cc] or None
    item.bcc_recipients = [MagicMock(email_address=a) for a in bcc] or None
    item.text_body = body
    account.fetch.return_value = [item]
    return item


def _open_circuit(gw):
    for _ in range(gw.circuit.threshold):
        gw.circuit.record(Failure("upstream_unavailable", "down"))


# ------------------------------------------------------------------ ordering


async def test_order_kill_switch_beats_circuit_and_gate(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=False)
    _open_circuit(gw)
    gw.state = "connecting"
    ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    assert not ok and err["code"] == "kill_switch"


async def test_order_tier_before_circuit(server_factory):
    _, ctx, gw = server_factory(ews_capability_tier="read")
    _open_circuit(gw)

    async def handler(**_):
        raise AssertionError("handler must not run")
    spec = ToolSpec("x", "draft", "write", handler, "")
    with pytest.raises(Failure) as e:
        await dispatch(ctx, spec, {})
    assert e.value.code == "tier_blocked"


async def test_order_circuit_before_gate(server_factory):
    mcp, _, gw = server_factory()
    _open_circuit(gw)
    gw.state = "connecting"
    ok, err = await call(mcp, "get_message", {"id": "AAMkRAW"})
    assert not ok and err["code"] == "upstream_unavailable" and "paused" in err["message"]
    assert err["retry_after_s"] >= 1


async def test_order_gate_before_recipient_guard(server_factory):
    mcp, _, gw = server_factory(ews_recipient_denylist="*@evil.example")
    gw.state = "connecting"
    ok, err = await call(mcp, "create_draft", {"to": ["x@evil.example"], "body": "b"})
    assert not ok and err["code"] == "upstream_unavailable"


async def test_order_recipient_guard_before_confirm(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True,
                                ews_recipient_denylist="*@evil.example")
    item = _draft(gw.mock_account, to=("boss@evil.example",))
    ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    assert not ok and err["code"] == "recipient_blocked"  # no token was issued
    item.send.assert_not_called()


async def test_preview_executes_nothing(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = _draft(gw.mock_account)
    ok, res = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    assert ok and res["requires_confirmation"] and res["preview"]["to"] == ["a@example.com"]
    item.send.assert_not_called()
    ok, res = await call(mcp, "delete_messages", {"ids": ["AAMkRAW"]})
    assert ok and res["requires_confirmation"]
    item.move_to_trash.assert_not_called()
    item.delete.assert_not_called()


# ------------------------------------------------------------------ circuit breaker


async def test_circuit_opens_after_threshold_and_stops_calling(server_factory):
    mcp, _, gw = server_factory(circuit_failure_threshold=3, circuit_open_seconds=60)
    gw.mock_account.fetch.side_effect = ConnectionError("unreachable")
    for _ in range(3):
        ok, err = await call(mcp, "get_message", {"id": "AAMkRAW"})
        assert err["code"] == "upstream_unavailable"
    calls_before = gw.mock_account.fetch.call_count
    ok, err = await call(mcp, "get_message", {"id": "AAMkRAW"})
    assert not ok and "paused" in err["message"] and err["retry_after_s"] > 0
    assert gw.mock_account.fetch.call_count == calls_before  # refused without touching EWS


async def test_circuit_success_resets_counter(server_factory):
    mcp, _, gw = server_factory(circuit_failure_threshold=3)
    gw.mock_account.fetch.side_effect = [ConnectionError("x"), ConnectionError("x"), [MagicMock()],
                                         ConnectionError("x"), ConnectionError("x")]
    for _ in range(5):
        await call(mcp, "get_message", {"id": "AAMkRAW"})
    assert gw.circuit.failures == 2 and gw.circuit.open_until is None


async def test_policy_errors_do_not_count(server_factory):
    mcp, _, gw = server_factory(circuit_failure_threshold=2)
    gw.mock_account.fetch.return_value = [None]  # -> not_found from the handler
    for _ in range(5):
        ok, err = await call(mcp, "get_message", {"id": "AAMkRAW"})
        assert err["code"] == "not_found"
    for _ in range(5):
        await call(mcp, "get_message", {"id": "m42"})  # stale alias: validation
    assert gw.circuit.failures == 0 and gw.circuit.open_until is None


def test_circuit_half_opens_after_cooldown():
    now = [0.0]
    cb = CircuitBreaker(2, 30, clock=lambda: now[0])
    cb.record(Failure("upstream_error", "x"))
    cb.record(Failure("upstream_error", "x"))
    with pytest.raises(Failure):
        cb.check()
    now[0] = 31
    cb.check()  # cooled down: the next call is let through


# ------------------------------------------------------------------ startup gate


async def test_cold_gate_fails_fast_and_status_still_works(server_factory):
    mcp, _, gw = server_factory()
    gw.state = "connecting"
    ok, err = await call(mcp, "search_messages", {})
    assert not ok and err["code"] == "upstream_unavailable" and "get_server_status" in err["hint"]
    assert gw.calls == 0
    ok, status = await call(mcp, "get_server_status", {})
    assert ok and status["connection"]["state"] == "connecting"


async def test_warmup_retries_until_warm(monkeypatch):
    gw = Gateway(make_settings(warmup_max_backoff_seconds=0))
    results = iter([False, False, True])

    def fake_connect():
        ok = next(results)
        gw.attempts += 1
        if ok:
            gw.state = "warm"
        return ok
    monkeypatch.setattr(gw, "connect", fake_connect)
    await gw.warmup()
    assert gw.state == "warm" and gw.attempts == 3


def test_auth_failure_marks_failed_and_gate_reports_it():
    gw = Gateway(make_settings())
    gw.account = MagicMock(side_effect=Failure("auth_failed", "401 Unauthorized"))
    assert gw.connect() is False and gw.state == "failed"
    with pytest.raises(Failure) as e:
        gw.gate()
    assert e.value.code == "auth_failed"


def test_server_starts_when_exchange_is_unreachable():
    settings = make_settings(ews_server_url="https://127.0.0.1:9/EWS/Exchange.asmx")
    with TestClient(build_app(settings), base_url="http://127.0.0.1:8000") as client:
        r = client.get("/health")
    assert r.status_code == 200 and r.json()["connection"]["state"] in ("connecting", "failed")


# ------------------------------------------------------------------ recipient guard


def test_recipient_globs_case_insensitive():
    check_recipients(["Alice@Corp.Example"], ["*@corp.example"], [], "t")
    with pytest.raises(Failure) as e:
        check_recipients(["x@other.example"], ["*@corp.example"], [], "t")
    assert e.value.code == "recipient_blocked"
    with pytest.raises(Failure):
        check_recipients(["CEO@corp.example"], ["*@corp.example"], ["ceo@*"], "t")


@pytest.mark.parametrize("tool,args", [
    ("create_draft", {"to": ["ok@corp.example"], "cc": ["x@other.example"], "body": "b"}),
    ("update_draft", {"draft_id": "AAMkRAW", "bcc": ["x@other.example"]}),
    ("create_event", {"subject": "s", "start": "2030-01-01T10:00", "end": "2030-01-01T11:00",
                      "attendees": ["x@other.example"]}),
])
async def test_recipient_guard_on_arguments(server_factory, tool, args):
    mcp, _, gw = server_factory(ews_recipient_allowlist="*@corp.example")
    ok, err = await call(mcp, tool, args)
    assert not ok and err["code"] == "recipient_blocked"
    gw.mock_account.fetch.assert_not_called()


async def test_recipient_guard_on_resolved_draft_recipients(server_factory):
    """send_draft carries no recipients in its arguments; the draft's own are checked."""
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True,
                                ews_recipient_allowlist="*@corp.example")
    item = _draft(gw.mock_account, to=("ok@corp.example",), bcc=("leak@other.example",))
    ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    assert not ok and err["code"] == "recipient_blocked" and "leak@other.example" in err["message"]
    item.send.assert_not_called()


# ------------------------------------------------------------------ two-phase confirm


async def test_token_reuse_refused(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = _draft(gw.mock_account)
    ok, preview = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    args = {"draft_id": "AAMkRAW", "confirm_token": preview["confirm_token"]}
    assert (await call(mcp, "send_draft", args))[0]
    ok, err = await call(mcp, "send_draft", args)
    assert not ok and err["code"] == "confirm_invalid" and "already used" in err["message"]
    assert item.send.call_count == 1


async def test_token_tampered_refused(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    _draft(gw.mock_account)
    ok, preview = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    tok = preview["confirm_token"]
    for bad in (tok[:-1] + ("0" if tok[-1] != "0" else "1"), "garbage", "x." + tok.split(".")[1]):
        ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW", "confirm_token": bad})
        assert not ok and err["code"] == "confirm_invalid" and "fresh preview" in err["hint"]


async def test_token_invalid_after_draft_edit(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = _draft(gw.mock_account, body="Version 1")
    ok, preview = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    item.text_body = "Version 2 with different terms"  # edited between preview and confirm
    ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW", "confirm_token": preview["confirm_token"]})
    assert not ok and err["code"] == "confirm_invalid" and "changed" in err["message"]
    item.send.assert_not_called()


async def test_token_invalid_after_recipient_change(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = _draft(gw.mock_account)
    ok, preview = await call(mcp, "send_draft", {"draft_id": "AAMkRAW"})
    item.to_recipients = [MagicMock(email_address="someone-else@example.com")]
    ok, err = await call(mcp, "send_draft", {"draft_id": "AAMkRAW", "confirm_token": preview["confirm_token"]})
    assert not ok and err["code"] == "confirm_invalid"


async def test_token_bound_to_arguments_and_tool(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    item = MagicMock()
    gw.mock_account.fetch.return_value = [item]
    ok, preview = await call(mcp, "delete_messages", {"ids": ["AAMkRAW"]})
    tok = preview["confirm_token"]
    ok, err = await call(mcp, "delete_messages", {"ids": ["AAMkRAW"], "permanent": True, "confirm_token": tok})
    assert not ok and err["code"] == "confirm_invalid"  # soft-delete approval can't hard-delete
    ok, err = await call(mcp, "cancel_event", {"event_id": "AAMkRAW", "confirm_token": tok})
    assert not ok and err["code"] == "confirm_invalid"
    item.delete.assert_not_called()


def test_token_expiry():
    now = [1000.0]
    c = Confirmer("secret", ttl_seconds=10, clock=lambda: now[0])
    tok, exp = c.issue("send_draft", {"a": 1})
    assert exp == 1010
    now[0] = 1011
    with pytest.raises(Failure) as e:
        c.verify(tok, "send_draft", {"a": 1})
    assert e.value.code == "confirm_invalid" and "expired" in e.value.message


def test_token_secret_matters():
    tok, _ = Confirmer("one", 60).issue("t", {"a": 1})
    with pytest.raises(Failure):
        Confirmer("two", 60).verify(tok, "t", {"a": 1})


@pytest.mark.parametrize("tool,args", [
    ("respond_to_event", {"event_id": "AAMkRAW", "response": "accept"}),
    ("cancel_event", {"event_id": "AAMkRAW"}),
    ("set_oof", {"state": "enabled", "internal_reply": "away"}),
    ("delete_messages", {"ids": ["AAMkRAW"]}),
])
async def test_every_send_and_destructive_tool_needs_confirmation(server_factory, tool, args):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    ok, res = await call(mcp, tool, args)
    assert ok and res["requires_confirmation"] and res["confirm_token"]
    gw.mock_account.fetch.assert_not_called()
    assert isinstance(gw.mock_account.oof_settings, MagicMock)  # set_oof did not assign anything


async def test_set_oof_runs_only_after_confirm(server_factory):
    mcp, _, gw = server_factory(ews_capability_tier="full", send_enabled=True)
    account = gw.mock_account
    ok, preview = await call(mcp, "set_oof", {"state": "disabled"})
    assert isinstance(account.oof_settings, MagicMock)  # not assigned yet
    res = await confirmed(mcp, "set_oof", {"state": "disabled"})
    assert res["state"] == "disabled"
    assert not isinstance(account.oof_settings, MagicMock)  # an OofSettings object was assigned


def test_settings_lists_parse():
    s = Settings(_env_file=None, ews_recipient_allowlist=" *@A.example, b@c.example ,",
                 ews_recipient_denylist="")
    assert s.allowlist == ["*@a.example", "b@c.example"] and s.denylist == []
