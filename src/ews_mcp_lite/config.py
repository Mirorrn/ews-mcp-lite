"""Environment-driven settings. Every knob defaults to the safe value."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Tier = Literal["read", "draft", "full"]
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

    # Safety
    ews_capability_tier: Tier = "draft"
    send_enabled: bool = False

    # Serving
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_api_key: str | None = None
    log_level: str = "INFO"

    def tier_allows(self, tier: str) -> bool:
        return TIER_RANK[tier] <= TIER_RANK[self.ews_capability_tier]
