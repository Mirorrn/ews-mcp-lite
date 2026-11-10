"""Server-side paging, the cheap fuzzy-sender scan and waiting_on cap warnings."""

from types import SimpleNamespace

import pytest
from exchangelib.queryset import QuerySet

from ews_mcp_lite.tools.common import take
from ews_mcp_lite.tools.mail import CARD_FIELDS, SENDER_SCAN_CAP, WAITING_INBOUND_CAP
from tests.conftest import call


class RecordingQS(QuerySet):
    """A QuerySet that records slicing and refuses plain iteration."""

    def __init__(self, n: int):  # skip QuerySet.__init__: no folders, no network
        self.n, self.calls = n, []

    def __getitem__(self, k):
        self.calls.append(k)
        return list(range(self.n))[k]

    def __iter__(self):
        raise AssertionError("take() must not iterate a QuerySet from the start")


@pytest.mark.parametrize("n,rows,more", [(100, list(range(10, 15)), True),
                                         (15, list(range(10, 15)), False),
                                         (12, [10, 11], False)])
def test_take_slices_queryset_server_side(n, rows, more):
    qs = RecordingQS(n)
    assert take(qs, 10, 5) == (rows, more)
    assert qs.calls == [slice(10, 16)]


def test_take_on_generator_still_works():
    assert take((i for i in range(20)), 3, 4) == ([3, 4, 5, 6], True)
    assert take(iter(range(5)), 3, 4) == ([3, 4], False)


def _light(i: int, name: str):
    return SimpleNamespace(id=f"RAW-{i}", changekey=f"CK-{i}",
                           sender=SimpleNamespace(name=name, email_address=f"{name.lower()}@x.com"))


def _full(raw: str):
    return SimpleNamespace(id=raw, changekey="CK", subject=f"S {raw}", sender=None, text_body="hi",
                           body=None, conversation_id=None, is_read=True, has_attachments=False,
                           importance="Normal", categories=None, datetime_received=None,
                           datetime_sent=None, datetime_created=None)


REJECTED = type("ErrorInvalidRestriction", (Exception,), {})("sender property not supported")


def _fuzzy_setup(gw, scanned):
    """A server that rejects the MAPI sender restriction: search_messages falls back to a
    light client-side scan of folder.all()."""
    gw.mock_account.inbox.filter.side_effect = REJECTED
    qs = gw.mock_account.inbox.all.return_value.order_by.return_value
    qs.only.return_value = scanned
    fetched = []

    def fetch(ids, folder, only_fields):
        fetched.append((list(ids), folder, only_fields))
        return [_full(i) for i, _ in ids] + [None, ValueError("gone")]
    gw.mock_account.fetch.side_effect = fetch
    return qs, fetched


@pytest.mark.anyio
async def test_fuzzy_sender_is_filtered_server_side(server_factory):
    mcp, _, gw = server_factory()
    inbox = gw.mock_account.inbox
    inbox.filter.return_value.order_by.return_value.only.return_value = [_full("RAW-1")]
    ok, res = await call(mcp, "search_messages", {"sender": "Lindqvist", "folder": "inbox"})
    assert ok, res
    sent = repr(inbox.filter.call_args.args[0])
    for prop in ("pr_sender_name", "pr_sender_smtp", "pr_sender_addr", "pr_from_name", "pr_from_smtp"):
        assert f"{prop} icontains 'Lindqvist'" in sent, sent
    assert not inbox.all.called, "no client-side scan when the server filters"
    assert [c["subject"] for c in res["items"]] == ["S RAW-1"] and "note" not in res


@pytest.mark.anyio
async def test_fuzzy_sender_fallback_scans_light_and_fetches_page_only(server_factory, caplog):
    mcp, _, gw = server_factory()
    scanned = [_light(i, "Alice" if i % 2 else "Bob") for i in range(20)]
    qs, fetched = _fuzzy_setup(gw, scanned)
    ok, res = await call(mcp, "search_messages", {"sender": "alice", "offset": 2, "limit": 3})
    assert ok, res
    assert any("matching sender names client-side" in r.getMessage() for r in caplog.records)
    qs.only.assert_called_once_with("id", "changekey", "sender")
    assert len(fetched) == 1
    ids, folder, only_fields = fetched[0]
    assert ids == [("RAW-5", "CK-5"), ("RAW-7", "CK-7"), ("RAW-9", "CK-9")]
    assert folder is gw.mock_account.inbox and only_fields == CARD_FIELDS
    assert [c["subject"] for c in res["items"]] == ["S RAW-5", "S RAW-7", "S RAW-9"]
    assert res["next_offset"] == 5 and "note" not in res, "a full page is not an incomplete result"


@pytest.mark.anyio
async def test_fuzzy_sender_fallback_notes_when_scan_hits_cap(server_factory):
    mcp, _, gw = server_factory()
    _fuzzy_setup(gw, [_light(i, "Bob") for i in range(SENDER_SCAN_CAP)])
    ok, res = await call(mcp, "search_messages", {"sender": "carol", "limit": 2})
    assert ok, res
    assert f"newest {SENDER_SCAN_CAP} messages" in res["note"] and res["items"] == []


@pytest.mark.anyio
async def test_fuzzy_sender_no_fetch_when_nothing_matches(server_factory):
    mcp, _, gw = server_factory()
    _, fetched = _fuzzy_setup(gw, [_light(i, "Bob") for i in range(3)])
    ok, res = await call(mcp, "search_messages", {"sender": "zed"})
    assert ok and res["items"] == [] and fetched == []


@pytest.mark.anyio
async def test_waiting_on_warns_when_inbound_hits_cap(server_factory):
    mcp, _, gw = server_factory()
    acct = gw.mock_account
    acct.sent.filter.return_value.order_by.return_value.only.return_value.__getitem__.return_value = []
    inbound = [SimpleNamespace(subject=f"S{i}", conversation_id=None, datetime_sent=None,
                               datetime_received=None) for i in range(WAITING_INBOUND_CAP)]
    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = inbound
    ok, res = await call(mcp, "waiting_on", {})
    assert ok, res
    assert "in 1 mail folder(s)" in res["note"]
    assert f"reply scan hit its cap of {WAITING_INBOUND_CAP} in Inbox" in res["note"]
    assert "sent scan hit its cap" not in res["note"]

    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = inbound[:10]
    ok, res = await call(mcp, "waiting_on", {})
    assert ok and "WARNING" not in res["note"]


def _sent(i: int, to: str, days_ago: float = 5):
    from datetime import UTC, datetime, timedelta
    who = SimpleNamespace(name=to.split("@")[0].title(), email_address=to)
    return SimpleNamespace(id=f"S-{i}", changekey="CK", subject=f"Quote {i}", to_recipients=[who], cc_recipients=[],
                           datetime_sent=datetime.now(UTC) - timedelta(days=days_ago), conversation_id=None)


@pytest.mark.anyio
async def test_waiting_on_to_filters_recipients_and_excludes_own_mail_server_side(server_factory):
    mcp, _, gw = server_factory()
    acct = gw.mock_account
    sent_qs = acct.sent.filter.return_value.order_by.return_value
    sent_qs.only.return_value.__getitem__.return_value = [_sent(1, "viktor.sandoval@helios.example"),
                                                          _sent(2, "bob@x.com")]
    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = []
    acct.fetch.side_effect = lambda ids, folder, only_fields: [_full(i) for i, _ in ids]
    ok, res = await call(mcp, "waiting_on", {"to": "Sandoval"})
    assert ok, res
    assert [c["subject"] for c in res["items"]] == ["S S-1"], res
    assert "1 of them to 'Sandoval'" in res["note"]
    assert "to_recipients" in sent_qs.only.call_args.args, "recipients are read when filtering by `to`"
    inbound_q = repr(acct.inbox.filter.call_args.args[0])
    assert "pr_sender_smtp != 'user@example.com'" in inbound_q, inbound_q

    ok, res = await call(mcp, "waiting_on", {})
    assert ok and res["count"] == 2
    assert "to_recipients" not in sent_qs.only.call_args.args, "no recipient fields without `to`"
