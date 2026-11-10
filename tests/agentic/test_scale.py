"""Agentic scenarios on a LARGE mailbox (seed_scale.py: ~20k messages in Inbox,
subfolders, Archive/<year> and Sent Items). All are questions: the agent must find
one message among thousands and must not change anything.

    EWS_AGENTIC=1 uv run pytest -m agentic -s tests/agentic/test_scale.py

The haystack is seeded once per session if it is not there yet (about two
minutes). The scenarios in test_scenarios.py reset the mailbox, so run this
module on its own or first. EWS_SCALE_COUNT sets the haystack size.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from .harness import REPO, run_agent

pytestmark = pytest.mark.agentic

SEED_DIR = REPO / "dev" / "grommunio"
MANIFEST = SEED_DIR / "seed_scale_manifest.json"
COUNT = int(os.environ.get("EWS_SCALE_COUNT", "20000"))
MAX_CALLS = 25  # a lookup that needs more tool calls than this is not working, even if it ends up right


@pytest.fixture(scope="session")
def scale(lite_server, alice) -> dict:
    """The needle facts. Seeds realistic week + haystack unless a haystack is already in place."""
    if not MANIFEST.exists() or alice.inbox.all().count() < 5000:
        subprocess.run([sys.executable, str(SEED_DIR / "seed_realistic.py"), "--reset"], cwd=REPO, check=True,
                       capture_output=True)
        subprocess.run([sys.executable, str(SEED_DIR / "seed_scale.py"), "--count", str(COUNT)], cwd=REPO, check=True,
                       capture_output=True)
    return json.loads(MANIFEST.read_text())["needles"]


def ask(scenario: str, prompt: str):
    run = run_agent(scenario, prompt)
    errors = [c.name for c in run.calls if c.is_error]
    print(f"[{scenario}] calls={len(run.calls)} errors={errors}\n[{scenario}] answer: {run.result[:400]!r}")
    assert not run.mutations(), f"a question caused mutations: {[c.name for c in run.mutations()]}"
    assert len(run.calls) <= MAX_CALLS, f"{len(run.calls)} tool calls: {run.tools}"
    return run


def test_old_invoice_found_by_body_text(scale):
    """425 days back in a 15k Inbox; number and amount are only in the body."""
    f = scale["invoice"]
    run = ask("scale-invoice", "Nordwind Logistics sent me an invoice last year for a transport to Rotterdam. "
                               "What is the invoice number and the amount?")
    assert f["number"] in run.result, run.result
    assert re.search(r"18[.,]?430[.,]50", run.result), run.result


def test_mail_filed_in_archive_subfolder(scale):
    """The mail is in Archive/2024, 760 days old."""
    f = scale["lease"]
    run = ask("scale-lease", "What is the notice period for our office lease in Lindenstraße? Our lawyers wrote to me "
                             "about it a while ago, I filed that mail away somewhere. Give me their reference number too.")
    assert f["reference"] in run.result, run.result
    assert re.search(r"neun Monate|nine months|9 months|9 Monate", run.result, re.IGNORECASE), run.result


def test_sender_known_by_name_only(scale):
    """380 days back, sender known by surname only."""
    f = scale["lecture"]
    run = ask("scale-lecture", "In which room is the guest lecture that Dr. Lindqvist invited me to, and what do they pay?")
    assert f["room"] in run.result, run.result
    assert f["fee"] in run.result, run.result


def test_recent_mail_in_project_subfolder(scale):
    """Nine days old in Inbox/Projects/Atlas, among 1,600 other Atlas mails."""
    f = scale["golive"]
    run = ask("scale-golive", "What did the steering committee decide about the Atlas go-live? "
                              "I need the codename and who owns the rollback.")
    assert f["codename"].lower() in run.result.lower(), run.result
    assert f["rollback_owner"] in run.result, run.result


def test_unanswered_sent_mail(scale):
    """Six days old among ~650 sent mails of the last 30 days, most of them unanswered."""
    run = ask("scale-waiting", "Did Viktor Sandoval from Helios ever get back to me about the maintenance contract quote?")
    assert "T-88" in run.result or "Turbine" in run.result, run.result
    assert re.search(r"\b(no|not|hasn't|has not|haven't|never|without)\b", run.result, re.IGNORECASE), run.result


def test_morning_triage_stays_cheap(scale):
    """The daily question must not get more expensive with mailbox size."""
    run = ask("scale-attention", "What needs my attention today?")
    assert "budget" in run.result.lower(), run.result
    assert len(run.calls) <= 12, f"{len(run.calls)} tool calls for a morning brief: {run.tools}"
    size = sum(len(c.result) for c in run.calls)
    print(f"[scale-attention] tool output read by the model: {size} chars")
    assert size < 120_000, f"morning brief pulled {size} chars of tool output"


def test_thread_followed_across_inbox_and_sent(scale):
    """The Q4 budget thread: Bob's draft (Inbox, with a CSV), Alice's objection (Sent Items),
    Bob's revision (Inbox, second CSV), buried under hundreds of other 'budget' mails. The
    answer needs all three messages in order plus the attachment names."""
    run = ask("scale-thread", "Walk me through the 'Q4 budget planning' conversation with Bob from last week: who said "
                              "what, in order, what changed in the numbers, and which files were attached along the way?")
    assert run.called("get_thread"), run.tools
    assert "budget_q4_draft.csv" in run.result and "budget_q4_v2.csv" in run.result, run.result
    assert re.search(r"1[.,]?600", run.result), run.result  # the revised Training figure, from Bob's last mail
    assert re.search(r"new hires|two hires|Training looks low|too low|low", run.result, re.IGNORECASE), run.result
