"""Calendar tools: list/get events, free/busy, create/update/respond/cancel."""

from datetime import timedelta
from typing import Annotated, Any, Literal

from exchangelib import Attendee, Body, CalendarItem
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
from pydantic import Field

from ..dates import parse_when, to_ews
from ..dto import event_card, event_full, page
from ..errors import Failure
from ..registry import Context, Registry
from .common import check_page, fetch_event, mailboxes

# Card fields only: no attendee properties, so plain appointments never trip
# servers that fail on them (grommunio E-3127) and the response stays small.
EVENT_CARD_FIELDS = ["id", "changekey", "subject", "start", "end", "location", "organizer",
                     "my_response_type", "is_recurring", "is_all_day"]
_RESPONSES = {"accept": "accept", "tentative": "tentatively_accept", "decline": "decline"}


def list_view(account: Any, ctx: Context, start, end, offset: int, limit: int
              ) -> tuple[list[dict[str, Any]], bool]:
    """CalendarView (expands recurrences) with a card-only projection."""
    if end <= start:
        raise Failure("validation", "end must be after start")
    qs = account.calendar.view(start=start, end=end, max_items=offset + limit + 1)
    items = list(qs.only(*EVENT_CARD_FIELDS))
    items.sort(key=lambda e: e.start)
    rows = items[offset:offset + limit]
    return [event_card(e, ctx.aliases, ctx.tz) for e in rows], len(items) > offset + limit


def free_busy_args(emails: list[str], start, end, tz: str) -> dict[str, Any]:
    """Build get_free_busy_info kwargs. start/end MUST be EWSDateTime in an
    EWSTimeZone: exchangelib reads tzinfo.ms_id (a zoneinfo tz crashes)."""
    return {"accounts": [(e, "Required", False) for e in emails],
            "start": to_ews(start, tz), "end": to_ews(end, tz)}


def _slots(busy: list[tuple[Any, Any]], start, end, minutes: int, work_only: bool, tz: str
           ) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    step = timedelta(minutes=30)
    dur = timedelta(minutes=minutes)
    begin = max(start, parse_when("now", "now", tz))  # never suggest slots in the past
    t = begin.replace(minute=0 if begin.minute == 0 else 30, second=0, microsecond=0)
    while t < begin:
        t += step
    while t + dur <= end and len(out) < 5:
        local = t.astimezone(start.tzinfo)
        in_hours = local.weekday() < 5 and 9 <= local.hour and (local + dur).hour <= 17
        if (not work_only or in_hours) and all(t + dur <= b0 or t >= b1 for b0, b1 in busy):
            out.append({"start": local.isoformat(timespec="minutes"),
                        "end": (local + dur).isoformat(timespec="minutes")})
        t += step
    return out


def register(r: Registry) -> None:
    ctx = r.ctx

    @r.tool("read", "read")
    async def list_events(
        start: Annotated[str, Field(description="'today', '+1d', YYYY-MM-DD or ISO datetime.")] = "today",
        end: Annotated[str, Field(description="Window end; default one week ahead.")] = "+7d",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """List calendar events overlapping a window (recurring series expanded).
        Ids are short aliases (e1 …) for get_event / update_event / respond_to_event."""
        check_page(offset, limit)
        s, e = parse_when(start, "start", ctx.tz), parse_when(end, "end", ctx.tz)

        def work(account):
            cards, more = list_view(account, ctx, s, e, offset, limit)
            return page(cards, offset, more)
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_event(id: Annotated[str, Field(description="Event id (e3) or raw EWS id.")]) -> dict[str, Any]:
        """Fetch one event in full: attendees with their responses, body, organizer."""
        raw, _ = ctx.aliases.resolve(id, expect=("e", "m"))

        def work(account):
            item, attendees_ok = fetch_event(account, raw)
            return {"event": event_full(item, ctx.aliases, ctx.tz, attendees_ok)}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def check_availability(
        attendees: Annotated[list[str], Field(description="Email addresses to check.")],
        start: Annotated[str, Field(description="Window start ('today', '+1d', ISO).")],
        end: Annotated[str, Field(description="Window end.")],
        duration_minutes: Annotated[int, Field(ge=15, le=480)] = 30,
        working_hours_only: bool = True,
    ) -> dict[str, Any]:
        """Free/busy for attendees in a window, plus up to 5 slots where everyone is free."""
        emails = [a.strip() for a in attendees if a and a.strip()]
        if not emails:
            raise Failure("validation", "attendees must list at least one email")
        s, e = parse_when(start, "start", ctx.tz), parse_when(end, "end", ctx.tz)
        if e <= s:
            raise Failure("validation", "end must be after start")

        def work(account):
            views = list(account.protocol.get_free_busy_info(**free_busy_args(emails, s, e, ctx.tz)))
            per: dict[str, Any] = {}
            busy_all: list[tuple[Any, Any]] = []
            for email, view in zip(emails, views, strict=False):
                if isinstance(view, Exception):
                    per[email] = {"error": f"{type(view).__name__}: {view}"[:160]}
                    continue
                busy = [(ev.start, ev.end) for ev in (view.calendar_events or [])
                        if str(ev.busy_type) not in ("Free", "NoData")]
                busy_all += busy
                per[email] = {"busy": [{"start": b0.astimezone(s.tzinfo).isoformat(timespec="minutes"),
                                        "end": b1.astimezone(s.tzinfo).isoformat(timespec="minutes")}
                                       for b0, b1 in busy]}
            return {"attendees": per,
                    "suggested_slots": _slots(busy_all, s, e, duration_minutes, working_hours_only, ctx.tz)}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def create_event(
        subject: str,
        start: Annotated[str, Field(description="ISO datetime or 'tomorrow' etc.")],
        end: Annotated[str, Field(description="ISO datetime; must be after start.")],
        attendees: list[str] | None = None,
        location: str | None = None,
        body: str | None = None,
        send_invitations: Annotated[bool, Field(description="Send invitations to attendees (needs SEND_ENABLED=true).")] = False,
    ) -> dict[str, Any]:
        """Create a calendar event. With attendees and send_invitations=true it becomes
        a meeting request sent to them (refused while SEND_ENABLED=false)."""
        s, e = parse_when(start, "start", ctx.tz), parse_when(end, "end", ctx.tz)
        if e <= s:
            raise Failure("validation", "end must be after start")
        notify = bool(attendees) and send_invitations
        if notify:
            ctx.require_send("sending meeting invitations")

        def work(account):
            item = CalendarItem(account=account, folder=account.calendar, subject=subject, start=s,
                                end=e, location=location, body=Body(body) if body else None)
            if attendees:
                item.required_attendees = [Attendee(mailbox=m, response_type="Unknown")
                                           for m in mailboxes(attendees)]
            item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY if notify else SEND_TO_NONE)
            return {"event_id": ctx.aliases.alias(item.id, "e", item.changekey),
                    "invitations_sent": notify}
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def update_event(
        event_id: Annotated[str, Field(description="Event id (e3).")],
        subject: str | None = None,
        start: str | None = None,
        end: str | None = None,
        location: str | None = None,
        body: str | None = None,
        notify_attendees: Annotated[bool, Field(description="Send updates to attendees (needs SEND_ENABLED=true).")] = False,
    ) -> dict[str, Any]:
        """Change an event's subject, time, location or body."""
        raw, _ = ctx.aliases.resolve(event_id, expect=("e",))
        if notify_attendees:
            ctx.require_send("notifying attendees")

        def work(account):
            item, _ = fetch_event(account, raw)
            changed = []
            if subject is not None:
                item.subject = subject
                changed.append("subject")
            if start is not None:
                item.start = parse_when(start, "start", ctx.tz)
                changed.append("start")
            if end is not None:
                item.end = parse_when(end, "end", ctx.tz)
                changed.append("end")
            if location is not None:
                item.location = location
                changed.append("location")
            if body is not None:
                item.body = Body(body)
                changed.append("body")
            if not changed:
                raise Failure("validation", "nothing to update")
            if item.end <= item.start:
                raise Failure("validation", "end must be after start")
            item.save(update_fields=changed,
                      send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY if notify_attendees else SEND_TO_NONE)
            if event_id[0] == "e" and event_id[1:].isdigit():
                ctx.aliases.rebind(event_id, item.id, item.changekey)
            return {"event_id": event_id, "updated_fields": changed, "attendees_notified": notify_attendees}
        return await ctx.gateway.call(work)

    @r.tool("full", "send")
    async def respond_to_event(
        event_id: Annotated[str, Field(description="Event id (e3) or the meeting-request message id (m5).")],
        response: Literal["accept", "tentative", "decline"],
        message: str | None = None,
    ) -> dict[str, Any]:
        """Accept, tentatively accept or decline a meeting. SENDS a reply to the organizer
        (refused while SEND_ENABLED=false)."""
        raw, _ = ctx.aliases.resolve(event_id, expect=("e", "m"))

        def work(account):
            item, _ = fetch_event(account, raw)
            method = getattr(item, _RESPONSES[response], None)
            if method is None:
                raise Failure("validation", "that item is not a meeting you can respond to")
            method(**({"body": message} if message else {}))
            return {"responded": response, "event_id": event_id}
        return await ctx.gateway.call(work)

    @r.tool("full", "send")
    async def cancel_event(
        event_id: Annotated[str, Field(description="Event id (e3) of a meeting you organize.")],
        message: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a meeting you organize: SENDS cancellations to all attendees and removes
        it from your calendar (refused while SEND_ENABLED=false). No confirmation step."""
        raw, _ = ctx.aliases.resolve(event_id, expect=("e",))

        def work(account):
            item, _ = fetch_event(account, raw)
            if not getattr(item, "is_meeting", False):
                item.move_to_trash()  # plain appointment: nobody to notify
                return {"cancelled": True, "event_id": event_id, "cancellations_sent": False}
            if str(getattr(item, "my_response_type", "")) not in ("Organizer", "None", ""):
                raise Failure("validation", "you are not the organizer; use respond_to_event to decline")
            item.cancel(**({"body": message} if message else {}))
            return {"cancelled": True, "event_id": event_id, "cancellations_sent": True}
        return await ctx.gateway.call(work)
