"""Mail tools: folders, search, read, threads, attachments, drafts, bulk updates."""

import base64
import re
from datetime import timedelta
from itertools import islice
from typing import Annotated, Any, Literal

from exchangelib import Body, Message, Q
from exchangelib.properties import ConversationId
from pydantic import Field

from ..dates import parse_when
from ..dto import msg_card, msg_full, page
from ..errors import Failure
from ..registry import Registry
from .common import check_page, fetch_one, mailboxes, resolve_folder, resolve_many, take

CARD_FIELDS = ["id", "changekey", "subject", "sender", "datetime_received", "datetime_sent",
               "is_read", "has_attachments", "conversation_id", "importance", "categories",
               "text_body"]
SENDER_SCAN_CAP = 500
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")
MAX_INLINE_BYTES = 512 * 1024


def _ordered(qs: Any) -> Any:
    try:
        return qs.order_by("-datetime_received")
    except Exception:
        return qs


def register(r: Registry) -> None:
    ctx = r.ctx

    @r.tool("read", "read")
    async def list_folders(
        parent: Annotated[str | None, Field(description="Start folder (alias, path or well-known name). Default: mailbox root.")] = None,
        depth: Annotated[int, Field(ge=1, le=4)] = 2,
    ) -> dict[str, Any]:
        """List mail folders as a tree walk: id (f-alias), name, path, total, unread, children."""
        def work(account):
            root = resolve_folder(account, parent, ctx.aliases) if parent else account.msg_folder_root
            out: list[dict[str, Any]] = []

            def walk(folder, prefix: str, level: int):
                for child in sorted(folder.children, key=lambda c: (c.name or "").lower()):
                    path = f"{prefix}{child.name}"
                    out.append({"id": ctx.aliases.alias(child.id, "f"), "name": child.name,
                                "path": path, "total": child.total_count,
                                "unread": child.unread_count,
                                "children": child.child_folder_count})
                    if level < depth and child.child_folder_count:
                        walk(child, path + "/", level + 1)
            walk(root, "", 1)
            return {"items": out, "count": len(out)}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def search_messages(
        query: Annotated[str | None, Field(description="Free text matched against subject and body (substring).")] = None,
        folder: Annotated[str, Field(description="Folder: inbox, sent, drafts, trash, an f-alias or a path.")] = "inbox",
        sender: Annotated[str | None, Field(
            description="Exact sender address (server-side) or a name/email substring (client-side, first 500 items).")] = None,
        subject: Annotated[str | None, Field(description="Subject substring.")] = None,
        since: Annotated[str | None, Field(description="'today', '-7d', YYYY-MM-DD or ISO datetime.")] = None,
        until: Annotated[str | None, Field(description="'today', '+1d', YYYY-MM-DD or ISO datetime.")] = None,
        unread_only: bool = False,
        has_attachments: bool | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search mail live in one folder, newest first. Returns compact cards with
        short ids (m1, m2 …) for get_message / get_thread / drafts."""
        check_page(offset, limit)
        base = Q()
        if subject:
            base &= Q(subject__icontains=subject)
        if since:
            base &= Q(datetime_received__gte=parse_when(since, "since", ctx.tz))
        if until:
            base &= Q(datetime_received__lte=parse_when(until, "until", ctx.tz))
        if unread_only:
            base &= Q(is_read=False)
        if has_attachments is not None:
            base &= Q(has_attachments=has_attachments)
        exact_sender = sender if sender and "@" in sender and " " not in sender.strip() else None
        if exact_sender:
            base &= Q(sender=exact_sender.strip())

        def run(account, q):
            target = resolve_folder(account, folder, ctx.aliases)
            qs = _ordered(target.all() if q.is_empty() else target.filter(q)).only(*CARD_FIELDS)
            if sender and not exact_sender:
                needle = sender.lower()
                rows = (m for m in islice(qs, SENDER_SCAN_CAP) if needle in
                        f"{getattr(m.sender, 'name', '')} {getattr(m.sender, 'email_address', '')}".lower())
                return take(rows, offset, limit)
            return take(qs, offset, limit)

        def work(account):
            note = None
            if query:
                text = Q(subject__icontains=query) | Q(body__icontains=query)
                try:
                    rows, more = run(account, base & text)
                except Exception as e:
                    if "ErrorInvalidRestriction" not in f"{type(e).__name__} {e}":
                        raise
                    # Some servers (grommunio) cannot run Contains on the body.
                    rows, more = run(account, base & Q(subject__icontains=query))
                    note = "this server cannot search message bodies; matched subjects only"
            else:
                rows, more = run(account, base)
            out = page([msg_card(m, ctx.aliases, ctx.tz) for m in rows], offset, more)
            if note:
                out["note"] = note
            return out
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_message(
        id: Annotated[str, Field(description="Message or draft id (m3, d1) or a raw EWS id.")],
        max_chars: Annotated[int, Field(ge=200, le=50000)] = 4000,
    ) -> dict[str, Any]:
        """Fetch one message: recipients, cleaned body (latest reply only), attachment list."""
        raw, _ = ctx.aliases.resolve(id, expect=("m", "d"))

        def work(account):
            item = fetch_one(account, raw)
            return {"message": msg_full(item, ctx.aliases, ctx.tz, max_chars,
                                        prefix=id[0] if id[0] in "md" else "m")}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_thread(
        id: Annotated[str, Field(description="A message id (m3) or thread id (t2).")],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """Rebuild a conversation from Inbox and Sent Items, oldest first, each body
        trimmed to its newest reply."""
        if id.startswith("t"):
            conv_raw, _ = ctx.aliases.resolve(id, expect=("t",))
            seed_raw = None
        else:
            seed_raw, _ = ctx.aliases.resolve(id, expect=("m", "d"))
            conv_raw = None

        def work(account):
            nonlocal conv_raw
            seed = None
            if seed_raw:
                seed = fetch_one(account, seed_raw)
                conv_raw = getattr(getattr(seed, "conversation_id", None), "id", None)
            items: list[Any] = []
            note = None
            if conv_raw:
                try:
                    for f in (account.inbox, account.sent):
                        items += list(f.filter(conversation_id=ConversationId(id=conv_raw))[:limit])
                except Exception:
                    items, note = [], "this server cannot filter by conversation; showing the message only"
            if not items and seed is not None:
                items = [seed]
            seen, unique = set(), []
            for it in items:
                if it.id not in seen:
                    seen.add(it.id)
                    unique.append(it)
            unique.sort(key=lambda m: (m.datetime_received or m.datetime_sent or m.datetime_created))
            cards = [msg_full(m, ctx.aliases, ctx.tz, 1500) for m in unique[:limit]]
            out: dict[str, Any] = {"thread": ctx.aliases.alias(conv_raw, "t") if conv_raw else None,
                                   "subject": cards[0]["subject"] if cards else "",
                                   "items": cards, "count": len(cards)}
            if note:
                out["note"] = note
            return out
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_attachment(
        message_id: Annotated[str, Field(description="Message id (m3) that carries the attachment.")],
        attachment: Annotated[str | None, Field(description="Attachment name or index; default: the first one.")] = None,
    ) -> dict[str, Any]:
        """Read one attachment. Text-like files come back as text, others as base64
        (up to 512 KiB); bigger files return metadata only."""
        raw, _ = ctx.aliases.resolve(message_id, expect=("m", "d"))

        def work(account):
            item = fetch_one(account, raw)
            atts = list(getattr(item, "attachments", None) or [])
            if not atts:
                raise Failure("not_found", "This message has no attachments.")
            chosen = atts[0]
            if attachment is not None:
                if attachment.isdigit() and int(attachment) < len(atts):
                    chosen = atts[int(attachment)]
                else:
                    chosen = next((a for a in atts if (a.name or "") == attachment), None)
                    if chosen is None:
                        raise Failure("not_found", f"No attachment named {attachment!r}.",
                                      "Call get_message to see the attachment list.")
            content = getattr(chosen, "content", None)
            meta = {"name": chosen.name, "size_bytes": getattr(chosen, "size", None),
                    "content_type": getattr(chosen, "content_type", None)}
            if content is None:
                return {**meta, "mode": "metadata", "note": "item attachment; open it with get_message"}
            ctype = (meta["content_type"] or "").lower()
            if len(content) > MAX_INLINE_BYTES:
                return {**meta, "mode": "metadata", "note": "too large to inline"}
            if ctype.startswith(TEXT_TYPES) or (chosen.name or "").lower().endswith((".txt", ".csv", ".md", ".json")):
                return {**meta, "mode": "text", "text": content.decode("utf-8", "replace")}
            return {**meta, "mode": "base64", "base64": base64.b64encode(content).decode()}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def waiting_on(
        days: Annotated[int, Field(ge=1, le=60, description="Only sent mail older than this many days.")] = 3,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        lookback_days: Annotated[int, Field(ge=1, le=90)] = 30,
    ) -> dict[str, Any]:
        """Sent messages with no inbound reply after `days` days ('what am I waiting on?').
        Live scan of Sent Items (capped at 200 messages) against the Inbox."""
        scan_cap = 200

        def work(account):
            tz = ctx.tz
            now = parse_when("now", "now", tz)
            sent = (account.sent.filter(datetime_sent__gte=now - timedelta(days=lookback_days),
                                        datetime_sent__lte=now - timedelta(days=days))
                    .order_by("-datetime_sent")
                    .only("id", "changekey", "subject", "to_recipients", "datetime_sent",
                          "conversation_id", "sender", "is_read", "has_attachments", "text_body"))
            sent_items = list(sent[:scan_cap])
            inbound = list(account.inbox.filter(datetime_sent__gte=now - timedelta(days=lookback_days))
                           .only("subject", "conversation_id", "datetime_sent", "datetime_received")[:1000])
            by_conv: dict[str, list[Any]] = {}
            by_subj: dict[str, list[Any]] = {}
            for m in inbound:
                cid = getattr(getattr(m, "conversation_id", None), "id", None)
                if cid:
                    by_conv.setdefault(cid, []).append(m)
                by_subj.setdefault(_norm_subject(m.subject), []).append(m)
            waiting = []
            for s in sent_items:
                when = s.datetime_sent
                cid = getattr(getattr(s, "conversation_id", None), "id", None)
                candidates = by_conv.get(cid, []) if cid else []
                candidates += by_subj.get(_norm_subject(s.subject), [])
                replied = any((c.datetime_received or c.datetime_sent) and
                              (c.datetime_received or c.datetime_sent) > when for c in candidates)
                if not replied:
                    card = msg_card(s, ctx.aliases, ctx.tz)
                    card["to"] = [r.email_address for r in (s.to_recipients or [])]
                    waiting.append(card)
                if len(waiting) >= limit:
                    break
            return {"items": waiting, "count": len(waiting),
                    "note": (f"scanned {len(sent_items)} sent message(s) from the last {lookback_days} "
                             f"days (cap {scan_cap}); replies matched by conversation or subject")}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_mailbox_overview(
        unread_limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Morning brief in one call: unread count, newest unread inbox cards, today's events."""
        from .calendar import list_view  # local import: calendar registers separately

        def work(account):
            unread = account.inbox.filter(is_read=False)
            rows = list(_ordered(unread).only(*CARD_FIELDS)[:unread_limit])
            account.inbox.refresh()
            start = parse_when("today", "start", ctx.tz)
            events, _ = list_view(account, ctx, start, start + timedelta(days=1), 0, 25)
            return {"unread_total": account.inbox.unread_count,
                    "recent_unread": [msg_card(m, ctx.aliases, ctx.tz) for m in rows],
                    "today_events": events}
        return await ctx.gateway.call(work)

    # ------------------------------------------------------------------ drafts

    @r.tool("draft", "write")
    async def create_draft(
        body: Annotated[str, Field(description="Plain-text body.")],
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        reply_to: Annotated[str | None, Field(description="Message id to reply to (m3). Recipients and subject default from it.")] = None,
        reply_all: bool = False,
        importance: Literal["low", "normal", "high"] = "normal",
    ) -> dict[str, Any]:
        """Save a new mail or a reply as a DRAFT in Drafts. Nothing is sent."""
        reply_raw = ctx.aliases.resolve(reply_to, expect=("m",))[0] if reply_to else None

        def work(account):
            if reply_raw:
                original = fetch_one(account, reply_raw)
                subj = subject or (original.subject if (original.subject or "").lower().startswith("re:")
                                   else f"Re: {original.subject or ''}")
                maker = original.create_reply_all if reply_all else original.create_reply
                kwargs = {}
                if to and not reply_all:
                    kwargs["to_recipients"] = mailboxes(to)
                maker(subj, Body(body), **kwargs).save(account.drafts)
                draft = _newest_draft(account, subj)
            else:
                if not to:
                    raise Failure("validation", "'to' is required for a new mail (or pass reply_to)")
                draft = Message(account=account, folder=account.drafts, subject=subject or "",
                                body=Body(body), to_recipients=mailboxes(to),
                                cc_recipients=mailboxes(cc), bcc_recipients=mailboxes(bcc),
                                importance=importance.capitalize())
                draft.save()
            if draft is None:
                raise Failure("upstream_error", "draft was saved but could not be read back")
            return {"draft_id": ctx.aliases.alias(draft.id, "d", draft.changekey), "folder": "drafts",
                    "to": [m.email_address for m in (draft.to_recipients or [])],
                    "subject": draft.subject, "note": "saved as draft; NOT sent"}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def update_draft(
        draft_id: Annotated[str, Field(description="Draft id (d1).")],
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Change recipients, subject or body of an existing draft."""
        raw, _ = ctx.aliases.resolve(draft_id, expect=("d", "m"))

        def work(account):
            draft = fetch_one(account, raw)
            if not getattr(draft, "is_draft", False):
                raise Failure("validation", "that item is not a draft")
            changed = []
            for name, value in (("to_recipients", mailboxes(to)), ("cc_recipients", mailboxes(cc)),
                                ("bcc_recipients", mailboxes(bcc))):
                if value is not None:
                    setattr(draft, name, value)
                    changed.append(name)
            if subject is not None:
                draft.subject = subject
                changed.append("subject")
            if body is not None:
                draft.body = Body(body)
                changed.append("body")
            if not changed:
                raise Failure("validation", "nothing to update")
            draft.save(update_fields=changed)
            return {"draft_id": draft_id, "updated_fields": changed}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def delete_draft(draft_id: Annotated[str, Field(description="Draft id (d1).")]) -> dict[str, Any]:
        """Move a draft to Deleted Items (recoverable)."""
        raw, _ = ctx.aliases.resolve(draft_id, expect=("d", "m"))

        def work(account):
            draft = fetch_one(account, raw)
            if not getattr(draft, "is_draft", False):
                raise Failure("validation", "that item is not a draft; use delete_messages for mail")
            draft.move_to_trash()
            return {"deleted": True, "draft_id": draft_id, "recoverable": True}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def update_messages(
        ids: Annotated[list[str], Field(description="Up to 50 message ids.")],
        set_read: bool | None = None,
        categories_add: list[str] | None = None,
        categories_remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mark messages read/unread and/or add/remove categories (bulk, max 50)."""
        if set_read is None and not categories_add and not categories_remove:
            raise Failure("validation", "pass set_read and/or categories_add/categories_remove")
        refs = resolve_many(ctx.aliases, ids, ("m", "d"))

        def work(account):
            done, failed = 0, []
            for ref, raw in refs:
                try:
                    item = fetch_one(account, raw)
                    changed = []
                    if set_read is not None:
                        item.is_read = set_read
                        changed.append("is_read")
                    if categories_add or categories_remove:
                        cats = [c for c in (item.categories or []) if c not in set(categories_remove or [])]
                        cats += [c for c in (categories_add or []) if c not in cats]
                        item.categories = cats or None
                        changed.append("categories")
                    item.save(update_fields=changed)
                    done += 1
                except Exception as e:
                    failed.append({"id": ref, "error": f"{type(e).__name__}: {e}"[:160]})
            return {"updated": done, "failed": failed}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def move_messages(
        ids: Annotated[list[str], Field(description="Up to 50 message ids.")],
        to_folder: Annotated[str, Field(description="Target folder: well-known name, f-alias or path.")],
    ) -> dict[str, Any]:
        """Move messages to another folder (ids keep working: aliases are rebound)."""
        refs = resolve_many(ctx.aliases, ids, ("m", "d"))

        def work(account):
            target = resolve_folder(account, to_folder, ctx.aliases)
            moved, failed = [], []
            for ref, raw in refs:
                try:
                    item = fetch_one(account, raw)
                    item.move(target)  # updates item.id in place
                    if ref[0] in "md" and ref[1:].isdigit():
                        ctx.aliases.rebind(ref, item.id, item.changekey)
                    moved.append(ref)
                except Exception as e:
                    failed.append({"id": ref, "error": f"{type(e).__name__}: {e}"[:160]})
            return {"moved": moved, "failed": failed, "folder": target.name}
        return await ctx.gateway.call(work)

    # ------------------------------------------------------------------ full tier

    @r.tool("full", "send")
    async def send_draft(draft_id: Annotated[str, Field(description="Draft id (d1).")]) -> dict[str, Any]:
        """SEND an existing draft (the only way mail leaves the mailbox). Refused while
        SEND_ENABLED=false. There is no confirmation step: only call this after the user
        approved the draft."""
        raw, _ = ctx.aliases.resolve(draft_id, expect=("d", "m"))

        def work(account):
            draft = fetch_one(account, raw)
            if not getattr(draft, "is_draft", False):
                raise Failure("validation", "that item is not a draft")
            rcpts = [m.email_address for m in (draft.to_recipients or []) + (draft.cc_recipients or [])
                     + (draft.bcc_recipients or [])]
            if not rcpts:
                raise Failure("validation", "the draft has no recipients")
            draft.send(save_copy=True)
            return {"sent": True, "recipients": rcpts, "subject": draft.subject}
        return await ctx.gateway.call(work)

    @r.tool("full", "destructive")
    async def delete_messages(
        ids: Annotated[list[str], Field(description="Up to 50 message ids.")],
        permanent: Annotated[bool, Field(description="true = hard delete (NOT recoverable). Default moves to Deleted Items.")] = False,
    ) -> dict[str, Any]:
        """Delete messages. Default: move to Deleted Items (recoverable). permanent=true
        deletes for good. There is no confirmation step."""
        refs = resolve_many(ctx.aliases, ids, ("m", "d"))

        def work(account):
            done, failed = [], []
            for ref, raw in refs:
                try:
                    item = fetch_one(account, raw)
                    if permanent:
                        item.delete()
                    else:
                        item.move_to_trash()
                    done.append(ref)
                except Exception as e:
                    failed.append({"id": ref, "error": f"{type(e).__name__}: {e}"[:160]})
            return {"deleted": done, "failed": failed, "recoverable": not permanent}
        return await ctx.gateway.call(work)


_SUBJ_PREFIX = re.compile(r"^\s*((re|aw|fw|fwd|wg)\s*:\s*)+", re.IGNORECASE)


def _norm_subject(subject: str | None) -> str:
    return _SUBJ_PREFIX.sub("", subject or "").strip().lower()


def _newest_draft(account: Any, subject: str) -> Any:
    """ReplyToItem.save() does not hand back the created item: read it back."""
    qs = account.drafts.filter(subject=subject).order_by("-datetime_created")
    return next(iter(qs.only("id", "changekey", "subject", "to_recipients")[:1]), None)
