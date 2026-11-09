"""Helpers shared by the tool modules (all run on worker threads)."""

from itertools import islice
from typing import Any

from exchangelib import CalendarItem, Mailbox
from exchangelib.properties import ItemId

from ..aliases import AliasStore, is_alias
from ..errors import Failure, classify

MAX_LIMIT = 100
MAX_BULK = 50
ATTENDEE_FIELDS = ("required_attendees", "optional_attendees", "resources")

WELL_KNOWN = {
    "inbox": "inbox", "sent": "sent", "sent items": "sent", "drafts": "drafts",
    "trash": "trash", "deleted": "trash", "deleted items": "trash", "junk": "junk",
    "calendar": "calendar", "contacts": "contacts", "tasks": "tasks", "outbox": "outbox",
}


def check_page(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0:
        raise Failure("validation", "offset must be >= 0")
    if not 1 <= limit <= MAX_LIMIT:
        raise Failure("validation", f"limit must be between 1 and {MAX_LIMIT}")
    return offset, limit


def take(iterable, offset: int, limit: int) -> tuple[list[Any], bool]:
    """Slice offset..offset+limit with one item of look-ahead."""
    rows = list(islice(iterable, offset, offset + limit + 1))
    return rows[:limit], len(rows) > limit


def resolve_folder(account: Any, ref: str, aliases: AliasStore) -> Any:
    """Well-known name ('inbox', 'f:inbox'), folder alias (f3), path ('Inbox/Sub') or raw id."""
    key = (ref or "inbox").strip()
    low = key.lower().removeprefix("f:")
    if low in WELL_KNOWN:
        return getattr(account, WELL_KNOWN[low])
    if is_alias(key):
        raw, _ = aliases.resolve(key, expect=("f",))
        return _folder_by_id(account, raw)
    if "/" in key or len(key) < 40:  # a path; raw EWS folder ids are much longer
        node = account.msg_folder_root
        for part in [p for p in key.split("/") if p]:
            match = next((c for c in node.children if (c.name or "").lower() == part.lower()), None)
            if match is None:
                raise Failure("not_found", f"No folder matches '{key}'.",
                              "Use list_folders and pass one of its ids or paths.")
            node = match
        return node
    return _folder_by_id(account, key)


def _folder_by_id(account: Any, raw: str) -> Any:
    for f in account.msg_folder_root.walk():
        if f.id == raw:
            return f
    raise Failure("not_found", "Folder id not found.", "Use list_folders to get fresh ids.")


def fetch_one(account: Any, raw_id: str, only: list[str] | None = None, folder: Any = None) -> Any:
    kwargs: dict[str, Any] = {}
    if only:
        kwargs["only_fields"] = only
        kwargs["folder"] = folder if folder is not None else account.inbox
    res = list(account.fetch(ids=[ItemId(id=raw_id)], **kwargs))
    if not res or res[0] is None:
        raise Failure("not_found", "Item not found.", "Re-run the list/search tool for a fresh id.")
    if isinstance(res[0], Exception):
        raise classify(res[0])
    return res[0]


def is_property_failure(exc: BaseException) -> bool:
    return type(exc).__name__ == "ErrorItemPropertyRequestFailed" or "E-3127" in str(exc)


def event_fields_without_attendees(account: Any) -> list[str]:
    return [f.name for f in CalendarItem.FIELDS
            if f.supports_version(account.version) and f.name not in ATTENDEE_FIELDS]


def fetch_event(account: Any, raw_id: str) -> tuple[Any, bool]:
    """Fetch a calendar item in full. Some EWS servers (grommunio, E-3127) fail
    on attendee properties of plain appointments; retry without them.
    Returns (item, attendees_available)."""
    try:
        return fetch_one(account, raw_id), True
    except Failure as f:
        if not is_property_failure(f):
            raise
    except Exception as e:
        if not is_property_failure(e):
            raise
    item = fetch_one(account, raw_id, only=event_fields_without_attendees(account),
                     folder=account.calendar)
    return item, False


def mailboxes(addrs: list[str] | None) -> list[Mailbox] | None:
    if not addrs:
        return None
    out = []
    for a in addrs:
        a = (a or "").strip()
        if "@" not in a:
            raise Failure("validation", f"not an email address: {a!r}")
        out.append(Mailbox(email_address=a))
    return out


def resolve_many(aliases: AliasStore, ids: list[str], expect: tuple[str, ...]) -> list[tuple[str, str]]:
    if not ids:
        raise Failure("validation", "ids must not be empty")
    if len(ids) > MAX_BULK:
        raise Failure("validation", f"at most {MAX_BULK} ids per call")
    return [(ref, aliases.resolve(ref, expect=expect)[0]) for ref in ids]
