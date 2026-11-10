"""Aliases, DTOs, dates, the exchangelib compat shim and server workarounds."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from exchangelib import EWSDateTime, EWSTimeZone
from exchangelib.ewsdatetime import EWSDateTime as _EDT

from ews_mcp_lite import compat
from ews_mcp_lite.aliases import AliasStore
from ews_mcp_lite.dates import parse_when, to_ews
from ews_mcp_lite.dto import clean_body, msg_card
from ews_mcp_lite.tools.common import ATTENDEE_FIELDS, fetch_event
from tests.conftest import EXCHANGE_2016, call

# ---------------------------------------------------------------- aliases


def test_alias_roundtrip_and_reuse():
    a = AliasStore()
    m1 = a.alias("RAW-1", "m", "CK1")
    assert m1 == "m1" and a.alias("RAW-1", "m") == "m1" and a.alias("RAW-2", "m") == "m2"
    assert a.resolve("m1") == ("RAW-1", "CK1")
    assert a.alias("RAW-1", "e") == "e1"  # prefixes count separately


def test_alias_stale_and_wrong_kind():
    a = AliasStore()
    a.alias("RAW", "e")
    with pytest.raises(ValueError, match="stale"):
        a.resolve("m7")
    with pytest.raises(ValueError, match="expected"):
        a.resolve("e1", expect=("m",))
    assert a.resolve("AAMkRAWID") == ("AAMkRAWID", None)  # raw ids pass through


def test_alias_rebind_after_move():
    a = AliasStore()
    a.alias("OLD", "m")
    a.rebind("m1", "NEW", "CK")
    assert a.resolve("m1") == ("NEW", "CK") and a.alias("NEW", "m") == "m1"


# ---------------------------------------------------------------- dto


def _msg(**kw):
    base = dict(id="RAW", changekey="CK", subject="S", sender=None, text_body="Hello",
                body=None, conversation_id=None, is_read=True, has_attachments=False,
                importance="Normal", categories=None, datetime_received=None,
                datetime_sent=None, datetime_created=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_card_date_falls_back_to_datetime_sent():
    sent = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    card = msg_card(_msg(datetime_sent=sent), AliasStore(), "Europe/Berlin")
    assert card["date"] == "2026-11-09T09:00+01:00"


def test_card_prefers_datetime_received():
    rec = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)
    sent = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
    card = msg_card(_msg(datetime_received=rec, datetime_sent=sent), AliasStore(), "UTC")
    assert card["date"].startswith("2026-11-09T07:00")


def test_clean_body_strips_quotes_and_signature():
    text = "Thanks!\n\n-- \nBob\n\nOn Mon, Bob wrote:\n> old"
    assert clean_body(text)["text"] == "Thanks!"
    out = clean_body("New\nOn Mon, 3 Nov 2026 Bob wrote:\n> old")
    assert out["text"] == "New" and out["quoted_history_stripped"]


# ---------------------------------------------------------------- dates / compat


def test_parse_when_returns_ews_types():
    for v in ("today", "+2d", "2026-11-09", "2026-11-09T10:30", "now"):
        d = parse_when(v, "f", "Europe/Berlin")
        assert isinstance(d, EWSDateTime) and isinstance(d.tzinfo, EWSTimeZone)
    with pytest.raises(ValueError):
        parse_when("next tuesday", "f", "UTC")


def test_to_ews_accepts_ews_and_zoneinfo_values():
    d = to_ews(parse_when("today", "f", "Europe/Berlin"), "Europe/Berlin")
    assert isinstance(d.tzinfo, EWSTimeZone) and d.tzinfo.ms_id
    d2 = to_ews(datetime(2026, 1, 1, 12, tzinfo=UTC), "Europe/Berlin")
    assert d2.hour == 13


def test_compat_parses_fractional_seconds():
    compat.apply()
    d = _EDT.from_string("2026-11-09T08:28:05.198803600Z")
    assert (d.hour, d.second, d.microsecond) == (8, 5, 198803)
    assert _EDT.from_string("2026-11-09T08:28:05Z").second == 5


# ---------------------------------------------------------------- server workarounds


def test_fetch_event_retries_without_attendee_fields():
    """grommunio fails attendee properties of plain appointments (E-3127)."""
    err_cls = type("ErrorItemPropertyRequestFailed", (Exception,), {})
    account = MagicMock()
    account.version = EXCHANGE_2016
    item = SimpleNamespace(id="RAW", subject="x")
    calls = []

    def fetch(ids, **kw):
        calls.append(kw)
        return [err_cls("E-3127: failed to get item property")] if len(calls) == 1 else [item]
    account.fetch.side_effect = fetch
    got, attendees_ok = fetch_event(account, "RAW")
    assert got is item and attendees_ok is False
    assert not set(ATTENDEE_FIELDS) & set(calls[1]["only_fields"])
    assert calls[1]["folder"] is account.calendar



@pytest.mark.anyio
async def test_get_event_marks_attendees_unavailable(server_factory):
    mcp, ctx, gw = server_factory()
    err_cls = type("ErrorItemPropertyRequestFailed", (Exception,), {})
    item = SimpleNamespace(id="RAW", changekey="CK", subject="Plain", start=None, end=None,
                           location=None, organizer=None, my_response_type=None,
                           is_recurring=False, is_all_day=False, text_body="b", body=None)
    gw.mock_account.fetch.side_effect = [[err_cls("E-3127")], [item]]
    ok, res = await call(mcp, "get_event", {"id": "AAMkRAWEVENT"})
    assert ok and res["event"]["attendees"] is None and "attendees_note" in res["event"]


@pytest.mark.anyio
async def test_check_availability_passes_ews_timezone_datetimes(server_factory):
    """zoneinfo datetimes would raise AttributeError 'ms_id' inside exchangelib.
    The mock must capture the real arguments, not just return canned data."""
    mcp, _, gw = server_factory()
    captured = {}

    def fake_free_busy(**kw):
        captured.update(kw)
        return iter([SimpleNamespace(calendar_events=[])])
    gw.mock_account.protocol.get_free_busy_info.side_effect = fake_free_busy
    ok, res = await call(mcp, "check_availability", {
        "attendees": ["a@example.com"], "start": "2030-01-07", "end": "2030-01-08"})
    assert ok, res
    for key in ("start", "end"):
        value = captured[key]
        assert isinstance(value, EWSDateTime)
        assert isinstance(value.tzinfo, EWSTimeZone) and value.tzinfo.ms_id  # what exchangelib reads
    assert captured["accounts"] == [("a@example.com", "Required", False)]
    assert res["suggested_slots"]


@pytest.mark.anyio
async def test_search_falls_back_to_subject_when_body_restriction_rejected(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="restriction")
    err_cls = type("ErrorInvalidRestriction", (Exception,), {})
    folder = gw.mock_account.inbox
    good = MagicMock()
    good.order_by.return_value.only.return_value = iter([])
    bad = MagicMock()
    bad.order_by.return_value.only.return_value.__iter__.side_effect = err_cls("E-3224")
    folder.filter.side_effect = [bad, bad, good]  # item:Body, then PR_BODY rejected
    ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert ok and "subjects only" in res["note"]
