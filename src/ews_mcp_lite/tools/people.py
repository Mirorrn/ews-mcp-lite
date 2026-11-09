"""People, tasks, out-of-office and server status."""

from itertools import islice
from typing import Annotated, Any, Literal

from exchangelib import OofSettings
from pydantic import Field

from .. import __version__
from ..aliases import is_alias
from ..dates import fmt, parse_when
from ..dto import page, task_card
from ..errors import Failure
from ..registry import Registry
from .common import check_page, fetch_one, take

CONTACT_SCAN_CAP = 500
CONTACT_FIELDS = ["id", "changekey", "display_name", "given_name", "surname", "email_addresses",
                  "company_name", "job_title", "phone_numbers", "department"]


def _contact_person(c: Any) -> dict[str, Any]:
    emails = [e.email for e in (getattr(c, "email_addresses", None) or []) if getattr(e, "email", None)]
    person: dict[str, Any] = {"name": c.display_name or " ".join(
        x for x in (c.given_name, c.surname) if x), "email": emails[0] if emails else None,
        "source": "contacts"}
    for key, attr in (("company", "company_name"), ("title", "job_title"), ("department", "department")):
        val = getattr(c, attr, None)
        if val:
            person[key] = val
    phones = [p.phone_number for p in (getattr(c, "phone_numbers", None) or []) if getattr(p, "phone_number", None)]
    if phones:
        person["phones"] = phones
    if len(emails) > 1:
        person["other_emails"] = emails[1:]
    return person


def _gal_person(entry: Any) -> dict[str, Any] | None:
    mailbox, contact = (entry if isinstance(entry, tuple) else (entry, None))
    email = getattr(mailbox, "email_address", None)
    if not email:
        return None
    person: dict[str, Any] = {"name": getattr(mailbox, "name", None) or email, "email": email,
                              "source": "directory"}
    if contact is not None:
        for key, attr in (("company", "company_name"), ("title", "job_title"),
                          ("department", "department")):
            val = getattr(contact, attr, None)
            if val:
                person[key] = val
    return person


def register(r: Registry) -> None:
    ctx = r.ctx

    @r.tool("read", "read")
    async def find_people(
        query: Annotated[str, Field(min_length=2, description="Name or email fragment.")],
        source: Literal["auto", "directory", "contacts"] = "auto",
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Find people in the directory (GAL, via ResolveNames) and/or personal contacts.
        Ids are p-aliases for get_contact."""
        def work(account):
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            if source in ("auto", "directory"):
                try:
                    entries = account.protocol.resolve_names([query], return_full_contact_data=True)
                except Exception:
                    entries = []
                for entry in entries or []:
                    if isinstance(entry, Exception):
                        continue
                    person = _gal_person(entry)
                    if person and person["email"].lower() not in seen:
                        seen.add(person["email"].lower())
                        found.append(person)
            if source in ("auto", "contacts"):
                needle = query.lower()
                for c in islice(account.contacts.all().only(*CONTACT_FIELDS), CONTACT_SCAN_CAP):
                    person = _contact_person(c)
                    hay = f"{person['name']} {' '.join([person['email'] or ''] + person.get('other_emails', []))}"
                    if needle in hay.lower() and (person["email"] or "").lower() not in seen:
                        seen.add((person["email"] or "").lower())
                        person["_raw"] = c.id
                        found.append(person)
            out = []
            for person in found[:limit]:
                raw = person.pop("_raw", None)
                person["id"] = ctx.aliases.alias(raw or person["email"], "p")
                out.append(person)
            return {"items": out, "count": len(out)}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_contact(
        id: Annotated[str, Field(description="p-alias from find_people, a raw contact id, or an email address.")],
    ) -> dict[str, Any]:
        """Fetch one person in full: directory entry or personal contact."""
        key = id.strip()
        if is_alias(key):
            key, _ = ctx.aliases.resolve(key, expect=("p",))

        def work(account):
            if "@" in key:
                for entry in account.protocol.resolve_names([key], return_full_contact_data=True) or []:
                    if isinstance(entry, Exception):
                        continue
                    person = _gal_person(entry)
                    if person:
                        return {"person": {"id": ctx.aliases.alias(key, "p"), **person}}
                for c in islice(account.contacts.all().only(*CONTACT_FIELDS), CONTACT_SCAN_CAP):
                    person = _contact_person(c)
                    if key.lower() in [e.lower() for e in [person["email"] or ""] + person.get("other_emails", [])]:
                        return {"person": {"id": ctx.aliases.alias(c.id, "p"), **person}}
                raise Failure("not_found", f"No person with address {key}.")
            c = fetch_one(account, key, only=CONTACT_FIELDS, folder=account.contacts)
            return {"person": {"id": ctx.aliases.alias(c.id, "p"), **_contact_person(c)}}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def list_tasks(
        include_completed: bool = False,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """List tasks (to-dos), open ones by default. Ids are k-aliases for update_task."""
        check_page(offset, limit)

        def work(account):
            qs = account.tasks.all().only("id", "changekey", "subject", "due_date", "is_complete",
                                          "status")
            rows = (t for t in qs if include_completed or not t.is_complete)
            items, more = take(rows, offset, limit)
            return page([task_card(t, ctx.aliases, ctx.tz) for t in items], offset, more)
        return await ctx.gateway.call(work)

    @r.tool("draft", "write")
    async def update_task(
        id: Annotated[str, Field(description="Task id (k1).")],
        complete: bool | None = None,
        due: Annotated[str | None, Field(description="'today', '+3d' or YYYY-MM-DD.")] = None,
    ) -> dict[str, Any]:
        """Mark a task complete and/or set its due date."""
        if complete is None and due is None:
            raise Failure("validation", "pass complete=true and/or due=<date>")
        raw, _ = ctx.aliases.resolve(id, expect=("k",))
        due_date = parse_when(due, "due", ctx.tz).date() if due else None

        def work(account):
            task = fetch_one(account, raw, only=["id", "changekey", "subject", "due_date",
                                                  "is_complete", "status", "start_date"],
                             folder=account.tasks)
            if due_date is not None:
                task.due_date = due_date
                task.save(update_fields=["due_date"])
            if complete:
                task.complete()
            return {"task_id": id, "complete": bool(complete) or bool(task.is_complete),
                    "due": due_date.isoformat() if due_date else None}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_oof_settings() -> dict[str, Any]:
        """Read the out-of-office (automatic reply) settings."""
        def work(account):
            oof = account.oof_settings
            out: dict[str, Any] = {"state": str(oof.state).lower(),
                                   "external_audience": str(oof.external_audience or ""),
                                   "internal_reply": oof.internal_reply or "",
                                   "external_reply": oof.external_reply or ""}
            if str(oof.state) == "Scheduled":
                out["start"], out["end"] = fmt(oof.start, ctx.tz), fmt(oof.end, ctx.tz)
            return out
        return await ctx.gateway.call(work)

    @r.tool("full", "send")
    async def set_oof(
        state: Literal["disabled", "enabled", "scheduled"],
        internal_reply: str | None = None,
        external_reply: str | None = None,
        external_audience: Literal["none", "known", "all"] = "all",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Set out-of-office auto-replies. Replies go to OTHER people, so this is
        send-class (refused while SEND_ENABLED=false). No confirmation step."""
        if state != "disabled" and not internal_reply:
            raise Failure("validation", "internal_reply is required unless state='disabled'")
        if state == "scheduled" and not (start and end):
            raise Failure("validation", "start and end are required when state='scheduled'")
        s = parse_when(start, "start", ctx.tz) if start else None
        e = parse_when(end, "end", ctx.tz) if end else None

        def work(account):
            kwargs: dict[str, Any] = {"state": state.capitalize(),
                                      "external_audience": external_audience.capitalize(),
                                      "internal_reply": internal_reply or "",
                                      "external_reply": external_reply or internal_reply or ""}
            if state == "scheduled":
                kwargs["start"], kwargs["end"] = s, e
            account.oof_settings = OofSettings(**kwargs)
            return {"state": state, "external_audience": external_audience}
        return await ctx.gateway.call(work)

    @r.tool("read", "read")
    async def get_server_status(
        probe: Annotated[bool, Field(description="true = make a live EWS round trip to test the connection.")] = False,
    ) -> dict[str, Any]:
        """This server's status: version, tier, kill-switch, registered tools, connection."""
        out: dict[str, Any] = {"version": __version__, "tier": ctx.settings.ews_capability_tier,
                               "send_enabled": ctx.settings.send_enabled,
                               "tools": len(ctx.registered), "tool_names": ctx.registered,
                               "call_counts": dict(ctx.counters), "aliases": ctx.aliases.stats()}
        if probe:
            def work(account):
                account.inbox.refresh()
                return {"inbox_total": account.inbox.total_count,
                        "server_build": str(account.version.build),
                        "api_version": account.version.api_version}
            try:
                out["probe"] = await ctx.gateway.call(work)
            except Failure as f:
                out["probe"] = {"error": f.code, "message": f.message}
        out["connection"] = ctx.gateway.status()
        return out
