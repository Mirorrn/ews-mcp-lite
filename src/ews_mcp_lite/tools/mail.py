"""Mail tools: folders, search, read, threads, attachments, drafts, bulk updates."""

import base64
import logging
import re
import time
from datetime import timedelta
from typing import Annotated, Any, Literal

from exchangelib import Body, FileAttachment, Message, Q
from exchangelib.folders import SentItems
from exchangelib.properties import ConversationId
from pydantic import Field

from .. import aqs
from ..compat import PR_CONVERSATION_ID, SENDER_PROPS, fields_without_mapi_extras, mapi_conversation_id
from ..dates import parse_when
from ..dto import body_text, is_inline, message_date, msg_card, msg_full, page
from ..errors import Failure, classify
from ..registry import Registry
from .common import (
    check_page,
    fetch_one,
    mail_folders,
    mailboxes,
    parallel,
    refresh_folders,
    resolve_folder,
    resolve_many,
    take,
)

CARD_FIELDS = ["id", "changekey", "subject", "sender", "datetime_received", "datetime_sent",
               "is_read", "has_attachments", "conversation_id", "importance", "categories",
               "text_body"]
# Client-side sender-name scan, used only when a server rejects the MAPI sender restriction.
SENDER_SCAN_CAP = 2000
# folder='all' merges per-folder results client-side, so deep pages get expensive.
ALL_FOLDERS_WINDOW = 1000
WAITING_SENT_CAP = 1000
WAITING_INBOUND_CAP = 5000  # per folder
# Server answers that mean "this restriction/property is not supported here".
REJECTED = ("ErrorInvalidRestriction", "ErrorUnsupportedPathForQuery", "ErrorInvalidPropertyRequest",
            "ErrorUnsupportedQueryFilter")
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv")
MAX_INLINE_BYTES = 512 * 1024
logger = logging.getLogger(__name__)


def _rejected(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return any(code in text for code in REJECTED)


def _sender_q(term: str) -> Q:
    """Substring match on the sender's or the sent-representing ('From') name and address."""
    first, *rest = (Q(**{f"{prop}__icontains": term}) for prop in SENDER_PROPS)
    for q in rest:
        first |= q
    return first


def _read_capped(att: Any, cap: int) -> bytes | None:
    """Attachment content, or None if it is longer than `cap` (reading stops at cap+1 bytes,
    so an oversized file is never pulled in full)."""
    local = getattr(att, "_content", None)  # already in memory, e.g. on an item built locally
    if local is not None or getattr(att, "attachment_id", None) is None:
        data = local or b""
    else:
        with att.fp as fp:
            data = fp.read(cap + 1)
        att._fp = None  # the stream was cut short: never reuse it
    return None if len(data) > cap else data


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
            refresh_folders(account)  # live counts, and folders created since the last call
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
        query: Annotated[str | None, Field(description=(
            "Free text matched against subject and body. On Exchange this uses the search index "
            "(all words, in order, each as a word prefix: 'q4 budg' finds 'Q4 budget'); on other "
            "servers a substring match. Plain text only, no AQS syntax."))] = None,
        folder: Annotated[str | None, Field(description=(
            "Where to look: 'all' = every mail folder with its subfolders, Sent Items included (not "
            "Deleted Items, Junk, Drafts); or inbox, sent, drafts, trash, an f-alias or a path such as "
            "'Inbox/Projects'. Default: 'all' when query, subject or sender is given, else 'inbox'."))] = None,
        sender: Annotated[str | None, Field(
            description="Sender: an exact email address, or part of a name or address ('lindqvist').")] = None,
        subject: Annotated[str | None, Field(
            description="Subject substring (word-prefix match when `query` uses the search index).")] = None,
        since: Annotated[str | None, Field(description="'today', '-7d', YYYY-MM-DD or ISO datetime.")] = None,
        until: Annotated[str | None, Field(description="'today', '+1d', YYYY-MM-DD or ISO datetime.")] = None,
        unread_only: bool = False,
        has_attachments: bool | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search mail live, newest first: in one folder, or across the whole mailbox with
        folder='all' (then each card names its folder). Returns compact cards with short ids
        (m1, m2 …) for get_message / get_thread / drafts."""
        check_page(offset, limit)
        text = (query or "").strip() if aqs.phrase(query) else ""
        lo = parse_when(since, "since", ctx.tz) if since else None
        hi = parse_when(until, "until", ctx.tz) if until else None
        exact_sender = sender.strip() if sender and "@" in sender and " " not in sender.strip() else None
        fuzzy = sender.strip().lower() if sender and sender.strip() and not exact_sender else None
        scope_all = (folder or ("all" if (text or subject or sender) else "inbox")).strip().lower() in ("all", "*")
        label = "all" if scope_all else (folder or "inbox")
        if scope_all and offset + limit > ALL_FOLDERS_WINDOW:
            raise Failure("validation", f"folder='all' pages up to offset+limit={ALL_FOLDERS_WINDOW}",
                          "Narrow the search (query, sender, since) or pick one folder.")
        base = Q()
        if subject:
            base &= Q(subject__icontains=subject)
        if lo is not None:
            base &= Q(datetime_received__gte=lo)
        if hi is not None:
            base &= Q(datetime_received__lte=hi)
        if unread_only:
            base &= Q(is_read=False)
        if has_attachments is not None:
            base &= Q(has_attachments=has_attachments)
        if exact_sender:
            base &= Q(sender=exact_sender)

        def scan(qs, fields, with_dates: bool, client_sender: bool, cap: int, need: int):
            """Light scan of at most `cap` rows; stops once `need` hits are found.
            -> (hits, scanned, complete)."""
            hits, scanned, done = [], 0, False
            for m in qs.only(*fields)[0:cap]:  # QuerySet slices page lazily, server-side
                scanned += 1
                if with_dates:
                    when = m.datetime_received or m.datetime_sent
                    if when is None or (hi is not None and when > hi):
                        continue
                    if lo is not None and when < lo:  # newest first: the rest is older
                        done = True
                        break
                if client_sender and fuzzy not in (f"{getattr(m.sender, 'name', '')} "
                                                   f"{getattr(m.sender, 'email_address', '')}").lower():
                    continue
                hits.append(m)
                if len(hits) >= need:
                    done = True
                    break
            return hits, scanned, done or scanned < cap

        def run(account, scope, qs_for, with_dates: bool, client_sender: bool, cap: int):
            """Page through qs_for(folder) over every folder in scope.
            -> (rows, more, scanned, complete, {id: folder path})."""
            if len(scope) == 1 and not with_dates and not client_sender:  # one folder: server-side offset
                rows, more = take(_ordered(qs_for(scope[0][1])).only(*CARD_FIELDS), offset, limit)
                return rows, more, None, True, {}
            # Several folders (EWS cannot sort across them) or client-side filters: read light rows
            # newest first per folder, merge, then fetch full cards for the requested page only.
            need = offset + limit + 1
            multi = len(scope) > 1
            filtered = with_dates or client_sender
            fields = (["id", "changekey"] + (["sender"] if client_sender else [])
                      + (["datetime_received", "datetime_sent"] if with_dates or multi else []))

            def one(entry):
                path, f = entry
                hits, scanned, complete = scan(_ordered(qs_for(f)), fields, with_dates, client_sender,
                                               cap if filtered else need, need)
                return [(m, path) for m in hits], scanned, complete
            results = parallel(one, scope)
            merged = [h for hits, _, _ in results for h in hits]
            if multi:
                merged.sort(key=lambda h: (message_date(h[0]) is not None, message_date(h[0]) or 0), reverse=True)
            hits, more = take(merged, offset, limit)
            rows = [m for m in account.fetch(ids=[(m.id, m.changekey) for m, _ in hits], folder=scope[0][1],
                                             only_fields=CARD_FIELDS)
                    if m is not None and not isinstance(m, Exception)] if hits else []
            scanned = sum(n for _, n, _ in results) if filtered else None
            return rows, more, scanned, all(c for _, _, c in results), {m.id: path for m, path in hits}

        def by_restriction(account, scope, notes: list[str], info: dict[str, Any]):
            info["mode"] = "restriction"
            # A sender name goes server-side through the MAPI sender properties; if the server
            # rejects that, it is matched client-side on a light scan instead.
            modes = ("server", "client") if fuzzy else ("none",)
            for i, sender_mode in enumerate(modes):
                attempt: list[str] = []
                try:
                    res = restriction_attempt(account, scope, sender_mode, attempt, info)
                except Exception as e:
                    if i + 1 < len(modes) and _rejected(e):
                        logger.warning("search_messages: server rejected a sender-name restriction (%s); "
                                       "matching sender names client-side", type(e).__name__)
                        continue
                    raise
                notes += attempt
                if fuzzy:
                    info["sender"] = sender_mode
                return res
            raise AssertionError("unreachable")

        def restriction_attempt(account, scope, sender_mode: str, notes: list[str], info: dict[str, Any]):
            q0 = base & _sender_q(sender.strip()) if sender_mode == "server" else base
            client = sender_mode == "client"
            if not text:
                qs_for = (lambda f: f.all()) if q0.is_empty() else (lambda f: f.filter(q0))
                res = run(account, scope, qs_for, False, client, SENDER_SCAN_CAP)
            else:
                # Body properties as configured for the server type (Exchange: item:Body,
                # then PR_BODY; grommunio: PR_BODY only), then subjects only. A rejected
                # property means EWS_SERVER_TYPE does not match the server: log it.
                stages = (*ctx.settings.body_fields, None)
                for body in stages:
                    q = Q(subject__icontains=text)
                    if body:
                        q |= Q(**{f"{body}__icontains": text})
                    try:
                        res = run(account, scope, lambda f, q=q: f.filter(q0 & q), False, client, SENDER_SCAN_CAP)
                        break
                    except Exception as e:
                        if not body or not _rejected(e):
                            raise
                        logger.warning("search_messages: server rejected a body search on %s (%s); "
                                       "trying %s. Check EWS_SERVER_TYPE=%s", body, type(e).__name__,
                                       stages[stages.index(body) + 1] or "subjects only",
                                       ctx.settings.ews_server_type)
                info["body_field"] = body or "none"
                if not body:
                    notes.append("this server cannot search message bodies; matched subjects only")
            info["scanned"] = res[2]
            if not res[3]:
                per = " per folder" if len(scope) > 1 else ""
                notes.append(f"sender name matched client-side within the newest {SENDER_SCAN_CAP} messages"
                             f"{per} only; results may be incomplete - pass an exact email address for a "
                             "full server-side search")
            return res

        def by_index(account, scope, notes: list[str], info: dict[str, Any]):
            info["mode"] = "aqs"
            qstr = aqs.build_query(text, subject=subject, exact_sender=exact_sender,
                                   unread_only=unread_only, has_attachments=has_attachments)
            with_dates = lo is not None or hi is not None
            logger.debug("search_messages: AQS query string %r", qstr)
            res = run(account, scope, lambda f: f.filter(Q(qstr)), with_dates, bool(fuzzy), aqs.AQS_HIT_CAP)
            rows, more, scanned, complete, _ = res
            info["scanned"] = scanned
            per = " per folder" if len(scope) > 1 else ""
            notes.append(f"matched via the Exchange search index (word-prefix match, at most "
                         f"{aqs.AQS_HIT_CAP} hits per search{per})")
            client = " and ".join(w for w, on in (("sender name", fuzzy), ("date range", with_dates)) if on)
            if client and not complete:
                notes.append(f"{client} applied to the newest {aqs.AQS_HIT_CAP} index hits{per} only; "
                             "results may be incomplete - make the query more specific")
            elif not client and not more and offset + len(rows) >= aqs.AQS_HIT_CAP:
                notes.append(f"the index returns at most {aqs.AQS_HIT_CAP} hits; later matches are "
                             "cut off - make the query more specific")
            return res

        filters = [name for name, on in (("text", text), ("subject", subject), ("since", lo is not None),
                                         ("until", hi is not None), ("unread", unread_only),
                                         ("attachments", has_attachments is not None),
                                         ("sender_exact", exact_sender), ("sender_name", fuzzy)) if on]

        def work(account):
            started = time.monotonic()
            if scope_all:
                scope = mail_folders(account) or [("Inbox", account.inbox)]
            else:
                scope = [(folder or "inbox", resolve_folder(account, folder or "inbox", ctx.aliases))]
            notes: list[str] = []
            info: dict[str, Any] = {}
            if text and ctx.settings.search_mode == "aqs":
                try:
                    res = by_index(account, scope, notes, info)
                except Exception as e:
                    if classify(e).code in ("auth_failed", "upstream_unavailable"):
                        raise
                    logger.warning("search_messages: AQS search failed (%s: %s); falling back to "
                                   "restrictions", type(e).__name__, str(e)[:200])
                    notes = [f"index search failed ({type(e).__name__}); used a substring search instead"]
                    res = by_restriction(account, scope, notes, info)
                    info["fallback"] = "aqs_failed"
            else:
                res = by_restriction(account, scope, notes, info)
            rows, more, _, _, where = res
            cards = []
            for m in rows:
                card = msg_card(m, ctx.aliases, ctx.tz)
                if scope_all:
                    card["folder"] = where.get(m.id, scope[0][0])
                cards.append(card)
            out = page(cards, offset, more)
            if len(scope) > 1:
                notes.insert(0, f"searched {len(scope)} mail folders")
            if notes:
                out["note"] = "; ".join(notes)
            # Search terms and addresses stay out of INFO logs (they are mailbox content).
            logger.info("search_messages folder=%s folders=%d mode=%s body_field=%s sender=%s filters=%s "
                        "hits=%d more=%s scanned=%s offset=%d limit=%d took_ms=%d%s", label, len(scope),
                        info.get("mode"), info.get("body_field", "-"), info.get("sender", "-"),
                        ",".join(filters) or "-", len(rows), more,
                        info.get("scanned") if info.get("scanned") is not None else "-", offset, limit,
                        (time.monotonic() - started) * 1000,
                        f" fallback={info['fallback']}" if "fallback" in info else "")
            if any("incomplete" in n or "cut off" in n for n in notes):
                logger.warning("search_messages: result capped (folder=%s mode=%s): %s", label,
                               info.get("mode"), "; ".join(n for n in notes if "incomplete" in n or "cut off" in n))
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
                thread_fields = [f.name for f in fields_without_mapi_extras(Message.supported_fields(account.version))]
                # grommunio returns nothing for item:ConversationId; it filters on the MAPI property.
                mapi = (mapi_conversation_id(conv_raw)
                        if ctx.settings.ews_server_type == "grommunio" else None)
                match = ({PR_CONVERSATION_ID: mapi} if mapi
                         else {"conversation_id": ConversationId(id=conv_raw)})
                try:
                    for f in (account.inbox, account.sent):
                        items += list(f.filter(**match).only(*thread_fields)[:limit])
                except Exception:
                    items, note = [], "this server cannot filter by conversation; showing the message only"
            if seed is not None:  # it may live in a subfolder that the conversation lookup skips
                items.append(seed)
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
        attachment: Annotated[str | None, Field(description=(
            "Attachment name or index (from get_message, also for inline_images); "
            "default: the first real attachment, never an inline image."))] = None,
    ) -> dict[str, Any]:
        """Read one attachment. Text-like files come back as text, others as base64
        (up to 512 KiB); bigger files return metadata only and are not downloaded."""
        raw, _ = ctx.aliases.resolve(message_id, expect=("m", "d"))

        def work(account):
            item = fetch_one(account, raw)
            atts = list(getattr(item, "attachments", None) or [])
            html = str(getattr(item, "body", None) or "").lower()
            if not atts:
                raise Failure("not_found", "This message has no attachments.")
            if attachment is None:
                files = [a for a in atts if not is_inline(a, html)]
                if not files:
                    raise Failure("not_found", f"This message has no attachments, only {len(atts)} inline "
                                  "image(s) embedded in the body.",
                                  "Pass attachment=<index> from get_message's inline_images to read one.")
                chosen = files[0]
            elif attachment.isdigit() and int(attachment) < len(atts):
                chosen = atts[int(attachment)]
            else:
                chosen = next((a for a in atts if (a.name or "") == attachment), None)
                if chosen is None:
                    raise Failure("not_found", f"No attachment named {attachment!r}.",
                                  "Call get_message to see the attachment list.")
            size = getattr(chosen, "size", None)
            meta = {"name": chosen.name, "size_bytes": size, "content_type": getattr(chosen, "content_type", None)}
            if is_inline(chosen, html):
                meta["inline"] = True
            if not isinstance(chosen, FileAttachment):
                return {**meta, "mode": "metadata", "note": "item attachment (a message or event); not readable here"}
            # The size is part of the message metadata: refuse big files before any download.
            if size is not None and size > MAX_INLINE_BYTES:
                return {**meta, "mode": "metadata", "note": f"too large to return (limit {MAX_INLINE_BYTES} bytes); "
                                                             "not downloaded"}
            content = _read_capped(chosen, MAX_INLINE_BYTES)
            if content is None:  # the server sent no size: the capped read stopped the download
                return {**meta, "mode": "metadata", "note": f"too large to return (more than {MAX_INLINE_BYTES} "
                                                             "bytes); download stopped"}
            ctype = (meta["content_type"] or "").lower()
            if ctype.startswith(TEXT_TYPES) or (chosen.name or "").lower().endswith((".txt", ".csv", ".md", ".json")):
                return {**meta, "mode": "text", "text": content.decode("utf-8", "replace")}
            return {**meta, "mode": "base64", "base64": base64.b64encode(content).decode()}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def waiting_on(
        days: Annotated[int, Field(ge=1, le=60, description="Only sent mail older than this many days.")] = 3,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        lookback_days: Annotated[int, Field(ge=1, le=90)] = 30,
        to: Annotated[str | None, Field(
            description="Only mail sent to this person: part of a name or address ('sandoval').")] = None,
    ) -> dict[str, Any]:
        """Sent messages with no reply after `days` days ('what am I waiting on?', 'did X get back
        to me?'). Live scan of up to 1000 sent messages; replies are looked for in every mail folder."""
        needle = (to or "").strip().lower()

        def addressed_to(item) -> bool:
            people = list(item.to_recipients or []) + list(item.cc_recipients or [])
            return any(needle in f"{p.name or ''} {p.email_address or ''}".lower() for p in people)

        def work(account):
            tz = ctx.tz
            now = parse_when("now", "now", tz)
            start = now - timedelta(days=lookback_days)
            # Recipient fields are costly to read (resolved mailboxes): only when filtering by `to`.
            fields = ["id", "changekey", "subject", "datetime_sent", "conversation_id"]
            sent = (account.sent.filter(datetime_sent__gte=start, datetime_sent__lte=now - timedelta(days=days))
                    .order_by("-datetime_sent")
                    .only(*fields, *(["to_recipients", "cc_recipients"] if needle else [])))
            sent_items = list(sent[:WAITING_SENT_CAP])
            candidates = [s for s in sent_items if addressed_to(s)] if needle else sent_items
            # Replies may have been filed anywhere: scan every mail folder except Sent Items.
            folders = mail_folders(account, skip=(SentItems,)) or [("Inbox", account.inbox)]
            # My own mail filed in a folder is not a reply. Excluded server-side by sender
            # address (the sender field itself is costly to read); servers that reject the
            # property get the scan without that exclusion.
            me = (ctx.settings.ews_email or "").strip()
            window = Q(datetime_sent__gte=start)

            def recent(entry, exclude_me: bool = bool(me)):
                _, f = entry
                q = window & ~Q(pr_sender_smtp=me) if exclude_me else window
                try:
                    return list(f.filter(q).only("subject", "conversation_id", "datetime_sent", "datetime_received")
                                [:WAITING_INBOUND_CAP])
                except Exception as e:
                    if not (exclude_me and _rejected(e)):
                        raise
                    logger.warning("waiting_on: server rejected the sender exclusion (%s)", type(e).__name__)
                    return recent(entry, exclude_me=False)
            per_folder = parallel(recent, folders)
            by_conv: dict[str, list[Any]] = {}
            by_subj: dict[str, list[Any]] = {}
            for rows in per_folder:
                for m in rows:
                    cid = getattr(getattr(m, "conversation_id", None), "id", None)
                    if cid:
                        by_conv.setdefault(cid, []).append(m)
                    by_subj.setdefault(_norm_subject(m.subject), []).append(m)
            waiting = []
            for s in candidates:
                when = s.datetime_sent
                cid = getattr(getattr(s, "conversation_id", None), "id", None)
                replies = (by_conv.get(cid, []) if cid else []) + by_subj.get(_norm_subject(s.subject), [])
                if not any((c.datetime_received or c.datetime_sent) and
                           (c.datetime_received or c.datetime_sent) > when for c in replies):
                    waiting.append(s)
                if len(waiting) >= limit:
                    break
            full = {m.id: m for m in account.fetch(ids=[(s.id, s.changekey) for s in waiting], folder=account.sent,
                                                   only_fields=[*CARD_FIELDS, "to_recipients"])
                    if m is not None and not isinstance(m, Exception)} if waiting else {}
            items = []
            for s in waiting:
                item = full.get(s.id, s)
                card = msg_card(item, ctx.aliases, ctx.tz)
                card["to"] = [r.email_address for r in (getattr(item, "to_recipients", None) or [])]
                items.append(card)
            note = (f"scanned {len(sent_items)} sent message(s) from the last {lookback_days} days "
                    f"(cap {WAITING_SENT_CAP})")
            if needle:
                note += f", {len(candidates)} of them to {to.strip()!r}"
            note += f"; replies matched by conversation or subject in {len(folders)} mail folder(s)"
            if len(sent_items) >= WAITING_SENT_CAP:
                note += (f"; WARNING: sent scan hit its cap of {WAITING_SENT_CAP}, older sent "
                         "messages were not checked - lower lookback_days")
            capped = [path for (path, _), rows in zip(folders, per_folder, strict=True) if len(rows) >= WAITING_INBOUND_CAP]
            if capped:
                note += (f"; WARNING: reply scan hit its cap of {WAITING_INBOUND_CAP} in {', '.join(capped)}, "
                         "some replies were not seen, so entries may be falsely listed as waiting")
            return {"items": items, "count": len(items), "note": note}
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

    def _draft_content(account, raw: str) -> tuple[Any, dict[str, Any]]:
        draft = fetch_one(account, raw)
        if not getattr(draft, "is_draft", False):
            raise Failure("validation", "that item is not a draft")

        def addrs(field):
            return sorted((m.email_address or "").lower() for m in (getattr(draft, field, None) or []))
        content = {"subject": draft.subject or "", "to": addrs("to_recipients"),
                   "cc": addrs("cc_recipients"), "bcc": addrs("bcc_recipients"), "body": body_text(draft)}
        return draft, content

    async def resolve_send_draft(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Bind the confirm token to the draft's CONTENT, re-read at both phases."""
        raw, _ = ctx.aliases.resolve(kwargs["draft_id"], expect=("d", "m"))
        _, content = await ctx.gateway.call(lambda account: _draft_content(account, raw))
        recipients = content["to"] + content["cc"] + content["bcc"]
        if not recipients:
            raise Failure("validation", "the draft has no recipients")
        preview = {**{k: content[k] for k in ("subject", "to", "cc", "bcc")},
                   "body_snippet": content["body"][:400], "draft_id": kwargs["draft_id"]}
        return {"preview": preview, "material": content, "recipients": recipients}

    @r.tool("full", "send", resolve=resolve_send_draft)
    async def send_draft(draft_id: Annotated[str, Field(description="Draft id (d1).")]) -> dict[str, Any]:
        """SEND an existing draft (the only way mail leaves the mailbox). Refused while
        SEND_ENABLED=false. The confirm token binds the draft's subject, recipients and body:
        editing the draft after the preview invalidates it."""
        raw, _ = ctx.aliases.resolve(draft_id, expect=("d", "m"))

        def work(account):
            draft, content = _draft_content(account, raw)
            draft.send(save_copy=True)
            return {"sent": True, "recipients": content["to"] + content["cc"] + content["bcc"],
                    "subject": content["subject"]}
        return await ctx.gateway.call(work)

    async def resolve_delete(kwargs: dict[str, Any]) -> dict[str, Any]:
        refs = resolve_many(ctx.aliases, kwargs["ids"], ("m", "d"))  # validates before any preview
        permanent = bool(kwargs.get("permanent", False))
        return {"preview": {"ids": [ref for ref, _ in refs], "count": len(refs), "permanent": permanent,
                            "recoverable": not permanent},
                "material": {"raw_ids": sorted(raw for _, raw in refs), "permanent": permanent},
                "recipients": []}

    @r.tool("full", "destructive", resolve=resolve_delete)
    async def delete_messages(
        ids: Annotated[list[str], Field(description="Up to 50 message ids.")],
        permanent: Annotated[bool, Field(description="true = hard delete (NOT recoverable). Default moves to Deleted Items.")] = False,
    ) -> dict[str, Any]:
        """Delete messages. Default: move to Deleted Items (recoverable). permanent=true
        deletes for good."""
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
