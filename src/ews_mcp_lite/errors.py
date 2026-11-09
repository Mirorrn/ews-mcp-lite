"""A small error taxonomy. Tools raise ``Failure``; the registry turns it into an
MCP tool error whose text is a JSON object the model can act on."""

import json

CODES = (
    "validation",
    "not_found",
    "tier_blocked",
    "kill_switch",
    "recipient_blocked",
    "confirm_invalid",
    "auth_failed",
    "upstream_unavailable",
    "upstream_error",
)


class Failure(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None,
                 retry_after_s: int | None = None):
        assert code in CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retry_after_s = retry_after_s

    def to_json(self) -> str:
        body: dict = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.retry_after_s is not None:
            body["retry_after_s"] = self.retry_after_s
        return json.dumps(body, ensure_ascii=False)


def classify(exc: Exception) -> Failure:
    """Map an exchangelib / network exception onto the taxonomy."""
    if isinstance(exc, Failure):
        return exc
    name = type(exc).__name__
    text = f"{name}: {exc}"[:300]
    if name in ("UnauthorizedError", "ErrorAccessDenied") or "401" in str(exc)[:40]:
        return Failure("auth_failed", text, "Check EWS_USERNAME / EWS_PASSWORD.")
    if name in ("ErrorItemNotFound", "ErrorFolderNotFound", "ErrorInvalidIdMalformed",
                "DoesNotExist", "ErrorInvalidIdNotAnItemAttachmentId"):
        return Failure("not_found", text,
                       "The id is stale or wrong. Re-run the list/search tool and use a fresh id.")
    if name in ("TransportError", "ConnectionError", "ConnectTimeout", "ReadTimeout",
                "ErrorServerBusy", "RateLimitError", "TimeoutError", "SSLError"):
        return Failure("upstream_unavailable", text, "Exchange is unreachable or busy; retry later.")
    if name in ("ValueError", "TypeError", "InvalidTypeError", "InvalidEnumValue"):
        return Failure("validation", text)
    return Failure("upstream_error", text)
