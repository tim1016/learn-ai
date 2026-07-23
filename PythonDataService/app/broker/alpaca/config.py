"""Alpaca settings and paper-only safety (Broker System v2, Layer 1).

Ports the IBKR three-layer safety pattern to Alpaca (V1 Goodness Inventory,
spec §10):

1. ``ALPACA_MODE`` selects paper/live; the default is ``paper`` and phase 1
   **refuses** anything else — a validator raises, so a mis-set mode cannot
   start the service (design decision D7).
2. The base URL is **derived** from the mode, never independently configurable,
   so a mode/URL mismatch cannot exist (the SDK is handed ``paper=is_paper``).
3. The runtime relies on Alpaca's own paper endpoint isolation — the paper base
   URL simply cannot reach live funds.

Credentials come from ``.env`` (never committed): ``ALPACA_API_KEY_ID``,
``ALPACA_API_SECRET_KEY``, ``ALPACA_MODE``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The registry key and ``{broker}`` path segment for this vendor.
BROKER_ID = "alpaca"

# Base URL per mode. Derived, never independently configurable (spec §7).
_BASE_URL_BY_MODE: dict[str, str] = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}


def alpaca_configuration_error_detail(exc: ValidationError) -> str:
    """Return validation messages without echoing credential-bearing inputs."""
    messages = [str(error.get("msg", "")) for error in exc.errors()]
    return (
        "; ".join(message for message in messages if message)
        or "invalid Alpaca configuration"
    )


class AlpacaSettings(BaseSettings):
    """Alpaca client settings, env-var-backed (``ALPACA_*``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ALPACA_",
        case_sensitive=False,
        extra="ignore",
    )

    api_key_id: str = Field(min_length=1)
    api_secret_key: str = Field(min_length=1)
    # paper | live. Phase 1 refuses live; live enablement is a deliberate
    # future change (H3 in the HITL closeout), never a config accident.
    mode: Literal["paper", "live"] = "paper"

    @model_validator(mode="after")
    def _enforce_paper_only(self) -> AlpacaSettings:
        if self.mode != "paper":
            raise ValueError(
                "ALPACA_MODE must be 'paper' in phase 1. Live (real-money) "
                "access is a deliberate future change, not a config toggle. "
                "Refusing to start."
            )
        return self

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def base_url(self) -> str:
        """Base URL derived from mode — the single source of the endpoint."""
        return _BASE_URL_BY_MODE[self.mode]


_settings: AlpacaSettings | None = None


def get_alpaca_settings() -> AlpacaSettings:
    """Return the process-wide Alpaca settings, instantiated on first use.

    Instantiation reads ``.env`` and validates paper-only safety; it raises if
    credentials are missing or the mode is not ``paper``. Callers on the read
    path translate that into a contract error — the service still boots without
    credentials because settings are only read when an endpoint is hit.
    """
    global _settings
    if _settings is None:
        _settings = AlpacaSettings()
    return _settings


def reset_alpaca_settings_for_testing() -> None:
    """Drop cached settings so a test can rebind the environment."""
    global _settings
    _settings = None
