"""The FastAPI host: /health, bearer auth and the mounted MCP endpoint."""

from fastapi.testclient import TestClient

from ews_mcp_lite.app import build_app
from tests.conftest import FakeGateway, make_settings

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _client(**kw):
    settings = make_settings(**kw)
    return TestClient(build_app(settings, FakeGateway(settings)), base_url="http://127.0.0.1:8000")


def test_health_is_public():
    with _client(mcp_api_key="secret") as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json()["tools"] == 23 and r.json()["send_enabled"] is False


def test_swagger_is_public_and_lists_only_health():
    with _client(mcp_api_key="secret") as c:
        docs = c.get("/docs")
        schema = c.get("/openapi.json")
        redoc = c.get("/redoc")
    assert docs.status_code == 200 and "swagger-ui" in docs.text
    assert schema.status_code == 200 and list(schema.json()["paths"]) == ["/health"]
    assert redoc.status_code != 200


def test_mcp_requires_bearer_when_key_set():
    with _client(mcp_api_key="secret") as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer wrong"}).status_code == 401
        ok = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer secret"})
    assert ok.status_code == 200 and "ews-mcp-lite" in ok.text


def test_mcp_mount_answers_initialize_without_key():
    with _client() as c:
        r = c.post("/mcp", json=INIT, headers=HEADERS)
    assert r.status_code == 200 and "serverInfo" in r.text
