"""Date parsing that always yields exchangelib-native aware datetimes.

exchangelib needs ``EWSDateTime`` in an ``EWSTimeZone`` for some services
(GetUserAvailability reads ``tzinfo.ms_id``); a plain ``zoneinfo`` datetime
crashes there. Everything here returns EWS types.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from exchangelib import EWSDateTime, EWSTimeZone

_REL = re.compile(r"^([+-]\d+)([dh])$")


def ews_tz(name: str) -> EWSTimeZone:
    return EWSTimeZone(name)


def to_ews(dt: datetime, tz_name: str) -> EWSDateTime:
    """Any datetime (naive, zoneinfo-aware or EWS) -> EWSDateTime in an EWSTimeZone."""
    tz = ews_tz(tz_name)
    zone = ZoneInfo(tz_name)
    aware = dt.replace(tzinfo=zone) if dt.tzinfo is None else dt
    local = datetime.fromtimestamp(aware.timestamp(), zone)
    return EWSDateTime(local.year, local.month, local.day, local.hour, local.minute,
                       local.second, local.microsecond, tzinfo=tz)


def parse_when(value: str, field: str, tz_name: str) -> EWSDateTime:
    """'now', 'today', 'tomorrow', '+3d', '-2h', YYYY-MM-DD or ISO datetime."""
    tz = ews_tz(tz_name)
    v = (value or "").strip().lower()
    now = EWSDateTime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v == "now":
        return now
    if v == "today":
        return midnight
    if v == "tomorrow":
        return midnight + timedelta(days=1)
    m = _REL.match(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return midnight + timedelta(days=n) if unit == "d" else now + timedelta(hours=n)
    try:
        if len(v) == 10:
            d = date.fromisoformat(v)
            return to_ews(datetime(d.year, d.month, d.day), tz_name)
        return to_ews(datetime.fromisoformat(value.strip()), tz_name)
    except ValueError:
        raise ValueError(
            f"{field}: cannot parse {value!r}; use 'today', '+Nd', YYYY-MM-DD or an ISO datetime"
        ) from None


def fmt(value, tz_name: str) -> str | None:
    if value is None or not hasattr(value, "astimezone"):
        return None
    try:
        return value.astimezone(ews_tz(tz_name)).isoformat(timespec="minutes")
    except Exception:
        return str(value)
