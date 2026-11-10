"""Compact result cards. The model sees short aliases, never raw EWS ids, and
cleaned plain-text bodies instead of Outlook HTML."""

import html
import re
from typing import Any

from .aliases import AliasStore
from .dates import fmt

SNIPPET_CHARS = 200

# Lines that start a quoted reply ("On … wrote:", Outlook header blocks, "-----Original Message-----").
_QUOTE_START = re.compile(
    r"^(\s*>|-{2,}\s*original message\s*-{2,}|\s*on .{3,200} wrote:\s*$|"
    r"\s*from:\s.+$|\s*_{8,}\s*$|\s*am .{3,200} schrieb .*:\s*$|\s*von:\s.+$)",
    re.IGNORECASE,
)
_SIG_START = re.compile(r"^(--\s*$|sent from my |get outlook for )", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_STYLE = re.compile(r"<(style|script|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL)


def html_to_text(raw: str) -> str:
    text = _STYLE.sub("", raw or "")
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_body(text: str, max_chars: int = 4000) -> dict[str, Any]:
    """Keep the newest reply only: cut at the first quote header or signature."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    quoted = False
    for line in lines:
        if _QUOTE_START.match(line) and kept:
            quoted = True
            break
        if _SIG_START.match(line) and kept:
            break
        kept.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    truncated = len(out) > max_chars
    return {"text": out[:max_chars], "quoted_history_stripped": quoted, "truncated": truncated}


def body_text(item: Any) -> str:
    text = getattr(item, "text_body", None) or ""
    if not text:
        body = getattr(item, "body", None)
        if body:
            text = html_to_text(str(body)) if "<" in str(body) else str(body)
    return text


def addr(mailbox: Any) -> str:
    if mailbox is None:
        return ""
    name = getattr(mailbox, "name", None)
    email = getattr(mailbox, "email_address", None) or ""
    return f"{name} <{email}>" if name and name != email else email


def emails(recipients: Any) -> list[str]:
    return [r.email_address for r in (recipients or []) if getattr(r, "email_address", None)]


def message_date(item: Any) -> Any:
    """datetime_received, falling back to datetime_sent (grommunio omits the former)."""
    return (getattr(item, "datetime_received", None)
            or getattr(item, "datetime_sent", None)
            or getattr(item, "datetime_created", None))


def msg_card(item: Any, aliases: AliasStore, tz: str, prefix: str = "m") -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": aliases.alias(str(item.id), prefix, getattr(item, "changekey", None)),
        "from": addr(getattr(item, "sender", None) or getattr(item, "author", None)),
        "subject": getattr(item, "subject", None) or "",
        "date": fmt(message_date(item), tz),
        "snippet": clean_body(body_text(item), SNIPPET_CHARS)["text"],
    }
    conv = getattr(item, "conversation_id", None)
    if conv is not None and getattr(conv, "id", None):
        card["thread"] = aliases.alias(conv.id, "t")
    if getattr(item, "is_read", True) is False:
        card["unread"] = True
    if getattr(item, "has_attachments", False):
        card["attach"] = True
    if str(getattr(item, "importance", "") or "").lower() == "high":
        card["importance"] = "high"
    cats = getattr(item, "categories", None)
    if cats:
        card["categories"] = list(cats)
    return card


def msg_full(item: Any, aliases: AliasStore, tz: str, max_chars: int = 4000,
             prefix: str = "m") -> dict[str, Any]:
    full = msg_card(item, aliases, tz, prefix)
    full.pop("snippet", None)
    full["to"] = emails(getattr(item, "to_recipients", None))
    cc = emails(getattr(item, "cc_recipients", None))
    if cc:
        full["cc"] = cc
    cleaned = clean_body(body_text(item), max_chars)
    full["body"] = cleaned["text"]
    if cleaned["quoted_history_stripped"]:
        full["quoted_history"] = "stripped; use get_thread for the whole conversation"
    if cleaned["truncated"]:
        full["body_truncated"] = True
    files, inline = attachment_lists(item)
    if files:
        full["attachments"] = files
    if inline:
        full["inline_images"] = inline
    return full


def is_inline(att: Any, html: str) -> bool:
    """Embedded in the body (signature logo, pasted screenshot) rather than attached.
    Some clients send such images without IsInline, so a cid: reference counts too.
    `html` is the body, lowercased."""
    if getattr(att, "is_inline", False):
        return True
    cid = (getattr(att, "content_id", None) or "").strip().strip("<>").lower()
    return bool(cid) and f"cid:{cid}" in html


def attachment_lists(item: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(real attachments, inline images). `index` is the position among ALL attachments,
    which is what get_attachment takes."""
    html = str(getattr(item, "body", None) or "").lower()
    files: list[dict[str, Any]] = []
    inline: list[dict[str, Any]] = []
    for i, a in enumerate(getattr(item, "attachments", None) or []):
        entry = {"index": i, "name": getattr(a, "name", None) or f"attachment-{i}",
                 "size_bytes": getattr(a, "size", None), "content_type": getattr(a, "content_type", None)}
        (inline if is_inline(a, html) else files).append(entry)
    return files, inline


def event_card(item: Any, aliases: AliasStore, tz: str) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": aliases.alias(str(item.id), "e", getattr(item, "changekey", None)),
        "subject": getattr(item, "subject", None) or "",
        "start": fmt(getattr(item, "start", None), tz),
        "end": fmt(getattr(item, "end", None), tz),
    }
    if getattr(item, "is_all_day", False):
        card["all_day"] = True
    loc = getattr(item, "location", None)
    if loc:
        card["location"] = str(loc)
    org = getattr(item, "organizer", None)
    if org is not None:
        card["organizer"] = addr(org)
    resp = getattr(item, "my_response_type", None)
    if resp:
        card["my_response"] = str(resp)
    if getattr(item, "is_recurring", False):
        card["recurring"] = True
    return card


def event_full(item: Any, aliases: AliasStore, tz: str, attendees_available: bool = True
               ) -> dict[str, Any]:
    full = event_card(item, aliases, tz)
    if attendees_available:
        people = []
        for kind, field in (("required", "required_attendees"), ("optional", "optional_attendees"),
                            ("resource", "resources")):
            for a in getattr(item, field, None) or []:
                mb = getattr(a, "mailbox", None)
                people.append({"email": getattr(mb, "email_address", None), "type": kind,
                               "response": str(getattr(a, "response_type", None) or "Unknown")})
        full["attendees"] = people
    else:
        full["attendees"] = None
        full["attendees_note"] = "this server could not return attendee data for the event"
    full["body"] = clean_body(body_text(item), 2000)["text"]
    return full


def task_card(item: Any, aliases: AliasStore, tz: str) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": aliases.alias(str(item.id), "k", getattr(item, "changekey", None)),
        "subject": getattr(item, "subject", None) or "",
        "complete": bool(getattr(item, "is_complete", False)),
        "status": str(getattr(item, "status", "") or ""),
    }
    due = getattr(item, "due_date", None)
    if due is not None:
        card["due"] = due.isoformat()[:10] if hasattr(due, "isoformat") else str(due)
    return card


def page(items: list[dict[str, Any]], offset: int, has_more: bool) -> dict[str, Any]:
    return {"items": items, "count": len(items),
            "next_offset": offset + len(items) if has_more else None}
