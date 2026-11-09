import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from exchangelib.version import Build, Version
from mcp import Client

from ews_mcp_lite.app import build_server
from ews_mcp_lite.config import Settings
from ews_mcp_lite.ews import Gateway


@pytest.fixture
def anyio_backend():
    return "asyncio"


EXCHANGE_2016 = Version(build=Build(15, 1, 2507, 0), api_version="Exchange2016")


class FakeGateway(Gateway):
    """Gateway whose account is a MagicMock: no network, real error mapping."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.mock_account = MagicMock(name="account")
        self.mock_account.version = EXCHANGE_2016
        self.calls = 0
        self.state = "warm"  # unit tests start past the startup gate

    def account(self):
        self.calls += 1
        return self.mock_account


def make_settings(**kw: Any) -> Settings:
    base = {"ews_server_url": "https://mail.example.com/EWS/Exchange.asmx",
            "ews_email": "user@example.com", "ews_password": "x", "ews_tz": "Europe/Berlin"}
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def server_factory():
    def factory(**kw: Any):
        settings = make_settings(**kw)
        gateway = FakeGateway(settings)
        mcp, ctx = build_server(settings, gateway)
        return mcp, ctx, gateway
    return factory


async def call(mcp, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
    """Call a tool in memory -> (ok, structured result or parsed error JSON)."""
    async with Client(mcp) as client:
        res = await client.call_tool(name, args or {})
    if res.is_error:
        text = res.content[0].text
        return False, json.loads(text[text.index("{"):])
    return True, res.structured_content


async def confirmed(mcp, name: str, args: dict[str, Any]) -> Any:
    """Two-phase helper: preview, then confirm with the issued token."""
    ok, preview = await call(mcp, name, args)
    assert ok and preview["requires_confirmation"], preview
    ok, res = await call(mcp, name, {**args, "confirm_token": preview["confirm_token"]})
    assert ok, res
    return res
