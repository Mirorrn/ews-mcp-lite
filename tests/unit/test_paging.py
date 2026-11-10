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


def _fuzzy_setup(gw, scanned):
    qs = gw.mock_account.inbox.all.return_value.order_by.return_value
    qs.only.return_value = scanned
    fetched = []

    def fetch(ids, folder, only_fields):
        fetched.append((list(ids), folder, only_fields))
        return [_full(i) for i, _ in ids] + [None, ValueError("gone")]
    gw.mock_account.fetch.side_effect = fetch
    return qs, fetched


@pytest.mark.anyio
async def test_fuzzy_sender_scans_light_and_fetches_page_only(server_factory):
    mcp, _, gw = server_factory()
    scanned = [_light(i, "Alice" if i % 2 else "Bob") for i in range(20)]
    qs, fetched = _fuzzy_setup(gw, scanned)
    ok, res = await call(mcp, "search_messages", {"sender": "alice", "offset": 2, "limit": 3})
    assert ok, res
    qs.only.assert_called_once_with("id", "changekey", "sender")
    assert len(fetched) == 1
    ids, folder, only_fields = fetched[0]
    assert ids == [("RAW-5", "CK-5"), ("RAW-7", "CK-7"), ("RAW-9", "CK-9")]
    assert folder is gw.mock_account.inbox and only_fields == CARD_FIELDS
    assert [c["subject"] for c in res["items"]] == ["S RAW-5", "S RAW-7", "S RAW-9"]
    assert res["next_offset"] == 5 and "note" not in res


@pytest.mark.anyio
async def test_fuzzy_sender_notes_when_scan_hits_cap(server_factory):
    mcp, _, gw = server_factory()
    _fuzzy_setup(gw, [_light(i, "Carol") for i in range(SENDER_SCAN_CAP)])
    ok, res = await call(mcp, "search_messages", {"sender": "carol", "limit": 2})
    assert ok, res
    assert f"newest {SENDER_SCAN_CAP} messages" in res["note"] and len(res["items"]) == 2


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
    assert "Inbox only" in res["note"] and "inbox scan hit its cap" in res["note"]
    assert "sent scan hit its cap" not in res["note"]

    acct.inbox.filter.return_value.only.return_value.__getitem__.return_value = inbound[:10]
    ok, res = await call(mcp, "waiting_on", {})
    assert ok and "WARNING" not in res["note"]
