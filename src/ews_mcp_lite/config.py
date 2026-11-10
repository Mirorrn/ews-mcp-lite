"""Environment-driven settings. Every knob defaults to the safe value."""

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Tier = Literal["read", "draft", "full"]
ServerType = Literal["exchange", "grommunio"]
SearchMode = Literal["aqs", "restriction"]
TIER_RANK = {"read": 0, "draft": 1, "full": 2}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # Exchange
    ews_server_url: str = ""
    ews_email: str = ""
    ews_username: str | None = None
    ews_password: str | None = None
    ews_insecure_skip_verify: bool = False
    ews_tz: str = "UTC"
    request_timeout: int = 30
    # What the EWS endpoint really is. It decides search behaviour up front, with no
    # live probing: exchange = AQS via the search index, body Contains on item:Body;
    # grommunio = restrictions only (gromox ignores QueryString), body via PR_BODY.
    ews_server_type: ServerType = "exchange"
    # Optional override of the free-text search path; empty = derived from the server type.
    ews_search_mode: SearchMode | None = None

    # Safety
    ews_capability_tier: Tier = "draft"
    send_enabled: bool = False
    ews_recipient_allowlist: str = ""  # comma-separated fnmatch globs; empty = allow all
    ews_recipient_denylist: str = ""
    send_confirm_secret: str | None = None  # empty = random per process
    confirm_ttl_seconds: int = 600

    # Reliability
    circuit_failure_threshold: int = 5
    circuit_open_seconds: int = 60
    warmup_max_backoff_seconds: int = 60

    # Serving
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_api_key: str | None = None
    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _empty_is_unset(cls, data):
        if isinstance(data, dict) and data.get("ews_search_mode") == "":
            data = {**data, "ews_search_mode": None}
        return data

    @model_validator(mode="after")
    def _check_search(self):
        if self.ews_server_type == "grommunio" and self.ews_search_mode == "aqs":
            raise ValueError("EWS_SEARCH_MODE=aqs cannot work with EWS_SERVER_TYPE=grommunio: "
                             "gromox ignores QueryString and returns every item")
        return self

    @property
    def search_mode(self) -> SearchMode:
        """Free-text search path: the explicit override, else what the server type supports."""
        return self.ews_search_mode or ("restriction" if self.ews_server_type == "grommunio" else "aqs")

    @property
    def body_fields(self) -> tuple[str, ...]:
        """Body properties for Contains restrictions, in the order to try them.
        grommunio has no item:Body mapping (E-3224) but searches the MAPI PR_BODY."""
        return ("pr_body",) if self.ews_server_type == "grommunio" else ("body", "pr_body")

    @staticmethod
    def _globs(raw: str) -> list[str]:
        return [g.strip().lower() for g in (raw or "").split(",") if g.strip()]

    @property
    def allowlist(self) -> list[str]:
        return self._globs(self.ews_recipient_allowlist)

    @property
    def denylist(self) -> list[str]:
        return self._globs(self.ews_recipient_denylist)

    def tier_allows(self, tier: str) -> bool:
        return TIER_RANK[tier] <= TIER_RANK[self.ews_capability_tier]
