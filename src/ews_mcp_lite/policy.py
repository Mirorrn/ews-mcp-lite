"""Policy building blocks used by the dispatcher (registry._guard):
circuit breaker, recipient guard and two-phase confirm tokens."""

import base64
import fnmatch
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

from .errors import Failure

# Failures that mean "Exchange is unhealthy". Policy and caller errors
# (validation, not_found, kill_switch, confirm_invalid …) never count.
CIRCUIT_COUNTED = {"upstream_unavailable", "upstream_error", "auth_failed"}


class CircuitBreaker:
    def __init__(self, threshold: int, open_seconds: int, clock=time.monotonic):
        self.threshold = max(1, threshold)
        self.open_seconds = open_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self.failures = 0
        self.open_until: float | None = None

    def check(self) -> None:
        with self._lock:
            if self.open_until is None:
                return
            remaining = self.open_until - self._clock()
            if remaining <= 0:  # half-open: let the next call through
                self.open_until = None
                return
        raise Failure("upstream_unavailable",
                      f"Exchange calls are paused after {self.failures} consecutive failures.",
                      "Wait and retry, or call get_server_status.", retry_after_s=int(remaining) + 1)

    def record(self, failure: Failure | None) -> None:
        with self._lock:
            if failure is None:
                self.failures, self.open_until = 0, None
            elif failure.code in CIRCUIT_COUNTED:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.open_until = self._clock() + self.open_seconds

    def status(self) -> dict[str, Any]:
        with self._lock:
            left = None if self.open_until is None else max(0, int(self.open_until - self._clock()))
            return {"state": "open" if left else "closed", "consecutive_failures": self.failures,
                    "retry_after_s": left}


def check_recipients(addresses: list[str], allow: list[str], deny: list[str], where: str) -> None:
    """Raise recipient_blocked if any address hits the denylist or misses a non-empty allowlist."""
    for addr in addresses:
        a = (addr or "").strip().lower()
        if not a:
            continue
        if any(fnmatch.fnmatchcase(a, g) for g in deny):
            raise Failure("recipient_blocked", f"{where}: {addr} is on the recipient denylist.")
        if allow and not any(fnmatch.fnmatchcase(a, g) for g in allow):
            raise Failure("recipient_blocked", f"{where}: {addr} is not on the recipient allowlist.",
                          "Only addresses matching EWS_RECIPIENT_ALLOWLIST may be used.")


def digest(material: Any) -> str:
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class Confirmer:
    """HMAC-SHA256 tokens binding (tool, content digest, expiry). Single use."""

    def __init__(self, secret: str | None, ttl_seconds: int, clock=time.time):
        self._key = (secret or secrets.token_hex(32)).encode()
        self.ttl = ttl_seconds
        self._clock = clock
        self._used: dict[str, float] = {}  # nonce -> expiry
        self._lock = threading.Lock()

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._key, body, hashlib.sha256).hexdigest()

    def issue(self, tool: str, material: Any) -> tuple[str, int]:
        exp = int(self._clock()) + self.ttl
        body = json.dumps({"t": tool, "d": digest(material), "e": exp, "n": secrets.token_hex(8)},
                          sort_keys=True).encode()
        return base64.urlsafe_b64encode(body).decode() + "." + self._sign(body), exp

    def verify(self, token: str, tool: str, material: Any) -> None:
        claims = self.precheck(token, tool)
        bad = "Call the tool again WITHOUT confirm_token to get a fresh preview and token."
        if not hmac.compare_digest(claims.get("d", ""), digest(material)):
            raise Failure("confirm_invalid",
                          "The content changed since the preview (arguments or draft edited).", bad)
        with self._lock:
            if claims["n"] in self._used:
                raise Failure("confirm_invalid", "confirm_token was already used.", bad)
            self._used[claims["n"]] = claims["e"]

    def precheck(self, token: str, tool: str) -> dict[str, Any]:
        """Signature, tool, expiry and reuse; no content check. Runs before any EWS work."""
        bad = "Call the tool again WITHOUT confirm_token to get a fresh preview and token."
        try:
            b64, sig = token.rsplit(".", 1)
            body = base64.urlsafe_b64decode(b64.encode())
            claims = json.loads(body)
        except Exception:  # noqa: BLE001 - any parse problem is just an invalid token
            raise Failure("confirm_invalid", "confirm_token is malformed.", bad) from None
        if not hmac.compare_digest(sig, self._sign(body)):
            raise Failure("confirm_invalid", "confirm_token signature is invalid.", bad)
        if claims.get("t") != tool:
            raise Failure("confirm_invalid", f"confirm_token was issued for {claims.get('t')}, not {tool}.", bad)
        now = self._clock()
        if claims.get("e", 0) < now:
            raise Failure("confirm_invalid", "confirm_token has expired.", bad)
        with self._lock:
            for n, e in list(self._used.items()):
                if e < now:
                    del self._used[n]
            if claims.get("n") in self._used:
                raise Failure("confirm_invalid", "confirm_token was already used.", bad)
        return claims
