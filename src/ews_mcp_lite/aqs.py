"""AQS (EWS ``QueryString``) support for search_messages.

A QueryString search runs against the Exchange search index (fast on big folders)
but EWS does not allow it together with a ``Restriction``, so every filter that
must stay server-side is written into the AQS string itself. Date bounds are not:
AQS dates are day-granular and parsed in the server's culture (``01/02/2026`` is
ambiguous), so search_messages applies since/until to the index hits instead.

grommunio/gromox parses no ``QueryString`` in FindItem and silently returns the
unfiltered folder, while reporting Exchange 2013 (build 15.0.847.4040). Which path
runs is therefore set by configuration (``EWS_SERVER_TYPE``), never guessed.
"""

import re

AQS_HIT_CAP = 250  # EWSFindCountLimit for AQS searches on Exchange 2013+ / Exchange Online
_QUOTES = re.compile(r'["“”„«»]')


def phrase(text: str | None, prefix: bool = True) -> str | None:
    """User text -> one quoted AQS phrase, or None if nothing is left.

    Quoting keeps AQS syntax in user input literal (AND/OR/NOT, ``from:``,
    parentheses, ``-``). The words must appear in this order; with ``prefix`` each
    may be the start of a word (``"q4 budg"*`` matches 'Q4 budget planning')."""
    words = _QUOTES.sub(" ", text or "").split()
    if not words:
        return None
    return f'"{" ".join(words)}"' + ("*" if prefix else "")


def build_query(text: str, *, subject: str | None = None, exact_sender: str | None = None,
                unread_only: bool = False, has_attachments: bool | None = None) -> str:
    """Free text plus the filters AQS can express exactly, joined with AND."""
    parts = [phrase(text)]
    if subject and (p := phrase(subject)):
        parts.append(f"subject:{p}")
    if exact_sender and (p := phrase(exact_sender, prefix=False)):
        parts.append(f"from:{p}")
    if unread_only:
        parts.append("isread:false")
    if has_attachments is not None:
        parts.append(f"hasattachment:{'true' if has_attachments else 'false'}")
    return " AND ".join(p for p in parts if p)

