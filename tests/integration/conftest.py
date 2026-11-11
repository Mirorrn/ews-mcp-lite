"""Live EWS integration tests (grommunio dev stack or a real Exchange test mailbox).

Enable with:
    EWS_IT_URL=https://127.0.0.1:10443/EWS/Exchange.asmx
    EWS_IT_USER=alice@grommunio.test   EWS_IT_PASSWORD=...
    EWS_IT_PEER=bob@grommunio.test     EWS_IT_PEER_PASSWORD=...   (second mailbox)
    EWS_IT_INSECURE=1                  (self-signed certificate)
    EWS_IT_TZ=Europe/Berlin
    EWS_IT_SERVER_TYPE=grommunio       (default: exchange)
Run: uv run pytest -m integration

Every item a test creates carries a unique marker in its subject; a session
finalizer hard-deletes everything with that marker in both mailboxes.
"""

import os
import time
import uuid
import warnings
from typing import Any

import pytest
from exchangelib import DELEGATE, Account, Configuration, Credentials, Q
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

from ews_mcp_lite.app import build_server
from ews_mcp_lite.config import Settings
from ews_mcp_lite.ews import Gateway

URL = os.environ.get("EWS_IT_URL")
MARK = f"[it-{uuid.uuid4().hex[:8]}]"

if not URL:
    pytest.skip("integration tests need EWS_IT_URL", allow_module_level=True)

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
if os.environ.get("EWS_IT_INSECURE") == "1":
    BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

USER = os.environ["EWS_IT_USER"]
PEER = os.environ.get("EWS_IT_PEER")
TZ = os.environ.get("EWS_IT_TZ", "UTC")
SERVER_TYPE = os.environ.get("EWS_IT_SERVER_TYPE", "exchange")


def settings(**kw: Any) -> Settings:
    base = dict(ews_server_url=URL, ews_email=USER, ews_password=os.environ["EWS_IT_PASSWORD"],
                ews_insecure_skip_verify=os.environ.get("EWS_IT_INSECURE") == "1", ews_tz=TZ,
                ews_server_type=SERVER_TYPE,
                ews_capability_tier="full", send_enabled=True)
    base.update(kw)
    return Settings(_env_file=None, **base)


def raw_account(user: str, password: str) -> Account:
    cfg = Configuration(service_endpoint=URL, credentials=Credentials(user, password))
    return Account(user, config=cfg, autodiscover=False, access_type=DELEGATE)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def gateway() -> Gateway:
    gw = Gateway(settings())
    assert gw.connect(), f"cannot reach {URL}: {gw.last_error}"
    return gw


@pytest.fixture
def server(gateway):
    mcp, ctx = build_server(settings(), gateway)
    return mcp


@pytest.fixture(scope="session")
def me() -> Account:
    return raw_account(USER, os.environ["EWS_IT_PASSWORD"])


@pytest.fixture(scope="session")
def peer() -> Account:
    if not PEER:
        pytest.skip("needs EWS_IT_PEER / EWS_IT_PEER_PASSWORD")
    return raw_account(PEER, os.environ["EWS_IT_PEER_PASSWORD"])


@pytest.fixture
def mark() -> str:
    return MARK


def wait_for(fn, timeout: float = 20.0, step: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = fn()
        if got:
            return got
        time.sleep(step)
    return fn()


def _purge(account: Account, marker: str = MARK) -> int:
    n = 0
    for folder in (account.inbox, account.sent, account.drafts, account.trash, account.calendar,
                   account.tasks):
        try:
            items = list(folder.filter(Q(subject__icontains=marker)).only("id", "changekey"))
            if items:  # bulk_delete: some item types come back without an account
                account.bulk_delete(items)
                n += len(items)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
    return n


@pytest.fixture(scope="session", autouse=True)
def cleanup(request):
    yield
    accounts = [raw_account(USER, os.environ["EWS_IT_PASSWORD"])]
    if PEER:
        accounts.append(raw_account(PEER, os.environ["EWS_IT_PEER_PASSWORD"]))
    time.sleep(2)
    for a in accounts:
        _purge(a)
