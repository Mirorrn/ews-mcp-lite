"""search_messages: EWS_SERVER_TYPE / EWS_SEARCH_MODE, AQS vs restrictions, fallbacks, logs."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from exchangelib.version import Build, Version
from pydantic import ValidationError

from ews_mcp_lite import aqs
from ews_mcp_lite.tools.mail import CARD_FIELDS, SENDER_SCAN_CAP
from tests.conftest import EXCHANGE_2016, FakeGateway, call, make_settings

NOW = datetime.now(UTC)
REJECTED = type("ErrorInvalidRestriction", (Exception,), {})("E-3224")


def _item(i: int, *, days_ago: float = 0, name: str = "Bob"):
    when = NOW - timedelta(days=days_ago)
    return SimpleNamespace(id=f"RAW-{i}", changekey=f"CK-{i}", subject=f"S{i}", text_body="hi", body=None,
                           sender=SimpleNamespace(name=name, email_address=f"{name.lower()}@x.com"),
                           conversation_id=None, is_read=True, has_attachments=False, importance="Normal",
                           categories=None, datetime_received=when, datetime_sent=when, datetime_created=when)


class FakeQS:
    def __init__(self, rows, error=None):
        self.rows, self.error, self.only_fields, self.ordered = rows, error, None, None

    def order_by(self, *fields):
        self.ordered = fields
        return self

    def only(self, *fields):
        self.only_fields = fields
        return self

    def _check(self):
        if self.error:
            raise self.error

    def __getitem__(self, s):
        self._check()
        return self.rows[s]

    def __iter__(self):
        self._check()
        return iter(self.rows)


class FakeFolder:
    """filter()/all() record the Q (or kwargs) and answer via `respond(q) -> FakeQS`."""

    def __init__(self, respond):
        self.respond, self.queries, self.made = respond, [], []

    def filter(self, *args, **kwargs):
        q = args[0] if args else kwargs
        self.queries.append(q)
        qs = self.respond(q)
        self.made.append(qs)
        return qs

    def all(self):
        return self.filter(None)


def qstr(q):
    return getattr(q, "query_string", None)


def _setup(gw, respond):
    folder = FakeFolder(respond)
    gw.mock_account.inbox = folder
    by_id = {}

    def fetch(ids, folder, only_fields):
        assert only_fields == CARD_FIELDS
        return [by_id[i] for i, _ in ids]
    gw.mock_account.fetch.side_effect = fetch
    return folder, by_id


# ------------------------------------------------------------------ query building


def test_phrase_quotes_user_input():
    assert aqs.phrase("q4 budg") == '"q4 budg"*'
    assert aqs.phrase('say "hi"  OR NOT from:x (a)') == '"say hi OR NOT from:x (a)"*'
    assert aqs.phrase("“smart” quotes", prefix=False) == '"smart quotes"'
    assert aqs.phrase(' " ') is None and aqs.phrase(None) is None


def test_build_query_translates_filters():
    got = aqs.build_query('budget "Q4"', subject="plan OR x", exact_sender="bob@x.com",
                          unread_only=True, has_attachments=False)
    assert got == ('"budget Q4"* AND subject:"plan OR x"* AND from:"bob@x.com" AND isread:false '
                   'AND hasattachment:false')
    assert aqs.build_query("x", has_attachments=True) == '"x"* AND hasattachment:true'
    assert aqs.build_query("x") == '"x"*'


# ------------------------------------------------------------------ explicit modes


@pytest.mark.anyio
async def test_aqs_mode_sends_one_query_string_with_filters(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    rows = [_item(i) for i in range(3)]
    folder, _ = _setup(gw, lambda q: FakeQS(rows))
    ok, res = await call(mcp, "search_messages", {"query": "invoice", "subject": "acme",
                                                  "sender": "billing@acme.example",
                                                  "unread_only": True, "has_attachments": True})
    assert ok, res
    assert [qstr(q) for q in folder.queries] == [
        '"invoice"* AND subject:"acme"* AND from:"billing@acme.example" AND isread:false '
        'AND hasattachment:true']
    qs = folder.made[0]
    assert qs.ordered == ("-datetime_received",) and qs.only_fields == tuple(CARD_FIELDS)
    assert len(res["items"]) == 3 and "search index" in res["note"]


@pytest.mark.anyio
async def test_restriction_mode_never_uses_query_strings(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="restriction")
    folder, _ = _setup(gw, lambda q: FakeQS([_item(1)]))
    ok, res = await call(mcp, "search_messages", {"query": "invoice", "unread_only": True})
    assert ok, res
    assert len(folder.queries) == 1 and qstr(folder.queries[0]) is None
    assert "body icontains" in repr(folder.queries[0]) and "note" not in res


@pytest.mark.anyio
async def test_no_query_means_no_aqs_even_in_aqs_mode(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    folder, _ = _setup(gw, lambda q: FakeQS([_item(1)]))
    ok, res = await call(mcp, "search_messages", {"query": "  ", "unread_only": True})
    assert ok and qstr(folder.queries[0]) is None and "note" not in res


@pytest.mark.anyio
async def test_aqs_dates_are_applied_to_index_hits_and_only_the_page_is_fetched(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    hits = [_item(i, days_ago=i) for i in range(12)]  # newest first, one per day
    folder, by_id = _setup(gw, lambda q: FakeQS(hits))
    by_id.update({m.id: m for m in hits})
    since = (NOW - timedelta(days=8, hours=12)).isoformat()
    until = (NOW - timedelta(days=1, hours=12)).isoformat()
    ok, res = await call(mcp, "search_messages", {"query": "budget", "since": since, "until": until,
                                                  "offset": 1, "limit": 3})
    assert ok, res
    assert qstr(folder.queries[0]) == '"budget"*'  # no AQS date syntax
    assert folder.made[0].only_fields == ("id", "changekey", "datetime_received", "datetime_sent")
    # in range: days 2..8 -> page offset 1, limit 3 = days 3, 4, 5
    assert [c["subject"] for c in res["items"]] == ["S3", "S4", "S5"] and res["next_offset"] == 4
    assert "index hits only" not in res["note"]


@pytest.mark.anyio
async def test_aqs_dates_warn_when_index_cap_reached(server_factory):
    """All index hits are newer than `until`: the scan runs out before a page is full."""
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    hits = [_item(i, days_ago=0.001 * i) for i in range(aqs.AQS_HIT_CAP)]
    folder, by_id = _setup(gw, lambda q: FakeQS(hits))
    by_id.update({m.id: m for m in hits})
    ok, res = await call(mcp, "search_messages", {"query": "budget", "until": "-2d", "limit": 2})
    assert ok and res["items"] == [], res
    assert f"date range applied to the newest {aqs.AQS_HIT_CAP} index hits only" in res["note"]


@pytest.mark.anyio
async def test_aqs_dates_full_page_is_not_reported_incomplete(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    hits = [_item(i, days_ago=0.001 * i) for i in range(aqs.AQS_HIT_CAP)]
    folder, by_id = _setup(gw, lambda q: FakeQS(hits))
    by_id.update({m.id: m for m in hits})
    ok, res = await call(mcp, "search_messages", {"query": "budget", "since": "-30d", "limit": 2})
    assert ok and len(res["items"]) == 2 and res["next_offset"] == 2
    assert "incomplete" not in res["note"]


@pytest.mark.anyio
async def test_aqs_with_fuzzy_sender_scans_light(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    hits = [_item(i, name="Alice" if i % 2 else "Bob") for i in range(6)]
    folder, by_id = _setup(gw, lambda q: FakeQS(hits))
    by_id.update({m.id: m for m in hits})
    ok, res = await call(mcp, "search_messages", {"query": "budget", "sender": "alice"})
    assert ok, res
    assert folder.made[0].only_fields == ("id", "changekey", "sender")
    assert [c["subject"] for c in res["items"]] == ["S1", "S3", "S5"]
    assert "index hits only" not in res["note"]


@pytest.mark.anyio
async def test_aqs_notes_index_cap_without_client_filters(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    hits = [_item(i) for i in range(aqs.AQS_HIT_CAP)]
    _setup(gw, lambda q: FakeQS(hits))
    ok, res = await call(mcp, "search_messages", {"query": "x", "offset": aqs.AQS_HIT_CAP - 10, "limit": 20})
    assert ok and len(res["items"]) == 10 and "later matches are cut off" in res["note"]


# ------------------------------------------------------------------ fallbacks


@pytest.mark.anyio
async def test_aqs_server_error_falls_back_to_restriction(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    err = type("ErrorInvalidArgument", (Exception,), {})("bad query")
    folder, _ = _setup(gw, lambda q: FakeQS([], error=err) if qstr(q) else FakeQS([_item(1)]))
    ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert ok, res
    assert qstr(folder.queries[0]) and qstr(folder.queries[1]) is None
    assert len(res["items"]) == 1 and "index search failed (ErrorInvalidArgument)" in res["note"]
    assert "search index" not in res["note"]


@pytest.mark.anyio
async def test_aqs_fallback_keeps_body_restriction_fallback(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    aqs_err = type("ErrorInvalidArgument", (Exception,), {})("x")
    body_err = type("ErrorInvalidRestriction", (Exception,), {})("E-3224")

    def respond(q):
        if qstr(q):
            return FakeQS([], error=aqs_err)
        return FakeQS([], error=body_err) if "body icontains" in repr(q) else FakeQS([_item(1)])
    _setup(gw, respond)
    ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert ok and "index search failed" in res["note"] and "subjects only" in res["note"]


@pytest.mark.anyio
@pytest.mark.parametrize("name,code", [("UnauthorizedError", "auth_failed"),
                                       ("TransportError", "upstream_unavailable")])
async def test_aqs_auth_and_connection_errors_are_not_masked(server_factory, name, code):
    mcp, _, gw = server_factory(ews_search_mode="aqs")
    err = type(name, (Exception,), {})("nope")
    folder, _ = _setup(gw, lambda q: FakeQS([], error=err) if qstr(q) else FakeQS([_item(1)]))
    ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert not ok and res["code"] == code and len(folder.queries) == 1


# ------------------------------------------------------------------ EWS_SERVER_TYPE


def test_server_type_decides_search_settings():
    assert make_settings().search_mode == "aqs"
    assert make_settings().body_fields == ("body", "pr_body")
    g = make_settings(ews_server_type="grommunio")
    assert g.search_mode == "restriction" and g.body_fields == ("pr_body",)
    assert make_settings(ews_search_mode="restriction").search_mode == "restriction"
    assert make_settings(ews_search_mode="").search_mode == "aqs"  # empty env var = unset


@pytest.mark.parametrize("kw", [{"ews_server_type": "grommunio", "ews_search_mode": "aqs"},
                                {"ews_search_mode": "auto"}, {"ews_server_type": "gromox"}])
def test_invalid_search_config_is_rejected_at_startup(kw):
    with pytest.raises(ValidationError):
        make_settings(**kw)


@pytest.mark.anyio
async def test_exchange_default_uses_aqs_without_probing(server_factory):
    mcp, _, gw = server_factory()
    folder, _ = _setup(gw, lambda q: FakeQS([_item(1)]))
    ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert ok and "search index" in res["note"]
    assert [qstr(q) for q in folder.queries] == ['"budget"*']  # one query, no probe


@pytest.mark.anyio
async def test_grommunio_searches_pr_body_directly(server_factory):
    mcp, _, gw = server_factory(ews_server_type="grommunio")
    folder, _ = _setup(gw, _rejecting("(body"))
    ok, res = await call(mcp, "search_messages", {"query": "legal"})
    assert ok and len(res["items"]) == 1 and "note" not in res
    assert len(folder.queries) == 1  # no failed item:Body round trip first
    sent = repr(folder.queries[0])
    assert "pr_body icontains" in sent and "(body icontains" not in sent and not qstr(folder.queries[0])


@pytest.mark.anyio
async def test_grommunio_rejected_pr_body_falls_back_to_subject(server_factory, caplog):
    mcp, _, gw = server_factory(ews_server_type="grommunio")
    folder, _ = _setup(gw, _rejecting("body"))
    with caplog.at_level(logging.INFO, logger="ews_mcp_lite.tools.mail"):
        ok, res = await call(mcp, "search_messages", {"query": "legal"})
    assert ok and "subjects only" in res["note"] and len(folder.queries) == 2
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("pr_body" in w and "subjects only" in w and "EWS_SERVER_TYPE=grommunio" in w for w in warn)


# ------------------------------------------------------------------ logs


def _search_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("search_messages folder=")]


@pytest.mark.anyio
async def test_search_logs_summary_without_search_terms(server_factory, caplog):
    mcp, _, gw = server_factory()
    _setup(gw, lambda q: FakeQS([_item(1)]))
    with caplog.at_level(logging.INFO, logger="ews_mcp_lite.tools.mail"):
        ok, _ = await call(mcp, "search_messages", {"query": "secret-term", "sender": "bob@x.com",
                                                    "unread_only": True})
    assert ok
    [line] = _search_logs(caplog)
    assert "mode=aqs" in line and "hits=1" in line and "filters=text,unread,sender_exact" in line
    assert "took_ms=" in line and "secret-term" not in line and "bob@x.com" not in line


@pytest.mark.anyio
async def test_search_logs_aqs_failure_and_fallback(server_factory, caplog):
    mcp, _, gw = server_factory()
    err = type("ErrorInvalidArgument", (Exception,), {})("bad query")
    _setup(gw, lambda q: FakeQS([], error=err) if qstr(q) else FakeQS([_item(1)]))
    with caplog.at_level(logging.INFO, logger="ews_mcp_lite.tools.mail"):
        ok, res = await call(mcp, "search_messages", {"query": "budget"})
    assert ok and "index search failed" in res["note"]
    assert any(r.levelno == logging.WARNING and "AQS search failed" in r.getMessage() for r in caplog.records)
    [line] = _search_logs(caplog)
    assert "mode=restriction" in line and "body_field=body" in line and "fallback=aqs_failed" in line


@pytest.mark.anyio
async def test_search_warns_when_result_is_capped(server_factory, caplog):
    mcp, _, gw = server_factory(ews_server_type="grommunio")
    _setup(gw, lambda q: FakeQS([_item(i) for i in range(SENDER_SCAN_CAP)],
                                error=REJECTED if "pr_sender" in repr(q) else None))
    with caplog.at_level(logging.INFO, logger="ews_mcp_lite.tools.mail"):
        ok, res = await call(mcp, "search_messages", {"sender": "nobody-matches"})
    assert ok and "incomplete" in res["note"]
    assert any(r.levelno == logging.WARNING and "result capped" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("server_type, warned", [("exchange", True), ("grommunio", False)])
def test_connect_warns_on_gromox_build_with_exchange_type(server_type, warned, caplog):
    gw = FakeGateway(make_settings(ews_server_type=server_type))
    gw.mock_account.version = Version(build=Build(15, 0, 847, 4040), api_version="Exchange2019")
    with caplog.at_level(logging.INFO, logger="ews_mcp_lite.ews"):
        assert gw.connect()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("server build 15.0.847.4040" in m and f"EWS_SERVER_TYPE={server_type}" in m for m in msgs)
    assert any(r.levelno == logging.WARNING and "gromox" in r.getMessage() for r in caplog.records) is warned


# ------------------------------------------------------------------ body restriction stages


def _rejecting(*fields):
    """respond() raising ErrorInvalidRestriction when a Contains on one of `fields` is sent."""
    err = type("ErrorInvalidRestriction", (Exception,), {})("E-3224")
    return lambda q: FakeQS([], error=err) if any(f"{f} icontains" in repr(q) for f in fields) \
        else FakeQS([_item(1)])


@pytest.mark.anyio
async def test_item_body_restriction_never_touches_pr_body(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="restriction")
    folder, _ = _setup(gw, _rejecting())
    ok, res = await call(mcp, "search_messages", {"query": "legal"})
    assert ok and len(res["items"]) == 1 and "note" not in res
    assert len(folder.queries) == 1 and "pr_body" not in repr(folder.queries[0])


@pytest.mark.anyio
async def test_rejected_item_body_retries_with_pr_body(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="restriction")
    folder, _ = _setup(gw, _rejecting("(body"))  # item:Body only, not pr_body
    ok, res = await call(mcp, "search_messages", {"query": "legal", "unread_only": True})
    assert ok and len(res["items"]) == 1 and "note" not in res
    assert len(folder.queries) == 2
    retry = repr(folder.queries[1])
    assert "pr_body icontains" in retry and "subject icontains" in retry and "is_read" in retry


@pytest.mark.anyio
async def test_both_body_restrictions_rejected_falls_back_to_subject(server_factory):
    mcp, _, gw = server_factory(ews_search_mode="restriction")
    folder, _ = _setup(gw, _rejecting("body"))
    ok, res = await call(mcp, "search_messages", {"query": "legal"})
    assert ok and len(res["items"]) == 1 and "subjects only" in res["note"]
    assert len(folder.queries) == 3 and "body" not in repr(folder.queries[2])


def test_fetch_one_without_only_skips_pr_body():
    from ews_mcp_lite.tools.common import fetch_one
    account = MagicMock(version=EXCHANGE_2016)
    account.fetch.return_value = [SimpleNamespace(id="X")]
    assert fetch_one(account, "X").id == "X"
    names = {f.name for f in account.fetch.call_args.kwargs["only_fields"]}
    assert "pr_body" not in names and {"subject", "body", "text_body", "start", "due_date"} <= names
    assert "folder" not in account.fetch.call_args.kwargs  # exchangelib's all-item-types default


# ------------------------------------------------------------------ folder='all'


def _tree_folder(name, *, cls=None, folder_class="IPF.Note", total=5, children=()):
    """A folder-tree node; cls makes it an instance of an exchangelib folder class."""
    if cls is None:
        return SimpleNamespace(name=name, folder_class=folder_class, total_count=total, children=list(children),
                               child_folder_count=len(children))
    sub = type(cls.__name__, (cls,), {"children": property(lambda self: self.__dict__["kids"])})
    node = sub.__new__(sub)  # no EWS round trip: only isinstance() and the attributes below matter
    for k, v in dict(name=name, folder_class=folder_class, total_count=total, kids=list(children),
                     child_folder_count=len(children)).items():
        object.__setattr__(node, k, v)
    return node


def test_mail_folders_skips_system_and_non_mail_folders():
    from exchangelib.folders import Calendar, DeletedItems, Drafts, JunkEmail, SentItems, SyncIssues

    from ews_mcp_lite.tools.common import mail_folders
    atlas = _tree_folder("Atlas")
    tree = [
        _tree_folder("Inbox", children=[_tree_folder("Projects", total=0, children=[atlas])]),
        _tree_folder("Sent Items", cls=SentItems),
        _tree_folder("Deleted Items", cls=DeletedItems, children=[_tree_folder("Old")]),
        _tree_folder("Junk Email", cls=JunkEmail),
        _tree_folder("Drafts", cls=Drafts),
        _tree_folder("Sync Issues", cls=SyncIssues, children=[_tree_folder("Conflicts")]),
        _tree_folder("Calendar", cls=Calendar, folder_class="IPF.Appointment"),
        _tree_folder("RSS Feeds", folder_class="IPF.Note.OutlookHomepage"),
        _tree_folder("Archive", total=0, children=[_tree_folder("2024")]),
    ]
    reloaded = []
    account = SimpleNamespace(msg_folder_root=SimpleNamespace(children=tree),
                              root=SimpleNamespace(clear_cache=lambda: reloaded.append(1)))
    paths = [p for p, _ in mail_folders(account)]
    assert paths == ["Inbox", "Inbox/Projects/Atlas", "Sent Items", "Archive/2024"], "empty folders are skipped"
    assert reloaded, "the folder tree is re-read for fresh counts"
    assert [p for p, _ in mail_folders(account, skip=(SentItems,))] == ["Inbox", "Inbox/Projects/Atlas",
                                                                        "Archive/2024"]


def _two_folders(monkeypatch, gw, inbox_rows, atlas_rows):
    from ews_mcp_lite.tools import mail
    inbox, atlas = FakeFolder(lambda q: FakeQS(inbox_rows)), FakeFolder(lambda q: FakeQS(atlas_rows))
    monkeypatch.setattr(mail, "mail_folders", lambda account, skip=(): [("Inbox", inbox), ("Inbox/Atlas", atlas)])
    by_id = {m.id: m for m in inbox_rows + atlas_rows}
    gw.mock_account.fetch.side_effect = lambda ids, folder, only_fields: [by_id[i] for i, _ in ids]
    return inbox, atlas


@pytest.mark.anyio
async def test_search_all_merges_folders_newest_first_and_names_the_folder(server_factory, monkeypatch):
    mcp, _, gw = server_factory(ews_server_type="grommunio")
    inbox_rows = [_item(i, days_ago=d) for i, d in ((1, 0), (2, 2), (3, 4))]
    atlas_rows = [_item(i, days_ago=d) for i, d in ((10, 1), (11, 3))]
    inbox, atlas = _two_folders(monkeypatch, gw, inbox_rows, atlas_rows)
    ok, res = await call(mcp, "search_messages", {"query": "plan", "limit": 3})
    assert ok, res
    assert [(c["subject"], c["folder"]) for c in res["items"]] == [("S1", "Inbox"), ("S10", "Inbox/Atlas"),
                                                                   ("S2", "Inbox")]
    assert res["next_offset"] == 3 and res["note"].startswith("searched 2 mail folders")
    assert inbox.made[0].only_fields == ("id", "changekey", "datetime_received", "datetime_sent"), \
        "folders are read light; only the page is fetched in full"
    ok, res = await call(mcp, "search_messages", {"query": "plan", "offset": 3, "limit": 3})
    assert [c["subject"] for c in res["items"]] == ["S11", "S3"] and res["next_offset"] is None


@pytest.mark.anyio
async def test_search_default_scope_all_for_searches_inbox_for_listing(server_factory, monkeypatch):
    mcp, _, gw = server_factory(ews_server_type="grommunio")
    inbox, atlas = _two_folders(monkeypatch, gw, [_item(1)], [_item(2)])
    gw.mock_account.inbox = FakeFolder(lambda q: FakeQS([_item(3)]))
    for args, expected in (({"sender": "bob"}, {"S1", "S2"}), ({"subject": "S"}, {"S1", "S2"}),
                           ({}, {"S3"}), ({"unread_only": True}, {"S3"}),
                           ({"query": "x", "folder": "inbox"}, {"S3"}), ({"folder": "all"}, {"S1", "S2"})):
        ok, res = await call(mcp, "search_messages", args)
        assert ok and {c["subject"] for c in res["items"]} == expected, (args, res)
        assert all(("folder" in c) == (expected != {"S3"}) for c in res["items"]), args


@pytest.mark.anyio
async def test_search_all_limits_the_page_window(server_factory):
    mcp, _, _ = server_factory()
    ok, res = await call(mcp, "search_messages", {"query": "x", "offset": 990, "limit": 20})
    assert not ok and res["code"] == "validation" and "folder='all'" in res["message"]
