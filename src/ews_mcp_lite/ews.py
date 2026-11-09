"""Lazy, thread-safe exchangelib account plus a helper that runs blocking EWS
work off the event loop and maps exceptions onto the error taxonomy.

Never pin ``auth_type``: exchangelib's auto-negotiation is what works against
on-prem Exchange (Basic/NTLM/Negotiate are picked from the server's challenge).
"""

import logging
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

compat.apply()
logger = logging.getLogger(__name__)
T = TypeVar("T")


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._account: Any = None
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_success: float | None = None
        if settings.ews_insecure_skip_verify:
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

    def _build_account(self) -> Any:
        s = self.settings
        if not s.ews_server_url or not s.ews_email:
            raise Failure("validation", "EWS_SERVER_URL and EWS_EMAIL must be configured")
        config = Configuration(
            service_endpoint=s.ews_server_url,
            credentials=Credentials(s.ews_username or s.ews_email, s.ews_password or ""),
            max_connections=4,
        )
        return Account(
            primary_smtp_address=s.ews_email,
            config=config,
            autodiscover=False,
            access_type=DELEGATE,
            default_timezone=ews_tz(s.ews_tz),
        )

    def account(self) -> Any:
        with self._lock:
            if self._account is None:
                self._account = self._build_account()
            return self._account

    def reset(self) -> None:
        with self._lock:
            self._account = None

    def run_sync(self, fn: Callable[[Any], T]) -> T:
        try:
            result = fn(self.account())
        except Failure:
            raise
        except Exception as exc:
            failure = classify(exc)
            self.last_error = failure.message
            if failure.code in ("auth_failed", "upstream_unavailable"):
                self.reset()
            raise failure from exc
        self.last_success = time.time()
        self.last_error = None
        return result

    async def call(self, fn: Callable[[Any], T]) -> T:
        return await anyio.to_thread.run_sync(self.run_sync, fn)

    def status(self) -> dict[str, Any]:
        age = int(time.time() - self.last_success) if self.last_success else None
        state = "unknown" if age is None and not self.last_error else (
            "error" if self.last_error else "ok")
        return {"state": state, "last_error": self.last_error, "last_success_age_s": age}
