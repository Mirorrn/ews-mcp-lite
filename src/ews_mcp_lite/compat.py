"""exchangelib 5.0.3 compatibility shim.

``EWSDateTime.from_string`` in 5.0.3 parses UTC values only as
``YYYY-MM-DDTHH:MM:SSZ``. Servers that send fractional seconds
(grommunio: ``2026-11-09T08:28:05.198803600Z``, 7 digits) make every such
field come back as ``None`` (DateTimeReceived, LastModifiedTime, OOF windows).
This patch accepts any number of fractional digits; plain ``…SSZ`` values and
offset-style values keep the original code path.
"""

import datetime
import re

from exchangelib import UTC
from exchangelib.ewsdatetime import EWSDateTime

_FRACTIONAL_UTC = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)Z$")
_original = EWSDateTime.from_string.__func__
_applied = False


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
