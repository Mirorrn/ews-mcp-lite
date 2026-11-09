"""Helpers for the agentic scenario tests: run Claude Code headless against the
lite server, record the trace, and inspect mailbox state with exchangelib."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exchangelib import DELEGATE, Account, Configuration, Credentials
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

from ews_mcp_lite import compat

compat.apply()
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter  # local grommunio, self-signed

REPO = Path(__file__).resolve().parents[2]
RUNS = Path(__file__).with_name("runs")
SERVER_NAME = "ews-lite"
PORT = int(os.environ.get("EWS_AGENTIC_PORT", "18900"))
MCP_URL = f"http://127.0.0.1:{PORT}/mcp"
EWS_URL = os.environ.get("EWS_SERVER_URL", "https://127.0.0.1/EWS/Exchange.asmx")
MODEL = os.environ.get("EWS_AGENTIC_MODEL", "sonnet")
BUDGET = os.environ.get("EWS_AGENTIC_BUDGET_USD", "1.50")
TIMEOUT = int(os.environ.get("EWS_AGENTIC_TIMEOUT", "600"))
SYSTEM = ("You are the email and calendar assistant of Alice Test (alice@grommunio.test). "
          "You act on her mailbox only through the ews-lite tools. Today's date and time come from the tools "
          "(server timezone Europe/Berlin).")

MUTATING = {"create_draft", "update_draft", "delete_draft", "update_messages", "move_messages", "create_event",
            "update_event", "update_task", "send_draft", "respond_to_event", "cancel_event", "delete_messages",
            "set_oof"}


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    result: str = ""
    is_error: bool = False


@dataclass
class AgentRun:
    scenario: str
    prompt: str
    session_id: str | None = None
    result: str = ""
    is_error: bool = False
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    calls: list[ToolCall] = field(default_factory=list)

    @property
    def tools(self) -> list[str]:
        return [c.name for c in self.calls]

    def called(self, *names: str) -> bool:
        return any(n in self.tools for n in names)

    def mutations(self) -> list[ToolCall]:
        return [c for c in self.calls if c.name in MUTATING]


def short(name: str) -> str:
    prefix = f"mcp__{SERVER_NAME}__"
    return name[len(prefix):] if name.startswith(prefix) else name


def run_agent(scenario: str, prompt: str, resume: str | None = None) -> AgentRun:
    """Run one headless Claude Code turn; only the ews-lite MCP tools are available."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RUNS / f"{stamp}-{scenario}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = out_dir / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {SERVER_NAME: {"type": "http", "url": MCP_URL}}}))
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(cfg), "--strict-mcp-config", "--tools", "",
           "--allowedTools", f"mcp__{SERVER_NAME}__*", "--permission-mode", "dontAsk",
           "--model", MODEL, "--max-budget-usd", BUDGET, "--append-system-prompt", SYSTEM]
    if resume:
        cmd += ["--resume", resume]
    started = time.time()
    # run from an empty directory so no project CLAUDE.md / settings leak into the scenario
    proc = subprocess.run(cmd, cwd=RUNS, capture_output=True, text=True, timeout=TIMEOUT)
    run = AgentRun(scenario=scenario, prompt=prompt, duration_s=round(time.time() - started, 1))
    (out_dir / "transcript.jsonl").write_text(proc.stdout)
    if proc.stderr:
        (out_dir / "stderr.txt").write_text(proc.stderr)
    pending: dict[str, ToolCall] = {}
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("session_id"):
            run.session_id = ev["session_id"]
        content = (ev.get("message") or {}).get("content")
        if ev.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    call = ToolCall(name=short(block["name"]), input=block.get("input") or {})
                    pending[block["id"]] = call
                    run.calls.append(call)
        if ev.get("type") == "user" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call = pending[block["tool_use_id"]]
                    raw = block.get("content")
                    call.result = raw if isinstance(raw, str) else "".join(
                        part.get("text", "") for part in raw or [] if isinstance(part, dict))
                    call.is_error = bool(block.get("is_error"))
        if ev.get("type") == "result":
            run.session_id = ev.get("session_id") or run.session_id
            run.result = ev.get("result") or ""
            run.is_error = bool(ev.get("is_error"))
            run.cost_usd = float(ev.get("total_cost_usd") or 0.0)
            run.num_turns = int(ev.get("num_turns") or 0)
    trace = {"scenario": scenario, "prompt": prompt, "resume": resume, "session_id": run.session_id,
             "model": MODEL, "cost_usd": run.cost_usd, "duration_s": run.duration_s, "num_turns": run.num_turns,
             "is_error": run.is_error, "exit_code": proc.returncode, "result": run.result,
             "tool_calls": [c.__dict__ for c in run.calls]}
    (out_dir / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False))
    print(f"\n[{scenario}] {run.duration_s}s ${run.cost_usd:.4f} turns={run.num_turns} tools={run.tools}")
    assert proc.returncode == 0 and not run.is_error, f"claude run failed ({proc.returncode}): {proc.stderr[-500:]}"
    return run


# ------------------------------------------------------------- mailbox state

def account(user: str, password: str) -> Account:
    cfg = Configuration(service_endpoint=EWS_URL, credentials=Credentials(user, password))
    return Account(user, config=cfg, autodiscover=False, access_type=DELEGATE)


def archive(acct: Account):
    return next((f for f in acct.msg_folder_root.children if f.name == "Archive"), None)


def recipients(item: Any) -> list[str]:
    out = []
    for field_name in ("to_recipients", "cc_recipients", "bcc_recipients"):
        out += [m.email_address.lower() for m in getattr(item, field_name, None) or [] if m.email_address]
    return out


def items(folder, *fields: str) -> list[Any]:
    base = ("id", "subject", "is_read", "to_recipients", "cc_recipients", "bcc_recipients", "datetime_received")
    return list(folder.all().only(*(base + fields)))


TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.][0-5]\d\b|\b\d{1,2}\s?(am|pm|uhr)\b|\b(monday|tuesday|wednesday|"
                     r"thursday|friday|tomorrow|morgen)\b", re.IGNORECASE)
