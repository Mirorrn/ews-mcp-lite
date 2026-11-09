"""Tool registry and THE dispatcher. All policy lives in ``_guard``; handlers
contain none beyond the documented calendar re-check (events only leave the
mailbox when they notify attendees).

Every call goes through, in this order:

    kill-switch → tier → circuit breaker → startup gate → recipient guard
    → two-phase confirm → handler
"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .aliases import AliasStore
from .config import Settings
from .errors import Failure
from .ews import Gateway
from .policy import Confirmer, check_recipients

logger = logging.getLogger(__name__)

# kind: read | write | send | destructive.  "send" = something leaves the mailbox.
KIND_ANNOTATIONS = {
    "read": ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    "write": ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    "send": ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
}
CONFIRM_KINDS = ("send", "destructive")
RECIPIENT_ARGS = ("to", "cc", "bcc", "attendees")
# Flags that make an otherwise mailbox-local call send mail (create_event invitations).
SEND_FLAGS = ("send_invitations", "notify_attendees")

# A resolver turns call arguments into what a confirm token binds:
#   {"preview": {...}, "material": <anything JSON-able>, "recipients": [...]}
Resolver = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Context:
    settings: Settings
    gateway: Gateway
    aliases: AliasStore = field(default_factory=AliasStore)
    counters: dict[str, int] = field(default_factory=dict)
    registered: list[str] = field(default_factory=list)
    confirmer: Confirmer | None = None

    def __post_init__(self) -> None:
        if self.confirmer is None:
            self.confirmer = Confirmer(self.settings.send_confirm_secret, self.settings.confirm_ttl_seconds)

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
    needs_ews: bool = True
    resolve: Resolver | None = None

    @property
    def confirm(self) -> bool:
        return self.kind in CONFIRM_KINDS


class Registry:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.specs: list[ToolSpec] = []

    def tool(self, tier: str, kind: str, *, needs_ews: bool = True, resolve: Resolver | None = None):
        def deco(fn: Callable[..., Awaitable[dict[str, Any]]]):
            doc = (fn.__doc__ or "").strip()
            self.specs.append(ToolSpec(fn.__name__, tier, kind, fn, doc, needs_ews, resolve))
            return fn
        return deco

    def install(self, mcp: MCPServer) -> list[str]:
        ctx = self.ctx
        names: list[str] = []
        for spec in self.specs:
            if not ctx.settings.tier_allows(spec.tier):
                continue
            mcp.add_tool(_guard(ctx, spec), name=spec.name, description=_describe(spec),
                         annotations=KIND_ANNOTATIONS[spec.kind])
            names.append(spec.name)
        ctx.registered[:] = names
        return names


def _describe(spec: ToolSpec) -> str:
    if not spec.confirm:
        return spec.description
    return (spec.description + "\n\nTWO-PHASE: call once WITHOUT confirm_token to get a preview and a "
            "token (nothing is executed); show the preview to the user; after they approve, call again "
            "with the same arguments plus confirm_token.")


def _recipients_in(kwargs: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name in RECIPIENT_ARGS:
        value = kwargs.get(name)
        if isinstance(value, list):
            out += [v for v in value if isinstance(v, str)]
    return out


def _sends(spec: ToolSpec, kwargs: dict[str, Any]) -> bool:
    return spec.kind == "send" or any(kwargs.get(flag) for flag in SEND_FLAGS)


async def dispatch(ctx: Context, spec: ToolSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    s = ctx.settings
    token = kwargs.pop("confirm_token", None)
    # 1. kill-switch: nothing that leaves the mailbox, not even a preview
    if spec.kind == "send":
        ctx.require_send(spec.name)
    # 2. tier (defence in depth: above-tier tools are not registered at all)
    if not s.tier_allows(spec.tier):
        raise Failure("tier_blocked", f"{spec.name} needs tier '{spec.tier}'")
    if spec.needs_ews:
        # 3. circuit breaker
        ctx.gateway.circuit.check()
        # 4. startup gate
        ctx.gateway.gate()
    if spec.confirm and token:  # replayed/forged/expired tokens fail before any EWS work
        assert ctx.confirmer is not None
        ctx.confirmer.precheck(token, spec.name)
    # 5. recipient guard, only where something leaves the mailbox: argument recipients of a
    #    sending call, then resolved recipients (send_draft checks the draft's own). Drafts and
    #    free/busy lookups may name anyone; the guard catches them when they are sent.
    if _sends(spec, kwargs):
        check_recipients(_recipients_in(kwargs), s.allowlist, s.denylist, spec.name)
    resolved = None
    if spec.confirm or spec.resolve:
        resolved = await (spec.resolve(kwargs) if spec.resolve else _default_resolve(spec, kwargs))
        check_recipients(resolved.get("recipients", []), s.allowlist, s.denylist, spec.name)
    # 6. two-phase confirm
    if spec.confirm:
        assert ctx.confirmer is not None and resolved is not None
        if not token:
            tok, exp = ctx.confirmer.issue(spec.name, resolved["material"])
            return {"requires_confirmation": True,
                    "message": f"{spec.name} previewed; NOTHING was executed. Show the preview to the "
                               "user; after approval call again with the same arguments plus confirm_token.",
                    "preview": resolved["preview"], "confirm_token": tok, "expires_at": exp,
                    "ttl_seconds": s.confirm_ttl_seconds}
        ctx.confirmer.verify(token, spec.name, resolved["material"])
    # 7. handler (confirm_token already stripped)
    return await spec.fn(**kwargs)


async def _default_resolve(spec: ToolSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    args = {k: v for k, v in kwargs.items() if v is not None}
    return {"preview": {"tool": spec.name, "arguments": args}, "material": {"args": args},
            "recipients": []}


def _guard(ctx: Context, spec: ToolSpec):
    @functools.wraps(spec.fn)
    async def wrapper(**kwargs: Any) -> dict[str, Any]:
        ctx.counters[spec.name] = ctx.counters.get(spec.name, 0) + 1
        try:
            return await dispatch(ctx, spec, kwargs)
        except Failure as f:
            logger.info("tool %s refused/failed: %s", spec.name, f.code)
            raise ToolError(f.to_json()) from None
        except ValueError as e:
            raise ToolError(Failure("validation", str(e)).to_json()) from None
        except ToolError:
            raise
        except Exception as e:  # never leak a traceback to the model
            logger.exception("tool %s crashed", spec.name)
            raise ToolError(Failure("upstream_error", f"{type(e).__name__}: {e}"[:300]).to_json()) from None

    if spec.confirm:  # expose confirm_token in the schema; the handler never sees it
        sig = inspect.signature(spec.fn)
        extra = inspect.Parameter(
            "confirm_token", inspect.Parameter.KEYWORD_ONLY, default=None,
            annotation=Annotated[str | None, Field(description="Token from the preview call; omit to preview.")])
        params = [p.replace(kind=inspect.Parameter.KEYWORD_ONLY) for p in sig.parameters.values()]
        wrapper.__signature__ = sig.replace(parameters=[*params, extra])
        wrapper.__annotations__ = {**getattr(spec.fn, "__annotations__", {}),
                                   "confirm_token": extra.annotation}
    return wrapper
