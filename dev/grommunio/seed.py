"""Seed the local grommunio test mailboxes (test data only). Idempotent: skips
when alice's inbox already holds the seeded budget mail.

    uv run python dev/grommunio/seed.py
"""

import datetime as dt
import os
import time
import warnings

from exchangelib import (
    DELEGATE,
    Account,
    CalendarItem,
    Configuration,
    Contact,
    Credentials,
    EWSTimeZone,
    FileAttachment,
    HTMLBody,
    Mailbox,
    Message,
    Task,
)
from exchangelib.indexed_properties import EmailAddress
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter  # the dev stack uses a self-signed certificate

URL = os.environ.get("EWS_URL", "https://127.0.0.1:10443/EWS/Exchange.asmx")
TZ = EWSTimeZone("Europe/Berlin")
A, B = "alice@grommunio.test", "bob@grommunio.test"


def account(user: str, password: str) -> Account:
    cfg = Configuration(service_endpoint=URL, credentials=Credentials(user, password))
    return Account(user, config=cfg, autodiscover=False, access_type=DELEGATE, default_timezone=TZ)


def main() -> None:
    alice = account(A, os.environ.get("ALICE_PASS", "Al1cePass!"))
    bob = account(B, os.environ.get("BOB_PASS", "B0bPass!!"))
    if list(alice.inbox.filter(subject="Q4 budget draft").only("id")[:1]):
        print("already seeded; nothing to do")
        return

    m = Message(account=bob, subject="Q4 budget draft", to_recipients=[Mailbox(email_address=A)],
                body=HTMLBody("<p>Hi Alice,</p><p>attached is the Q4 budget draft. "
                              "Please review by Friday.</p><p>Bob</p>"))
    m.attach(FileAttachment(name="budget.txt", content=b"Item,Amount\nTravel,1200\nHardware,4800\n"))
    m.send(save_copy=True)
    Message(account=bob, subject="Lunch on Tuesday?", to_recipients=[Mailbox(email_address=A)],
            body="Are you free for lunch on Tuesday?").send()
    Message(account=bob, subject="اجتماع الميزانية", to_recipients=[Mailbox(email_address=A)],
            body="مرحبا، هل يمكننا مناقشة الميزانية غدا؟").send()
    Message(account=alice, subject="Contract renewal question", to_recipients=[Mailbox(email_address=B)],
            body="Bob, can you confirm the renewal date for the hosting contract?").send(save_copy=True)

    start = (dt.datetime.now(TZ) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    CalendarItem(account=alice, folder=alice.calendar, subject="Budget review", start=start,
                 end=start + dt.timedelta(hours=1), location="Room 2", body="Review Q4 numbers").save()
    Task(account=alice, folder=alice.tasks, subject="Send Q4 budget feedback",
         due_date=dt.date.today() + dt.timedelta(days=2)).save()
    Contact(account=alice, folder=alice.contacts, given_name="Carol", surname="Partner",
            display_name="Carol Partner", company_name="Partner Corp",
            email_addresses=[EmailAddress(email="carol@partner.example", label="EmailAddress1")]).save()
    time.sleep(3)
    print("seeded; alice inbox:", [i.subject for i in alice.inbox.all().only("subject")])


if __name__ == "__main__":
    main()
