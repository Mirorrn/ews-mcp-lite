"""exchangelib 5.0.3 compatibility shim.

``EWSDateTime.from_string`` in 5.0.3 parses UTC values only as
``YYYY-MM-DDTHH:MM:SSZ``. Servers that send fractional seconds
(grommunio: ``2026-11-09T08:28:05.198803600Z``, 7 digits) make every such
field come back as ``None`` (DateTimeReceived, LastModifiedTime, OOF windows).
This patch accepts any number of fractional digits; plain ``…SSZ`` values and
offset-style values keep the original code path.

MAPI properties registered on Message so restrictions can use them. Full fetches
must leave them out (``fields_without_mapi_extras``), or the body is downloaded
twice and every item carries a few unused properties:

- ``pr_body`` (PR_BODY, 0x1000): the plain-text body, for servers that reject
  Contains on ``item:Body`` (grommunio, E-3224).
- ``pr_conversation_id`` (PR_CONVERSATION_ID, 0x3013): grommunio answers a
  restriction on ``item:ConversationId`` with zero items and no error, but filters
  correctly on the MAPI property. Its EWS ConversationId is those 16 bytes plus
  one trailing byte, base64-encoded.
- ``pr_sender_name`` / ``pr_sender_smtp`` / ``pr_sender_addr`` (PR_SENDER_NAME
  0x0C1A, PR_SENDER_SMTP_ADDRESS 0x5D01, PR_SENDER_EMAIL_ADDRESS 0x0C1F) and
  ``pr_from_name`` / ``pr_from_smtp`` (PR_SENT_REPRESENTING_NAME 0x0042,
  PR_SENT_REPRESENTING_SMTP_ADDRESS 0x5D02, what Outlook shows as From for
  delegate mail and what EWS ``From`` writes): EWS has no substring restriction on
  ``message:Sender``, so a search by sender *name* would otherwise have to scan
  items client-side.
"""

import base64
import datetime
import re

from exchangelib import UTC, ExtendedProperty, Message
from exchangelib.ewsdatetime import EWSDateTime

_FRACTIONAL_UTC = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)Z$")
_original = EWSDateTime.from_string.__func__
_applied = False
PR_BODY = "pr_body"
PR_CONVERSATION_ID = "pr_conversation_id"
SENDER_PROPS = ("pr_sender_name", "pr_sender_smtp", "pr_sender_addr", "pr_from_name", "pr_from_smtp")


def _prop(tag: int, ptype: str) -> type[ExtendedProperty]:
    return type(f"Mapi{tag:04X}", (ExtendedProperty,), {"property_tag": tag, "property_type": ptype})


EXTRAS = {
    PR_BODY: _prop(0x1000, "String"),
    PR_CONVERSATION_ID: _prop(0x3013, "Binary"),
    "pr_sender_name": _prop(0x0C1A, "String"),
    "pr_sender_smtp": _prop(0x5D01, "String"),
    "pr_sender_addr": _prop(0x0C1F, "String"),
    "pr_from_name": _prop(0x0042, "String"),
    "pr_from_smtp": _prop(0x5D02, "String"),
}


def mapi_conversation_id(ews_id: str) -> bytes | None:
    """EWS ConversationId -> PR_CONVERSATION_ID as grommunio encodes it, else None."""
    try:
        raw = base64.b64decode(ews_id, validate=True)
    except ValueError:
        return None
    return raw[:16] if len(raw) == 17 else None


def _from_string(cls, date_string):
    m = _FRACTIONAL_UTC.match(date_string or "")
    if m:
        base = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        micros = int((m.group(2) + "000000")[:6])
        return cls.from_datetime(base.replace(microsecond=micros, tzinfo=UTC))
    return _original(cls, date_string)


def apply() -> None:
    global _applied
    if not _applied:
        EWSDateTime.from_string = classmethod(_from_string)
        _applied = True
    registered = {f.name for f in Message.FIELDS}
    for name, cls in EXTRAS.items():
        if name not in registered:  # register() raises on a second call
            Message.register(name, cls)


def fields_without_mapi_extras(fields) -> list:
    return [f for f in fields if f.name not in EXTRAS]
