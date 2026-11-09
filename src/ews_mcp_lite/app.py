"""FastAPI app: GET /health plus the MCP Streamable HTTP endpoint at /mcp.

The MCP SDK's Starlette app is mounted inside FastAPI. Starlette does not run a
mounted app's lifespan, so the host lifespan enters ``session_manager.run()``.
"""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer

from . import __version__
from .config import Settings
from .ews import Gateway
from .registry import Context, Registry
from .tools import MODULES

INSTRUCTIONS = (
    "Live Microsoft Exchange (EWS) mailbox access. Ids are short aliases (m1 message, d1 draft, "
    "e1 event, f1 folder, p1 person, k1 task, t1 thread): pass them back exactly. Aliases are "
    "lost when the server restarts; re-run the list/search tool if one is reported stale. "
    "Errors are JSON with a code and a hint. Send-class and destructive tools are two-phase: the "
    "first call only returns a preview and a confirm_token; show the preview to the user and call "
    "again with confirm_token only after they approve."
)


def build_server(settings: Settings, gateway: Gateway | None = None) -> tuple[MCPServer, Context]:
    ctx = Context(settings=settings, gateway=gateway or Gateway(settings))
    registry = Registry(ctx)
    for module in MODULES:
        module.register(registry)
    mcp = MCPServer("ews-mcp-lite", version=__version__, instructions=INSTRUCTIONS)
    registry.install(mcp)
    return mcp, ctx


def build_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or Settings()
    mcp, ctx = build_server(settings, gateway)
    mcp_app = mcp.streamable_http_app(host=settings.mcp_host)  # creates mcp.session_manager

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            tg.start_soon(ctx.gateway.warmup)  # never blocks startup; Exchange may be down
            async with mcp.session_manager.run():
                yield
            tg.cancel_scope.cancel()

    app = FastAPI(title="ews-mcp-lite", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        key = settings.mcp_api_key
        if key and request.url.path != "/health":
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
            if not hmac.compare_digest(token, key):
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__,
                "tier": settings.ews_capability_tier, "send_enabled": settings.send_enabled,
                "tools": len(ctx.registered), "connection": ctx.gateway.status()}

    app.mount("/", mcp_app)  # serves /mcp; declared routes above take precedence
    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(build_app(settings), host=settings.mcp_host, port=settings.mcp_port,
                log_level=settings.log_level.lower())
