"""Lazy, thread-safe exchangelib account, connection state + warmup, and a
helper that runs blocking EWS work off the event loop and maps exceptions
onto the error taxonomy.

Never pin ``auth_type``: exchangelib's auto-negotiation is what works against
on-prem Exchange (Basic/NTLM/Negotiate are picked from the server's challenge).

Connection states: ``connecting`` (no successful contact yet; warmup retrying),
``warm`` (warmup succeeded), ``failed`` (authentication rejected; warmup stopped).
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
from exchangelib import DELEGATE, Account, Configuration, Credentials
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

from . import compat
from .config import Settings
from .dates import ews_tz
from .errors import Failure, classify
from .policy import CircuitBreaker

compat.apply()
logger = logging.getLogger(__name__)
T = TypeVar("T")

CONNECTING, WARM, FAILED = "connecting", "warm", "failed"
GROMOX_DEFAULT_BUILD = "15.0.847.4040"  # gromox ews.cfg reported_server_version default


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._account: Any = None
        self._lock = threading.Lock()
        self.state = CONNECTING
        self.attempts = 0
        self.last_error: str | None = None
        self.last_success: float | None = None
        self.circuit = CircuitBreaker(settings.circuit_failure_threshold, settings.circuit_open_seconds)
        if settings.ews_insecure_skip_verify:
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    # ------------------------------------------------------------ account

    def _build_account(self) -> Any:
        s = self.settings
        if not s.ews_server_url or not s.ews_email:
            raise Failure("validation", "EWS_SERVER_URL and EWS_EMAIL must be configured")
        config = Configuration(
            service_endpoint=s.ews_server_url,
            credentials=Credentials(s.ews_username or s.ews_email, s.ews_password or ""),
            max_connections=4,
        )
        return Account(primary_smtp_address=s.ews_email, config=config, autodiscover=False,
                       access_type=DELEGATE, default_timezone=ews_tz(s.ews_tz))

    def account(self) -> Any:
        with self._lock:
            if self._account is None:
                self._account = self._build_account()
            return self._account

    def reset(self) -> None:
        with self._lock:
            self._account = None

    # ------------------------------------------------------------ calls

    def _execute(self, fn: Callable[[Any], T]) -> T:
        try:
            result = fn(self.account())
        except Exception as exc:
            failure = classify(exc)
            self.last_error = failure.message
            self.circuit.record(failure)
            if failure.code in ("auth_failed", "upstream_unavailable"):
                self.reset()
            if failure is exc:
                raise
            raise failure from exc
        self.circuit.record(None)
        self.last_success = time.time()
        self.last_error = None
        return result

    def run_sync(self, fn: Callable[[Any], T]) -> T:
        return self._execute(fn)

    async def call(self, fn: Callable[[Any], T]) -> T:
        return await anyio.to_thread.run_sync(self._execute, fn)

    # ------------------------------------------------------------ startup

    def connect(self) -> bool:
        """One blocking connection attempt (a real round trip). Updates state."""
        self.attempts += 1
        try:
            version = self._execute(lambda account: (account.inbox.refresh(), account.version)[1])
        except Failure as f:
            if f.code == "auth_failed":
                self.state = FAILED
            logger.warning("EWS connect attempt %d failed: %s", self.attempts, f.message)
            return False
        self.state = WARM
        build = str(getattr(version, "build", "?"))
        s = self.settings
        logger.info("EWS connection is warm (server build %s, api %s; EWS_SERVER_TYPE=%s, search=%s)",
                    build, getattr(version, "api_version", "?"), s.ews_server_type, s.search_mode)
        if build == GROMOX_DEFAULT_BUILD and s.ews_server_type != "grommunio":
            logger.warning("server reports build %s, the gromox (grommunio) default, but "
                           "EWS_SERVER_TYPE=%s: AQS search would return unfiltered results there. "
                           "Set EWS_SERVER_TYPE=grommunio if this is grommunio.", build, s.ews_server_type)
        return True

    async def warmup(self) -> None:
        """Background loop: retry with exponential backoff + jitter until warm or failed."""
        backoff = 1.0
        while self.state == CONNECTING:
            if await anyio.to_thread.run_sync(self.connect):
                return
            if self.state == FAILED:
                return
            await anyio.sleep(random.uniform(0.5, 1.0) * backoff)
            backoff = min(backoff * 2, self.settings.warmup_max_backoff_seconds)

    def gate(self) -> None:
        """Startup gate: fail fast instead of hanging while Exchange is not reached yet."""
        if self.state == CONNECTING:
            raise Failure("upstream_unavailable", "Still connecting to Exchange.",
                          "Call get_server_status to see the connection state, then retry.",
                          retry_after_s=5)
        if self.state == FAILED:
            raise Failure("auth_failed", f"Exchange rejected the credentials: {self.last_error}",
                          "Fix EWS_USERNAME / EWS_PASSWORD and restart the server.")

    def status(self) -> dict[str, Any]:
        age = int(time.time() - self.last_success) if self.last_success else None
        return {"state": self.state, "attempts": self.attempts, "last_error": self.last_error,
                "last_success_age_s": age, "circuit": self.circuit.status()}
