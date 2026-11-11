"""Time the read tools against the mailbox seeded by seed_scale.py.

    uv run python dev/grommunio/bench_scale.py

Calls the tool handlers in-process through the real dispatcher (no MCP/HTTP hop)
and prints, per call: wall time, result size in characters, item count, whether
the expected needle came back, and any note the tool attached.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from pathlib import Path

from ews_mcp_lite.config import Settings
from ews_mcp_lite.errors import Failure
from ews_mcp_lite.ews import Gateway
from ews_mcp_lite.registry import Context, Registry, dispatch
from ews_mcp_lite.tools import MODULES

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
N = json.loads(Path(__file__).with_name("seed_scale_manifest.json").read_text())["needles"]

# (label, tool, arguments, text that must appear in the result or None)
CASES = [
    ("overview", "get_mailbox_overview", {}, None),
    ("folders depth 3", "list_folders", {"depth": 3}, "Atlas"),
    ("inbox page 1", "search_messages", {"limit": 20}, None),
    ("inbox page at offset 5000", "search_messages", {"offset": 5000, "limit": 20}, None),
    ("inbox limit 100", "search_messages", {"limit": 100}, None),
    ("unread only", "search_messages", {"unread_only": True, "limit": 20}, None),
    ("since -7d", "search_messages", {"since": "-7d", "limit": 20}, None),
    ("subject, one folder", "search_messages", {"subject": "go-live", "folder": "Inbox/Projects/Atlas"}, "go-live"),
    ("subject, all folders", "search_messages", {"subject": "go-live"}, "go-live"),
    ("subject, common word, all", "search_messages", {"subject": "budget review", "limit": 20}, None),
    ("body needle, inbox (425 d old)", "search_messages", {"query": N["invoice"]["number"], "folder": "inbox"},
     N["invoice"]["subject"]),
    ("body needle, all (425 d old)", "search_messages", {"query": N["invoice"]["number"]}, N["invoice"]["subject"]),
    ("body common phrase, all", "search_messages", {"query": "rollout", "limit": 20}, None),
    ("body no hit, all", "search_messages", {"query": "zzqx-no-such-token"}, None),
    ("archived needle, inbox only", "search_messages", {"query": N["lease"]["reference"], "folder": "inbox"},
     N["lease"]["subject"]),
    ("archived needle, all", "search_messages", {"query": N["lease"]["reference"]}, N["lease"]["subject"]),
    ("all, offset 500", "search_messages", {"query": "rollout", "offset": 500, "limit": 20}, None),
    ("two words, not adjacent", "search_messages", {"query": "invoice Rotterdam"}, N["invoice"]["subject"]),
    ("exact sender, all", "search_messages", {"sender": "k.lindqvist@uni-example.edu"}, "Guest lecture"),
    ("sender by name, inbox (380 d)", "search_messages", {"sender": "Lindqvist", "folder": "inbox"}, "Guest lecture"),
    ("sender by name, all (380 d)", "search_messages", {"sender": "Lindqvist"}, "Guest lecture"),
    ("sender by name, frequent, all", "search_messages", {"sender": "priya", "limit": 20}, None),
    ("waiting_on defaults", "waiting_on", {}, None),
    ("waiting_on to='sandoval'", "waiting_on", {"to": "sandoval"}, N["unanswered"]["subject"]),
    ("sent, exact subject", "search_messages", {"folder": "sent", "subject": "Turbine T-88"}, "T-88"),
]


async def main() -> None:
    s = Settings(_env_file=None, ews_server_url=os.environ.get("EWS_SERVER_URL", "https://127.0.0.1:10443/EWS/Exchange.asmx"),
                 ews_email="alice@grommunio.test", ews_password=os.environ.get("ALICE_PASS", "Al1cePass!"),
                 ews_insecure_skip_verify=True, ews_tz="Europe/Berlin", ews_server_type="grommunio",
                 ews_capability_tier="read", circuit_failure_threshold=1000)
    gw = Gateway(s)
    assert gw.connect(), gw.last_error
    ctx = Context(settings=s, gateway=gw)
    reg = Registry(ctx)
    for m in MODULES:
        m.register(reg)
    specs = {sp.name: sp for sp in reg.specs}
    acct = gw.account()
    sizes = {f.name: f.total_count for f in [acct.inbox, acct.sent, *acct.inbox.walk()]}
    print("folder sizes:", sizes, "\n")
    print(f"{'case':36} {'ms':>7} {'chars':>7} {'items':>5} {'found':>5}  note")
    first_id = None
    for label, tool, args, expect in CASES:
        t = time.monotonic()
        try:
            out = await dispatch(ctx, specs[tool], dict(args))
        except Failure as f:
            print(f"{label:36} {(time.monotonic() - t) * 1000:7.0f}  FAILED {f.code}: {f.message[:90]}")
            continue
        ms = (time.monotonic() - t) * 1000
        blob = json.dumps(out, ensure_ascii=False)
        found = "-" if expect is None else ("yes" if expect in blob else "NO")
        items = out.get("items") or out.get("recent_unread") or []
        if tool == "search_messages" and items and first_id is None:
            first_id = items[0]["id"]
        print(f"{label:36} {ms:7.0f} {len(blob):7d} {len(items):5d} {found:>5}  {(out.get('note') or '')[:110]}")
    for label, tool, args in (("get_message", "get_message", {"id": first_id}),
                              ("get_thread", "get_thread", {"id": first_id})):
        t = time.monotonic()
        out = await dispatch(ctx, specs[tool], args)
        print(f"{label:36} {(time.monotonic() - t) * 1000:7.0f} {len(json.dumps(out)):7d} {out.get('count', 1):5d}"
              f"     -  {out.get('note') or ''}")


if __name__ == "__main__":
    asyncio.run(main())
