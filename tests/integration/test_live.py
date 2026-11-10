"""End-to-end tool tests against a live EWS server (see conftest for setup)."""

import base64
import datetime as dt
import uuid

import anyio
import pytest
from exchangelib import Attendee, CalendarItem, FileAttachment, HTMLBody, Mailbox, Message, Q, Task
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, MeetingRequest

from ews_mcp_lite.app import build_server
from tests.conftest import call, confirmed
from tests.integration.conftest import PEER, SERVER_TYPE, USER, settings, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def ok(server, name, args=None):
    good, res = await call(server, name, args or {})
    assert good, f"{name} failed: {res}"
    return res


# ------------------------------------------------------------------ reads


async def test_status_probe(server):
    res = await ok(server, "get_server_status", {"probe": True})
    assert res["tools"] == 28 and "inbox_total" in res["probe"]


async def test_folders_search_message_attachment(server):
    folders = await ok(server, "list_folders", {"depth": 1})
    assert any(f["name"].lower() == "inbox" for f in folders["items"])
    found = await ok(server, "search_messages", {"limit": 50})
    assert found["count"] >= 1 and all(c["date"] for c in found["items"]), "every card needs a date"
    with_att = [c for c in found["items"] if c.get("attach")]
    if with_att:
        msg = await ok(server, "get_message", {"id": with_att[0]["id"]})
        assert msg["message"]["attachments"]
        att = await ok(server, "get_attachment", {"message_id": with_att[0]["id"]})
        assert att["mode"] in ("text", "base64", "metadata")
    first = found["items"][0]
    words = [w for w in first["subject"].split() if len(w) > 3]
    if words:
        hit = await ok(server, "search_messages", {"query": words[0]})
        assert any(c["id"] == first["id"] for c in hit["items"])
    since = await ok(server, "search_messages", {"since": "-30d", "until": "+1d"})
    assert since["count"] >= 1
    thread = await ok(server, "get_thread", {"id": first["id"]})
    assert thread["count"] >= 1


PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
CSV_BYTES = b"Item,Amount\nTravel,1200\nHardware,4800\n"


def _stored(me, folder, subject: str, body: str, sender: str, to: str, attachments=()) -> Message:
    m = Message(account=me, folder=folder, subject=subject, body=body, author=Mailbox(email_address=sender),
                to_recipients=[Mailbox(email_address=to)])
    m.save()
    for name, content in attachments:
        m.attach(FileAttachment(name=name, content=content))
    return m


async def test_attachment_metadata_and_content(server, mark, me):
    """Attachment metadata is visible at every level (card flag, get_message, get_thread)
    and get_attachment returns the content by name and by index."""
    subject = f"{mark} attachments"
    _stored(me, me.inbox, subject, "two files attached", PEER or USER, USER,
            [("numbers.csv", CSV_BYTES), ("scan.pdf", PDF_BYTES)])
    _stored(me, me.inbox, f"{mark} plain", "no files here", PEER or USER, USER)
    found = await ok(server, "search_messages", {"subject": mark, "has_attachments": True})
    assert [c["subject"] for c in found["items"]] == [subject], "has_attachments filter"
    card = found["items"][0]
    assert card["attach"] is True
    plain = await ok(server, "search_messages", {"subject": f"{mark} plain"})
    assert "attach" not in plain["items"][0]

    msg = (await ok(server, "get_message", {"id": card["id"]}))["message"]
    atts = {a["name"]: a for a in msg["attachments"]}
    assert set(atts) == {"numbers.csv", "scan.pdf"}
    assert sorted(a["index"] for a in atts.values()) == [0, 1]
    assert atts["numbers.csv"]["size_bytes"] == len(CSV_BYTES), atts
    assert atts["scan.pdf"]["size_bytes"] == len(PDF_BYTES), atts
    assert all(a["content_type"] for a in atts.values()), atts
    assert "base64" not in str(msg) and "Travel,1200" not in str(msg), "get_message lists metadata, not content"

    thread = await ok(server, "get_thread", {"id": card["id"]})
    in_thread = next(i for i in thread["items"] if i["id"] == card["id"])
    assert {a["name"] for a in in_thread["attachments"]} == {"numbers.csv", "scan.pdf"}

    csv = await ok(server, "get_attachment", {"message_id": card["id"], "attachment": "numbers.csv"})
    assert csv["mode"] == "text" and csv["text"] == CSV_BYTES.decode() and csv["size_bytes"] == len(CSV_BYTES)
    pdf = await ok(server, "get_attachment", {"message_id": card["id"], "attachment": str(atts["scan.pdf"]["index"])})
    assert pdf["mode"] == "base64" and base64.b64decode(pdf["base64"]) == PDF_BYTES and pdf["name"] == "scan.pdf"
    good, err = await call(server, "get_attachment", {"message_id": card["id"], "attachment": "missing.doc"})
    assert not good and err["code"] == "not_found"
    good, err = await call(server, "get_attachment", {"message_id": plain["items"][0]["id"]})
    assert not good and err["code"] == "not_found"


async def test_get_thread_follows_conversation_across_inbox_and_sent(server, mark, me):
    """A conversation is rebuilt from Inbox and Sent Items, oldest first, from any of its
    messages or from the thread id; unrelated mail stays out."""
    topic = f"{mark} thread {uuid.uuid4().hex[:6]}"
    peer = PEER or "peer@example.com"
    _stored(me, me.inbox, topic, "first: can you review the plan?", peer, USER)
    await anyio.sleep(1.1)  # distinct timestamps: the order is asserted
    _stored(me, me.sent, f"RE: {topic}", "second: yes, by Friday", USER, peer)
    await anyio.sleep(1.1)
    _stored(me, me.inbox, f"RE: {topic}", "third: thanks, Friday is fine", peer, USER, [("plan.csv", CSV_BYTES)])
    _stored(me, me.inbox, f"{mark} unrelated", "not part of it", peer, USER)

    found = await ok(server, "search_messages", {"subject": topic})  # default scope: all mail folders
    assert sorted(c["folder"] for c in found["items"]) == ["Inbox", "Inbox", "Sent Items"], found["items"]
    assert len({c["thread"] for c in found["items"]}) == 1, found["items"]
    inbox_only = await ok(server, "search_messages", {"subject": topic, "folder": "inbox"})
    assert inbox_only["count"] == 2 and all("folder" not in c for c in inbox_only["items"])
    newest = found["items"][0]
    for ref in (newest["id"], newest["thread"]):
        thread = await ok(server, "get_thread", {"id": ref})
        bodies = [i["body"].split(":")[0] for i in thread["items"]]
        assert bodies == ["first", "second", "third"], (ref, thread)
        assert thread["thread"] == newest["thread"] and "note" not in thread
        assert [bool(i.get("attachments")) for i in thread["items"]] == [False, False, True]
    sent = await ok(server, "search_messages", {"subject": topic, "folder": "sent"})
    from_sent = await ok(server, "get_thread", {"id": sent["items"][0]["id"]})
    assert from_sent["count"] == 3, "starting from the sent message finds the same thread"


async def test_search_follows_server_type(gateway):
    """EWS_IT_SERVER_TYPE must match the server: a nonsense term finds nothing (on
    grommunio AQS would return the whole folder), and a subject word finds its message."""
    mcp, ctx = build_server(settings(), gateway)
    none = await ok(mcp, "search_messages", {"query": f"zz{uuid.uuid4().hex}"})
    assert none["count"] == 0, f"unfiltered results: is EWS_IT_SERVER_TYPE={SERVER_TYPE} right?"
    assert ("search index" in none.get("note", "")) is (ctx.settings.search_mode == "aqs")
    newest = await ok(mcp, "search_messages", {"limit": 1})
    words = [w for w in newest["items"][0]["subject"].split() if len(w) > 3 and w.isalnum()] if newest["items"] else []
    if words:
        hit = await ok(mcp, "search_messages", {"query": words[0], "limit": 100})
        assert any(c["id"] == newest["items"][0]["id"] for c in hit["items"])


async def test_search_finds_body_only_terms(gateway):
    """'pre-approval' is only in the seeded body of 'FYI: new expense policy'
    (grommunio: via the PR_BODY fallback, not 'subjects only')."""
    mcp, _ = build_server(settings(), gateway)
    hit = await ok(mcp, "search_messages", {"query": "pre-approval"})
    assert any(c["subject"] == "FYI: new expense policy" for c in hit["items"]), hit
    assert "subjects only" not in hit.get("note", "")
    none = await ok(mcp, "search_messages", {"query": f"zz{uuid.uuid4().hex}"})
    assert none["count"] == 0


async def test_search_html_only_bodies(gateway, mark, me, peer):
    """Delivered HTML-only mail is found by body. On grommunio, an HTML-only item
    created via EWS (no SMTP delivery) gets no PR_BODY, so its body is not searchable."""
    mcp, _ = build_server(settings(), gateway)
    sent_word, saved_word = f"zx{uuid.uuid4().hex[:10]}", f"zx{uuid.uuid4().hex[:10]}"
    Message(account=peer, subject=f"{mark} html sent", body=HTMLBody(f"<p>Hi <b>{sent_word}</b></p>"),
            to_recipients=[Mailbox(email_address=USER)]).send(save_copy=False)
    Message(account=me, folder=me.drafts, subject=f"{mark} html saved",
            body=HTMLBody(f"<p>Hi <b>{saved_word}</b></p>")).save()
    assert wait_for(lambda: list(me.inbox.filter(subject=f"{mark} html sent").only("id")))
    hit = await ok(mcp, "search_messages", {"query": sent_word})
    assert [c["subject"] for c in hit["items"]] == [f"{mark} html sent"], hit
    saved = await ok(mcp, "search_messages", {"query": saved_word, "folder": "drafts"})
    if not saved["items"]:
        pytest.xfail("server keeps no plain-text body (PR_BODY) for HTML-only items saved via EWS")
    assert [c["subject"] for c in saved["items"]] == [f"{mark} html saved"]

async def test_overview_tasks_oof_waiting(server):
    ov = await ok(server, "get_mailbox_overview")
    assert "unread_total" in ov and isinstance(ov["today_events"], list)
    await ok(server, "list_tasks", {"include_completed": True})
    oof = await ok(server, "get_oof_settings")
    assert oof["state"] in ("disabled", "enabled", "scheduled")
    wait = await ok(server, "waiting_on", {"days": 1})
    assert "scanned" in wait["note"]


async def test_people(server, peer):
    found = await ok(server, "find_people", {"query": PEER.split("@")[0]})
    assert any(p["email"] == PEER for p in found["items"])
    person = await ok(server, "get_contact", {"id": PEER})
    assert person["person"]["email"] == PEER


async def test_calendar_reads(server):
    events = await ok(server, "list_events", {"start": "-7d", "end": "+30d"})
    if events["items"]:
        ev = await ok(server, "get_event", {"id": events["items"][0]["id"]})
        assert ev["event"]["subject"] == events["items"][0]["subject"]


async def test_check_availability(server, peer):
    res = await ok(server, "check_availability",
                   {"attendees": [USER, PEER], "start": "+1d", "end": "+3d", "working_hours_only": False})
    assert set(res["attendees"]) == {USER, PEER}
    assert all("busy" in v for v in res["attendees"].values())


# ------------------------------------------------------------------ drafts and mail writes


async def test_draft_lifecycle(server, mark, me):
    d = await ok(server, "create_draft", {"to": ["someone@example.com"], "subject": f"{mark} draft",
                                         "body": "first version"})
    did = d["draft_id"]
    got = await ok(server, "get_message", {"id": did})
    assert got["message"]["subject"] == f"{mark} draft"
    await ok(server, "update_draft", {"draft_id": did, "body": "second version", "subject": f"{mark} draft v2"})
    got = await ok(server, "get_message", {"id": did})
    assert "second version" in got["message"]["body"]
    upd = await ok(server, "update_messages", {"ids": [did], "categories_add": ["ItTest"]})
    assert upd["updated"] == 1
    await ok(server, "delete_draft", {"draft_id": did})
    assert wait_for(lambda: list(me.trash.filter(subject=f"{mark} draft v2").only("id")))


async def test_reply_draft(server, mark):
    msgs = await ok(server, "search_messages", {"limit": 5})
    original = msgs["items"][0]
    d = await ok(server, "create_draft", {"reply_to": original["id"], "body": f"reply {mark}",
                                         "subject": f"Re: {mark} {original['subject']}"})
    got = await ok(server, "get_message", {"id": d["draft_id"]})
    assert got["message"]["subject"].startswith("Re:") and got["message"]["to"]


async def test_move_and_soft_then_permanent_delete(server, mark, me):
    a = await ok(server, "create_draft", {"to": ["x@example.com"], "subject": f"{mark} del-a", "body": "a"})
    b = await ok(server, "create_draft", {"to": ["x@example.com"], "subject": f"{mark} del-b", "body": "b"})
    moved = await ok(server, "move_messages", {"ids": [a["draft_id"]], "to_folder": "trash"})
    assert moved["moved"] == [a["draft_id"]]
    soft = await confirmed(server, "delete_messages", {"ids": [b["draft_id"]]})
    assert soft["recoverable"] is True
    assert wait_for(lambda: list(me.trash.filter(subject=f"{mark} del-b").only("id")))
    hard = await confirmed(server, "delete_messages", {"ids": [a["draft_id"]], "permanent": True})
    assert hard["recoverable"] is False and hard["deleted"]
    assert not list(me.trash.filter(subject=f"{mark} del-a").only("id"))


async def test_send_draft_reaches_peer(server, mark, peer):
    d = await ok(server, "create_draft", {"to": [PEER], "subject": f"{mark} send", "body": "hello"})
    sent = await confirmed(server, "send_draft", {"draft_id": d["draft_id"]})
    assert sent["sent"] is True
    assert wait_for(lambda: list(peer.inbox.filter(subject=f"{mark} send").only("id")))


async def test_kill_switch_live(gateway, mark):
    mcp, _ = build_server(settings(send_enabled=False), gateway)
    d = await ok(mcp, "create_draft", {"to": ["x@example.com"], "subject": f"{mark} ks", "body": "x"})
    good, err = await call(mcp, "send_draft", {"draft_id": d["draft_id"]})
    assert not good and err["code"] == "kill_switch"


# ------------------------------------------------------------------ calendar writes


def _day(hour: int, days: int = 5) -> str:
    return (dt.date.today() + dt.timedelta(days=days)).isoformat() + f"T{hour:02d}:00"


async def test_plain_event_lifecycle(server, mark):
    ev = await ok(server, "create_event", {"subject": f"{mark} plain", "start": _day(9), "end": _day(10),
                                          "location": "Room 1"})
    listed = await ok(server, "list_events", {"start": _day(0), "end": _day(23)})
    assert any(e["id"] == ev["event_id"] for e in listed["items"])
    full = await ok(server, "get_event", {"id": ev["event_id"]})
    assert full["event"]["location"] == "Room 1"
    await ok(server, "update_event", {"event_id": ev["event_id"], "location": "Room 2"})
    full = await ok(server, "get_event", {"id": ev["event_id"]})
    assert full["event"]["location"] == "Room 2"
    res = await confirmed(server, "cancel_event", {"event_id": ev["event_id"]})
    assert res["cancelled"] and res["cancellations_sent"] is False


async def test_meeting_invite_accept_cancel(server, mark, peer):
    ev = await ok(server, "create_event", {"subject": f"{mark} meeting", "start": _day(11), "end": _day(12),
                                          "attendees": [PEER], "send_invitations": True})
    assert ev["invitations_sent"] is True
    reqs = wait_for(lambda: [m for m in peer.inbox.filter(subject=f"{mark} meeting")
                             if isinstance(m, MeetingRequest)])
    assert reqs, "peer never received the meeting request"
    reqs[0].accept()
    full = None
    for _ in range(15):
        full = await ok(server, "get_event", {"id": ev["event_id"]})
        people = full["event"]["attendees"] or []
        if any(p["email"] == PEER and p["response"] == "Accept" for p in people):
            break
        await anyio.sleep(1)
    assert any(p["email"] == PEER for p in (full["event"]["attendees"] or []))
    res = await confirmed(server, "cancel_event", {"event_id": ev["event_id"], "message": "cancelled by test"})
    assert res["cancellations_sent"] is True
    assert wait_for(lambda: list(peer.inbox.filter(Q(subject__icontains=mark) & Q(subject__icontains="meeting"))
                                 .only("subject")))


async def test_respond_to_peer_invite(server, mark, peer):
    start = dt.datetime.combine(dt.date.today() + dt.timedelta(days=6), dt.time(14), tzinfo=dt.UTC)
    item = CalendarItem(account=peer, folder=peer.calendar, subject=f"{mark} invite",
                        start=start, end=start + dt.timedelta(hours=1),
                        required_attendees=[Attendee(mailbox=Mailbox(email_address=USER))])
    item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY)

    async def find():
        res = await ok(server, "list_events", {"start": "+5d", "end": "+8d"})
        return [e for e in res["items"] if e["subject"] == f"{mark} invite"]
    events = []
    for _ in range(20):
        events = await find()
        if events:
            break
        await anyio.sleep(1)
    assert events, "invite never showed up in the calendar"
    res = await confirmed(server, "respond_to_event", {"event_id": events[0]["id"], "response": "accept"})
    assert res["responded"] == "accept"


# ------------------------------------------------------------------ tasks and OOF


async def test_update_task(server, mark, me):
    Task(account=me, folder=me.tasks, subject=f"{mark} task").save()
    tasks = await ok(server, "list_tasks", {"limit": 100})
    task = next(t for t in tasks["items"] if t["subject"] == f"{mark} task")
    res = await ok(server, "update_task", {"id": task["id"], "due": "+3d"})
    assert res["due"]
    res = await ok(server, "update_task", {"id": task["id"], "complete": True})
    assert res["complete"] is True


async def test_oof_roundtrip(server):
    before = await ok(server, "get_oof_settings")
    try:
        await confirmed(server, "set_oof", {"state": "enabled", "internal_reply": "Away (integration test)"})
        now = await ok(server, "get_oof_settings")
        assert now["state"] == "enabled" and "Away" in now["internal_reply"]
    finally:
        await confirmed(server, "set_oof", {"state": "disabled"})
    after = await ok(server, "get_oof_settings")
    assert after["state"] == "disabled" or before["state"] != "disabled"


# ------------------------------------------------------------------ two-phase confirm, live


async def test_send_draft_confirm_flow_exactly_one_mail(server, mark, peer):
    subject = f"{mark} confirm-send"
    d = await ok(server, "create_draft", {"to": [PEER], "subject": subject, "body": "approved text"})
    preview = await ok(server, "send_draft", {"draft_id": d["draft_id"]})
    assert preview["requires_confirmation"] and preview["preview"]["to"] == [PEER]
    assert preview["preview"]["subject"] == subject
    # nothing was sent by the preview
    assert not wait_for(lambda: list(peer.inbox.filter(subject=subject).only("id")), timeout=4)
    token = preview["confirm_token"]
    sent = await ok(server, "send_draft", {"draft_id": d["draft_id"], "confirm_token": token})
    assert sent["sent"] is True
    good, err = await call(server, "send_draft", {"draft_id": d["draft_id"], "confirm_token": token})
    assert not good and err["code"] == "confirm_invalid" and "already used" in err["message"]
    assert wait_for(lambda: list(peer.inbox.filter(subject=subject).only("id")))
    await anyio.sleep(3)
    assert len(list(peer.inbox.filter(subject=subject).only("id"))) == 1, "peer must get exactly one mail"


async def test_send_draft_token_dies_when_draft_is_edited(server, mark, peer):
    subject = f"{mark} confirm-edit"
    d = await ok(server, "create_draft", {"to": [PEER], "subject": subject, "body": "version 1"})
    preview = await ok(server, "send_draft", {"draft_id": d["draft_id"]})
    await ok(server, "update_draft", {"draft_id": d["draft_id"], "body": "version 2"})
    good, err = await call(server, "send_draft", {"draft_id": d["draft_id"],
                                                  "confirm_token": preview["confirm_token"]})
    assert not good and err["code"] == "confirm_invalid"
    assert not wait_for(lambda: list(peer.inbox.filter(subject=subject).only("id")), timeout=4)


async def test_delete_messages_confirm_flow(server, mark, me):
    subject = f"{mark} confirm-delete"
    d = await ok(server, "create_draft", {"to": ["x@example.com"], "subject": subject, "body": "x"})
    preview = await ok(server, "delete_messages", {"ids": [d["draft_id"]]})
    assert preview["requires_confirmation"] and preview["preview"]["recoverable"] is True
    assert list(me.drafts.filter(subject=subject).only("id")), "preview must not delete"
    token = preview["confirm_token"]
    res = await ok(server, "delete_messages", {"ids": [d["draft_id"]], "confirm_token": token})
    assert res["deleted"] == [d["draft_id"]]
    assert wait_for(lambda: list(me.trash.filter(subject=subject).only("id")))
    good, err = await call(server, "delete_messages", {"ids": [d["draft_id"]], "confirm_token": token})
    assert not good and err["code"] == "confirm_invalid"


async def test_recipient_guard_live(gateway, mark, me):
    """Drafts may name anyone; the allowlist is enforced on the draft's recipients at send time."""
    mcp, _ = build_server(settings(ews_recipient_allowlist="*@grommunio.test"), gateway)
    subject = f"{mark} rg"
    d = await ok(mcp, "create_draft", {"to": ["someone@example.com"], "subject": subject, "body": "x"})
    good, err = await call(mcp, "send_draft", {"draft_id": d["draft_id"]})
    assert not good and err["code"] == "recipient_blocked"
    assert list(me.drafts.filter(subject=subject).only("id")), "a blocked send must leave the draft in place"


async def test_search_all_finds_mail_in_new_subfolder_by_sender_name(server, mark, me):
    """folder='all' (the default for searches) reaches a subfolder created after the server
    started, and a sender name fragment is matched server-side."""
    from exchangelib.folders import Folder
    sub = Folder(parent=me.inbox, name=f"{mark} sub")
    sub.save()
    try:
        m = Message(account=me, folder=sub, subject=f"{mark} filed away", body="kept in a subfolder",
                    author=Mailbox(name="Katarina Lindqvist", email_address="k.lindqvist@uni-example.edu"),
                    to_recipients=[Mailbox(email_address=USER)])
        m.save()
        found = await ok(server, "search_messages", {"subject": f"{mark} filed"})
        assert [(c["subject"], c["folder"]) for c in found["items"]] == [(f"{mark} filed away", f"Inbox/{mark} sub")]
        by_name = await ok(server, "search_messages", {"sender": "lindqv", "subject": mark})
        assert [c["subject"] for c in by_name["items"]] == [f"{mark} filed away"], by_name
        assert "incomplete" not in by_name.get("note", ""), "sender name must be matched server-side"
        assert not (await ok(server, "search_messages", {"subject": f"{mark} filed", "folder": "inbox"}))["items"]
    finally:
        sub.delete()


async def test_inline_images_and_big_attachments(server, mark, me):
    """Inline images are listed apart from real attachments; files over 512 KiB are refused
    from their size alone, which the server must report."""
    big = b"%PDF-1.4\n" + b"0" * (700 * 1024)
    html = ('<html><body><p>Numbers attached.</p><img src="cid:logo@corp"><p>--</p>'
            '<img src="cid:sig@corp"></body></html>')
    m = Message(account=me, folder=me.inbox, subject=f"{mark} inline", body=HTMLBody(html),
                author=Mailbox(email_address=PEER or USER), to_recipients=[Mailbox(email_address=USER)])
    m.save()
    m.attach(FileAttachment(name="logo.png", content=b"\x89PNG-logo", content_type="image/png", is_inline=True,
                            content_id="logo@corp"))
    m.attach(FileAttachment(name="sig.png", content=b"\x89PNG-sig", content_type="image/png", content_id="sig@corp"))
    m.attach(FileAttachment(name="numbers.csv", content=CSV_BYTES, content_type="text/csv"))
    m.attach(FileAttachment(name="big.pdf", content=big, content_type="application/pdf"))
    only_inline = Message(account=me, folder=me.inbox, subject=f"{mark} logo only", body=HTMLBody(html),
                          author=Mailbox(email_address=PEER or USER), to_recipients=[Mailbox(email_address=USER)])
    only_inline.save()
    only_inline.attach(FileAttachment(name="logo.png", content=b"\x89PNG", content_type="image/png", is_inline=True,
                                      content_id="logo@corp"))

    card = (await ok(server, "search_messages", {"subject": f"{mark} inline"}))["items"][0]
    msg = (await ok(server, "get_message", {"id": card["id"]}))["message"]
    assert sorted(a["name"] for a in msg["attachments"]) == ["big.pdf", "numbers.csv"], msg
    assert sorted(a["name"] for a in msg["inline_images"]) == ["logo.png", "sig.png"], msg
    sizes = {a["name"]: a["size_bytes"] for a in msg["attachments"]}
    assert sizes["big.pdf"] and sizes["big.pdf"] >= len(big), f"server must report sizes: {sizes}"

    default = await ok(server, "get_attachment", {"message_id": card["id"]})
    assert default["name"] in ("numbers.csv", "big.pdf") and "inline" not in default
    pdf = await ok(server, "get_attachment", {"message_id": card["id"], "attachment": "big.pdf"})
    assert pdf["mode"] == "metadata" and "not downloaded" in pdf["note"], pdf
    logo_index = next(a["index"] for a in msg["inline_images"] if a["name"] == "logo.png")
    logo = await ok(server, "get_attachment", {"message_id": card["id"], "attachment": str(logo_index)})
    assert logo["inline"] is True and base64.b64decode(logo["base64"]) == b"\x89PNG-logo"

    lone = (await ok(server, "search_messages", {"subject": f"{mark} logo only"}))["items"][0]
    print(f"\n[inline-only] card attach flag on this server: {lone.get('attach', False)}")
    good, err = await call(server, "get_attachment", {"message_id": lone["id"]})
    assert not good and err["code"] == "not_found" and "inline image" in err["message"], err
