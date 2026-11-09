"""Agentic scenario tests: Claude Code (headless) drives ews-lite against the
local grommunio stack; assertions are on mailbox state and the tool trace.

    EWS_AGENTIC=1 uv run pytest -m agentic -s

Opt-in only: costs real model tokens. Needs the grommunio dev stack, the
repo .env (see README) and a logged-in `claude` CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import pytest

from .harness import PORT, REPO, account

if os.environ.get("EWS_AGENTIC") != "1":
    pytest.skip("agentic scenarios are opt-in: set EWS_AGENTIC=1", allow_module_level=True)

ALICE = ("alice@grommunio.test", os.environ.get("ALICE_PASS", "Al1cePass!"))
BOB = ("bob@grommunio.test", os.environ.get("BOB_PASS", "B0bPass!!"))
SEED = REPO / "dev" / "grommunio" / "seed_realistic.py"
MANIFEST = REPO / "dev" / "grommunio" / "seed_manifest.json"


def _health() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


@pytest.fixture(scope="session")
def lite_server():
    """Reuse a warm ews-lite on PORT, otherwise start `uv run ews-mcp-lite` (reads the repo .env)."""
    if shutil.which("claude") is None:
        pytest.fail("the `claude` CLI is not on PATH")
    proc = None
    if not _health():
        # MCP_PORT in the environment beats the .env value, so the dev server can stay on 8000.
        proc = subprocess.Popen(["uv", "run", "ews-mcp-lite"], cwd=REPO,
                                env={**os.environ, "MCP_PORT": str(PORT)},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 60
    while time.time() < deadline:
        h = _health()
        if h and h["connection"]["state"] == "warm":
            break
        time.sleep(1)
    else:
        if proc:
            proc.terminate()
        pytest.fail(f"ews-lite did not become warm on port {PORT}: {_health()}")
    assert h["tier"] == "full" and h["send_enabled"], f"scenarios need tier=full and SEND_ENABLED=true: {h}"
    yield h
    if proc:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def seeded(lite_server) -> dict:
    """Fresh, deterministic mailbox state for every scenario."""
    subprocess.run([sys.executable, str(SEED), "--reset"], cwd=REPO, check=True, capture_output=True)
    return json.loads(MANIFEST.read_text())


@pytest.fixture(scope="session")
def alice():
    return account(*ALICE)


@pytest.fixture(scope="session")
def bob():
    return account(*BOB)
