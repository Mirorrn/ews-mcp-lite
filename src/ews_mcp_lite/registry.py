"""Tool registry: tier filtering, the send kill-switch and error mapping live here
and only here. Handlers contain no policy beyond the documented calendar
re-check (events only leave the mailbox when they notify attendees)."""

import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .aliases import AliasStore
from .config import Settings
from .errors import Failure
from .ews import Gateway

logger = logging.getLogger(__name__)

# kind: read | write | send | destructive.  "send" = something leaves the mailbox.
KIND_ANNOTATIONS = {
    "read": ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    "write": ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    "send": ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
}


@dataclass
class Context:
    settings: Settings
    gateway: Gateway
    aliases: AliasStore = field(default_factory=AliasStore)
    counters: dict[str, int] = field(default_factory=dict)
    registered: list[str] = field(default_factory=list)

    @property
    def tz(self) -> str:
        return self.settings.ews_tz

    def require_send(self, what: str) -> None:
        if not self.settings.send_enabled:
            raise Failure("kill_switch", f"{what} is blocked: SEND_ENABLED=false on this server.",
                          "Save a draft instead, or ask the operator to enable sending.")


@dataclass
class ToolSpec:
    name: str
    tier: str
    kind: str
    fn: Callable[..., Awaitable[dict[str, Any]]]
    description: str


class Registry:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.specs: list[ToolSpec] = []

    def tool(self, tier: str, kind: str):
        def deco(fn: Callable[..., Awaitable[dict[str, Any]]]):
            doc = (fn.__doc__ or "").strip()
            self.specs.append(ToolSpec(fn.__name__, tier, kind, fn, doc))
            return fn
        return deco

    def install(self, mcp: MCPServer) -> list[str]:
        ctx = self.ctx
        names: list[str] = []
        for spec in self.specs:
            if not ctx.settings.tier_allows(spec.tier):
                continue
            mcp.add_tool(_guard(ctx, spec), name=spec.name, description=spec.description,
                         annotations=KIND_ANNOTATIONS[spec.kind])
            names.append(spec.name)
        ctx.registered[:] = names
        return names


def _guard(ctx: Context, spec: ToolSpec):
    @functools.wraps(spec.fn)
    async def wrapper(**kwargs: Any) -> dict[str, Any]:
        ctx.counters[spec.name] = ctx.counters.get(spec.name, 0) + 1
        try:
            if not ctx.settings.tier_allows(spec.tier):  # defence in depth
                raise Failure("tier_blocked", f"{spec.name} needs tier '{spec.tier}'")
            if spec.kind == "send":
                ctx.require_send(spec.name)
            return await spec.fn(**kwargs)
        except Failure as f:
            logger.info("tool %s failed: %s", spec.name, f.code)
            raise ToolError(f.to_json()) from None
        except ValueError as e:
            raise ToolError(Failure("validation", str(e)).to_json()) from None
        except ToolError:
            raise
        except Exception as e:  # never leak a traceback to the model
            logger.exception("tool %s crashed", spec.name)
            raise ToolError(Failure("upstream_error", f"{type(e).__name__}: {e}"[:300]).to_json()
                            ) from None
    return wrapper
