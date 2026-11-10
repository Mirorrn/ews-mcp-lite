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


def test_resolve_folder_reloads_stale_folder_tree():
    """exchangelib caches the folder tree for the life of the account: a folder created
    in another client is found after one cache reload instead of failing forever."""
    from ews_mcp_lite.aliases import AliasStore
    from ews_mcp_lite.errors import Failure
    from ews_mcp_lite.tools.common import resolve_folder

    new = SimpleNamespace(name="Projects", children=[])
    root = SimpleNamespace(name="root", children=[])
    reloads = []

    def clear_cache():
        reloads.append(1)
        root.children = [new]

    account = SimpleNamespace(msg_folder_root=root, root=SimpleNamespace(clear_cache=clear_cache))
    assert resolve_folder(account, "projects", AliasStore()) is new
    assert len(reloads) == 1
    with pytest.raises(Failure) as e:
        resolve_folder(account, "Nope/Deeper", AliasStore())
    assert e.value.code == "not_found"


def test_mapi_conversation_id_from_grommunio_ews_id():
    import base64
    raw = bytes(range(16))
    assert compat.mapi_conversation_id(base64.b64encode(raw + b"\x01").decode()) == raw
    assert compat.mapi_conversation_id("AAQkAGI2not-a-grommunio-id") is None  # Exchange ids: leave alone
    assert compat.mapi_conversation_id(base64.b64encode(b"x" * 40).decode()) is None


@pytest.mark.anyio
async def test_get_thread_filters_on_mapi_conversation_id_for_grommunio(server_factory):
    import base64
    raw = bytes(range(16))
    conv = base64.b64encode(raw + b"\x01").decode()
    for server_type, expected in (("grommunio", {"pr_conversation_id": raw}), ("exchange", None)):
        mcp, ctx, gw = server_factory(ews_server_type=server_type)
        acct = gw.mock_account
        for folder in (acct.inbox, acct.sent):
            folder.filter.return_value.only.return_value.__getitem__.return_value = []
        ok, res = await call(mcp, "get_thread", {"id": ctx.aliases.alias(conv, "t")})
        assert ok and res["count"] == 0, res
        kwargs = acct.inbox.filter.call_args.kwargs
        if expected:
            assert kwargs == expected
        else:
            assert list(kwargs) == ["conversation_id"] and kwargs["conversation_id"].id == conv


# ------------------------------------------------------------------ attachments


def _att(name, *, size=10, inline=False, cid=None, content=None, ctype="text/plain"):
    from exchangelib import FileAttachment
    from exchangelib.attachments import AttachmentId
    a = FileAttachment(name=name, content_type=ctype, size=size, is_inline=inline, content_id=cid)
    if content is None:  # as fetched from a server: content stays there until read
        a.attachment_id = AttachmentId(id=f"ATT-{name}")
    else:
        a._content = content
    return a


def _with_attachments(gw, atts, html="<p>hi</p>"):
    from exchangelib import HTMLBody
    item = SimpleNamespace(id="RAW-1", changekey="CK", subject="S", sender=None, text_body="hi", body=HTMLBody(html),
                           conversation_id=None, is_read=True, has_attachments=True, importance="Normal",
                           categories=None, datetime_received=None, datetime_sent=None, datetime_created=None,
                           to_recipients=[], cc_recipients=[], attachments=atts)
    gw.mock_account.fetch.return_value = [item]
    return item


@pytest.mark.anyio
async def test_get_attachment_refuses_big_files_before_downloading(server_factory):
    mcp, ctx, gw = server_factory()
    # attachment_id set and no account: any download attempt would raise
    _with_attachments(gw, [_att("video.mp4", size=30 * 1024 * 1024, ctype="video/mp4")])
    ok, res = await call(mcp, "get_attachment", {"message_id": ctx.aliases.alias("RAW-1", "m")})
    assert ok, res
    assert res["mode"] == "metadata" and "not downloaded" in res["note"] and res["size_bytes"] == 30 * 1024 * 1024


def test_read_capped_stops_when_the_server_sends_no_size():
    import io

    from ews_mcp_lite.tools.mail import _read_capped
    reads = []

    class Stream(io.BytesIO):  # stand-in for the GetAttachment stream
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    def remote(data: bytes):
        return SimpleNamespace(_content=None, attachment_id="ATT-1", fp=Stream(data), _fp="open")
    att = remote(b"x" * 1000)
    assert _read_capped(att, 100) is None and reads == [101], "reads at most cap+1 bytes"
    assert att._fp is None, "a cut-short stream is not reused"
    assert _read_capped(remote(b"x" * 1000), 5000) == b"x" * 1000
    assert _read_capped(SimpleNamespace(_content=b"local", attachment_id=None), 100) == b"local"


@pytest.mark.anyio
async def test_inline_images_are_listed_apart_and_skipped_by_default(server_factory):
    mcp, ctx, gw = server_factory()
    atts = [_att("logo.png", inline=True, cid="logo1", ctype="image/png"),
            _att("signature.png", cid="sig@x", ctype="image/png"),  # not flagged inline, but referenced by cid:
            _att("report.csv", content=b"a,b\n1,2\n", ctype="text/csv")]
    _with_attachments(gw, atts, '<img src="cid:logo1"><p>see report</p><img src="CID:sig@x">')
    mid = ctx.aliases.alias("RAW-1", "m")
    ok, res = await call(mcp, "get_message", {"id": mid})
    msg = res["message"]
    assert [a["name"] for a in msg["attachments"]] == ["report.csv"] and msg["attachments"][0]["index"] == 2
    assert [(a["name"], a["index"]) for a in msg["inline_images"]] == [("logo.png", 0), ("signature.png", 1)]
    ok, res = await call(mcp, "get_attachment", {"message_id": mid})
    assert ok and res["name"] == "report.csv" and res["text"] == "a,b\n1,2\n" and "inline" not in res


@pytest.mark.anyio
async def test_get_attachment_with_only_inline_images_points_to_them(server_factory):
    mcp, ctx, gw = server_factory()
    _with_attachments(gw, [_att("logo.png", inline=True, cid="l", content=b"PNG", ctype="image/png")])
    mid = ctx.aliases.alias("RAW-1", "m")
    ok, err = await call(mcp, "get_attachment", {"message_id": mid})
    assert not ok and err["code"] == "not_found" and "inline image" in err["message"], err
    ok, res = await call(mcp, "get_attachment", {"message_id": mid, "attachment": "0"})
    assert ok and res["inline"] is True and res["mode"] == "base64"


@pytest.mark.anyio
async def test_get_thread_keeps_the_starting_message_from_a_subfolder(server_factory):
    """The conversation is gathered from Inbox and Sent Items; a message opened from
    another folder (found via folder='all') must still be part of its own thread."""
    from datetime import timedelta

    mcp, ctx, gw = server_factory()
    now = datetime.now(UTC)

    def msg(raw, days_ago, body):
        when = now - timedelta(days=days_ago)
        return SimpleNamespace(id=raw, changekey="CK", subject="Atlas plan", sender=None, author=None,
                               text_body=body, body=None, conversation_id=SimpleNamespace(id="CONV-1"),
                               is_read=True, has_attachments=False, importance="Normal", categories=None,
                               datetime_received=when, datetime_sent=when, datetime_created=when,
                               to_recipients=[], cc_recipients=[], attachments=[])
    filed, reply = msg("RAW-FILED", 2, "first, filed in Projects"), msg("RAW-REPLY", 1, "reply in the Inbox")
    acct = gw.mock_account
    acct.fetch.return_value = [filed]
    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = [reply]
    acct.sent.filter.return_value.only.return_value.__getitem__.return_value = []
    ok, res = await call(mcp, "get_thread", {"id": ctx.aliases.alias("RAW-FILED", "m")})
    assert ok, res
    assert [i["body"] for i in res["items"]] == ["first, filed in Projects", "reply in the Inbox"]
    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = [filed, reply]
    ok, res = await call(mcp, "get_thread", {"id": ctx.aliases.alias("RAW-FILED", "m")})
    assert res["count"] == 2, "no duplicate when the starting message is in the Inbox"
