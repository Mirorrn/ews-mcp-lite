"""Fill alice's mailbox with a large, deterministic haystack plus a few needles.

    uv run python dev/grommunio/seed_scale.py --count 20000          # add on top of what is there
    uv run python dev/grommunio/seed_scale.py --count 20000 --reset  # wipe both mailboxes first

For answering "does this still work on a big mailbox?". Only for the local dev
stack (same guard as seed_realistic.py). The haystack is spread over Inbox,
Inbox/Projects/Atlas, Archive/2024, Archive/2025 and Sent Items, from ~60
senders over the last two and a half years. The needles are single messages
with unique facts, placed where the tools are known to be weak (old, in a
subfolder, from a sender known only by name, an unanswered sent mail among
hundreds). dev/grommunio/seed_scale_manifest.json records them for
tests/agentic/test_scale.py and dev/grommunio/bench_scale.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import time
from pathlib import Path

import seed_realistic as sr
from exchangelib import EWSDateTime, Message
from exchangelib.folders import Folder

MANIFEST = Path(__file__).with_name("seed_scale_manifest.json")
ME = sr.ALICE

FIRST = ["Priya", "Jonas", "Mei", "Tomasz", "Aisha", "Lukas", "Sofia", "Daniel", "Hannah", "Omar", "Elena", "Marco",
         "Ingrid", "Pavel", "Chloe", "Rafael", "Nora", "Stefan", "Yuki", "Fatima"]
LAST = ["Raman", "Becker", "Chen", "Kowalski", "Rahman", "Vogel", "Rossi", "Meyer", "Schulz", "Haddad", "Petrova",
        "Bianchi", "Nilsson", "Novak", "Martin", "Silva", "Hansen", "Braun", "Tanaka", "Malik"]
DOMAINS = ["grommunio.test", "grommunio.test", "grommunio.test", "acme.example", "nordwind.example", "contoso.example",
           "fabrikam.example", "helios-energy.example", "kanzlei-hartmann.example", "uni-example.edu"]
BULK = [("Jira", "jira@tracker.example"), ("GitLab", "gitlab@ci.example"), ("Vendor Weekly", "news@vendor.example"),
        ("CloudNews Digest", "digest@cloudnews.example"), ("HR Portal", "noreply@hr.example"),
        ("Travel Desk", "bookings@travel.example"), ("Monitoring", "alerts@monitoring.example")]
PROJECTS = ["Atlas", "Borealis", "Cobalt", "Delta", "Ember", "Falcon", "Granite", "Harbor"]
TOPICS = ["status update", "budget review", "sprint planning", "release notes", "customer escalation", "contract renewal",
          "security review", "onboarding", "data migration", "vendor selection", "roadmap", "retrospective",
          "capacity planning", "incident follow-up", "quarterly report", "offsite logistics", "hiring pipeline",
          "license audit", "architecture decision", "performance numbers"]
WORDS = ("the team has reviewed proposal and we think that next steps should include a short call with everyone involved "
         "please find details below numbers look stable compared to last month although delivery slipped by few days "
         "let me know if this works for you otherwise I will reschedule thanks regards meeting agenda attached draft "
         "open questions remain about pricing timeline scope staffing risk dependencies approval sign-off rollout "
         "testing feedback customers partners stakeholders management summary action items owner deadline").split()


def people(rng: random.Random) -> list[tuple[str, str]]:
    out = []
    for i in range(60):
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7 + i // 20) % len(LAST)]
        out.append((f"{first} {last}", f"{first}.{last}@{DOMAINS[i % len(DOMAINS)]}".lower()))
    rng.shuffle(out)
    return out


def text(rng: random.Random, sentences: int) -> str:
    out = []
    for _ in range(sentences):
        s = " ".join(rng.choices(WORDS, k=rng.randint(8, 18)))
        out.append(s.capitalize() + ".")
    return " ".join(out)


def message(acct, folder, *, subject, frm, to, body, when, read=True, importance="Normal", categories=None) -> Message:
    msgid = f"<{rng_id(subject, when)}@scale.grommunio.test>"
    return Message(account=acct, folder=folder, subject=subject, body=body, author=sr.mbox(*frm),
                   to_recipients=[sr.mbox(*r) for r in to], is_read=read, importance=importance,
                   categories=categories, x_delivery=when, x_submit=when, x_msgflags=1 if read else 0,
                   x_sname=frm[0], x_saddr=frm[1], x_stype="SMTP", x_ssmtp=frm[1], x_msgid=msgid)


def rng_id(subject: str, when: EWSDateTime) -> str:
    return f"{abs(hash((subject, when.isoformat()))):x}"


def ago(days: float, hour: int = 10) -> EWSDateTime:
    d = sr.NOW - dt.timedelta(days=days)
    return d.replace(hour=hour, minute=int(days * 37) % 60)


def ensure(parent: Folder, name: str) -> Folder:
    for c in parent.children:
        if c.name == name:
            return c
    f = Folder(parent=parent, name=name)
    f.save()
    parent.root.clear_cache() if hasattr(parent.root, "clear_cache") else None
    return f


def haystack(rng: random.Random, acct, folders: dict[str, Folder], count: int):
    """Yield (folder key, Message). Zipf-ish senders, threads of 1-6, ~8% bulk mail."""
    ppl = people(rng)
    weights = [1 / (i + 1) ** 0.8 for i in range(len(ppl))]
    plan = (("inbox", 0.50, (0, 540)), ("atlas", 0.05, (0, 400)), ("a2025", 0.15, (313, 678)),
            ("a2024", 0.15, (679, 900)), ("sent", 0.15, (0, 180)))
    made = 0
    while made < count:
        key, _, (lo, hi) = rng.choices(plan, weights=[p[1] for p in plan])[0]
        start = rng.uniform(lo, hi)
        if key != "sent" and rng.random() < 0.08:
            frm = rng.choice(BULK)
            subj = f"{frm[0]}: {rng.choice(TOPICS)} #{rng.randint(100, 99999)}"
            yield key, message(acct, folders[key], subject=subj, frm=frm, to=[ME], body=text(rng, 6),
                               when=ago(start, rng.randint(6, 20)), read=rng.random() < 0.6)
            made += 1
            continue
        other = rng.choices(ppl, weights=weights)[0]
        project = "Atlas" if key == "atlas" else rng.choice(PROJECTS)
        subj = f"{project}: {rng.choice(TOPICS)} {rng.choice(['', 'Q1', 'Q2', 'Q3', 'Q4', 'draft', 'v2', 'follow-up'])}".strip()
        for n in range(rng.choice([1, 1, 1, 2, 2, 3, 4, 6])):
            if made >= count:
                break
            day = max(start - n * rng.uniform(0.05, 1.5), lo if key != "inbox" else 0.02)
            mine = key == "sent"
            frm, to = (ME, [other]) if mine else (other, [ME])
            body = f"Hi {to[0][0].split()[0]},\n\n{text(rng, rng.randint(3, 9))}\n\nBest,\n{frm[0].split()[0]}"
            yield key, message(acct, folders[key], subject=("Re: " if n else "") + subj, frm=frm, to=to, body=body,
                               when=ago(day, rng.randint(7, 19)), read=mine or day > 20 or rng.random() < 0.7)
            made += 1


def needles(acct, folders: dict[str, Folder]) -> tuple[list[tuple[str, Message]], dict]:
    """Single messages with unique facts. Each documents which weakness it probes."""
    lind = ("Dr. Katarina Lindqvist", "k.lindqvist@uni-example.edu")
    nordwind = ("Nordwind Logistics Billing", "billing@nordwind.example")
    hartmann = ("Kanzlei Hartmann", "office@kanzlei-hartmann.example")
    helios = ("Viktor Sandoval", "viktor.sandoval@helios-energy.example")
    out = [
        # old + fact only in the body: needs body search to reach 14 months back
        ("inbox", message(acct, folders["inbox"], subject="Rechnung / Invoice October", frm=nordwind, to=[ME], when=ago(425),
                          body="Dear Ms Test,\n\nplease find our invoice NW-2025-0471 for the pallet transport to Rotterdam.\n"
                               "Amount due: 18,430.50 EUR, payable within 30 days.\n\nKind regards,\nNordwind Logistics Billing")),
        # lives in a subfolder: search_messages looks at one folder per call
        ("a2024", message(acct, folders["a2024"], subject="Mietvertrag Büro Lindenstraße", frm=hartmann, to=[ME], when=ago(760),
                          body="Sehr geehrte Frau Test,\n\ndie Kündigungsfrist für den Mietvertrag Lindenstraße 14 beträgt "
                               "neun Monate zum Quartalsende. Das Aktenzeichen lautet HM-4471-B.\n\nMit freundlichen Grüßen\n"
                               "Kanzlei Hartmann")),
        # sender known by name only, far beyond the newest 500 inbox items
        ("inbox", message(acct, folders["inbox"], subject="Guest lecture in the spring term", frm=lind, to=[ME], when=ago(380),
                          body="Dear Alice,\n\nthank you for agreeing to the guest lecture. It will take place in lecture hall "
                               "H-207 and the honorarium is 750 EUR.\n\nBest wishes,\nKatarina Lindqvist")),
        # project subfolder, recent: an agent must think of looking outside the Inbox
        ("atlas", message(acct, folders["atlas"], subject="Atlas: go-live decision", frm=("Priya Raman", "priya.raman@grommunio.test"),
                          to=[ME], when=ago(9), importance="High",
                          body="Hi Alice,\n\nthe steering committee decided: Atlas goes live on the cutover weekend with the "
                               "codename BLUE HERON. Rollback owner is Tomasz.\n\nBest,\nPriya")),
        # an unanswered sent mail from 6 days ago, among hundreds of sent items
        ("sent", message(acct, folders["sent"], subject="Angebot Wartungsvertrag Turbine T-88", frm=ME, to=[helios], when=ago(6),
                         body="Hello Viktor,\n\ncould you send the quote for the T-88 maintenance contract by the end of the "
                              "month? We need it for the budget round.\n\nThanks,\nAlice")),
    ]
    facts = {
        "invoice": {"folder": "Inbox", "age_days": 425, "subject": "Rechnung / Invoice October",
                    "number": "NW-2025-0471", "amount": "18,430.50"},
        "lease": {"folder": "Archive/2024", "age_days": 760, "subject": "Mietvertrag Büro Lindenstraße",
                  "reference": "HM-4471-B", "notice": "neun Monate"},
        "lecture": {"folder": "Inbox", "age_days": 380, "sender_name": "Lindqvist", "room": "H-207", "fee": "750"},
        "golive": {"folder": "Inbox/Projects/Atlas", "age_days": 9, "codename": "BLUE HERON", "rollback_owner": "Tomasz"},
        "unanswered": {"folder": "Sent Items", "age_days": 6, "subject": "Angebot Wartungsvertrag Turbine T-88",
                       "to": helios[1]},
    }
    return out, facts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=20000)
    ap.add_argument("--reset", action="store_true", help="hard-delete both mailboxes' contents first")
    ap.add_argument("--chunk", type=int, default=100)
    args = ap.parse_args()
    sr.guard()
    alice = sr.account(sr.A, sr.A_PASS)
    if args.reset:
        print(f"reset: removed {sr.reset_mailbox(alice) + sr.reset_mailbox(sr.account(sr.B, sr.B_PASS))} items")
    arch = sr.archive_folder(alice, create=True)
    folders = {"inbox": alice.inbox, "sent": alice.sent, "a2024": ensure(arch, "2024"), "a2025": ensure(arch, "2025"),
               "atlas": ensure(ensure(alice.inbox, "Projects"), "Atlas")}
    rng = random.Random(4711)
    special, facts = needles(alice, folders)
    pending: dict[str, list[Message]] = {k: [] for k in folders}
    written: dict[str, int] = {k: 0 for k in folders}
    started = time.time()

    def flush(key: str) -> None:
        if pending[key]:
            alice.bulk_create(folder=folders[key], items=pending[key], message_disposition="SaveOnly",
                              chunk_size=args.chunk)
            written[key] += len(pending[key])
            pending[key] = []

    total = 0
    for key, msg in [*special, *haystack(rng, alice, folders, args.count)]:
        pending[key].append(msg)
        total += 1
        if len(pending[key]) >= args.chunk:
            flush(key)
        if total % 2000 == 0:
            print(f"  {total} items, {total / (time.time() - started):.0f}/s", flush=True)
    for key in folders:
        flush(key)
    took = time.time() - started
    manifest = {"seeded_at": sr.NOW.isoformat(), "haystack": args.count, "written": written, "needles": facts,
                "seconds": round(took, 1)}
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {total} items in {took:.0f}s ({total / took:.0f}/s): {written}\nmanifest: {MANIFEST}")


if __name__ == "__main__":
    main()
