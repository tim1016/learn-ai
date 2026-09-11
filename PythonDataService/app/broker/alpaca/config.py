"""Alpaca settings and mode agreement (Broker System v2, Layer 1).

Ports the IBKR three-layer safety pattern to Alpaca (V1 Goodness Inventory,
spec §10):

1. ``ALPACA_MODE`` selects paper/live; the default is ``paper``. ``live`` is
   admitted only when every ``ALPACA_LIVE_*`` envelope value is present (ADR
   0059 D1/D4); a validator raises otherwise, so a half-configured live mode
   cannot start the service.
2. The base URL is **derived** from the mode, never independently configurable,
   so a mode/URL mismatch cannot exist (the SDK is handed ``paper=is_paper``).
3. The runtime relies on Alpaca's own paper endpoint isolation — the paper base
   URL simply cannot reach live funds.

Credentials come from ``.env`` (never committed): ``ALPACA_API_KEY_ID``,
``ALPACA_API_SECRET_KEY``, ``ALPACA_MODE``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The registry key and ``{broker}`` path segment for this vendor.
BROKER_ID = "alpaca"
_SERVICE_ROOT = Path(__file__).resolve().parents[3]

# Base URL per mode. Derived, never independently configurable (spec §7).
_BASE_URL_BY_MODE: dict[str, str] = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}

# The envelope and ceremony settings a live boot must carry (ADR 0059 D4).
_LIVE_REQUIRED_FIELDS: tuple[str, ...] = (
    "live_loss_fraction",
    "live_loss_usd",
    "live_shadow_sessions",
    "live_arming_max_sessions",
    "live_xh_entry_bps",
    "live_xh_exit_bps",
)


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
    # paper | live. ADR 0059 D1: live is admitted only on mode agreement —
    # here, that every ALPACA_LIVE_* value is present. The activation record
    # and the broker-observed mode are checked where an authority is
    # constructed (select_active_clerk_runtime), never here.
    mode: Literal["paper", "live"] = "paper"
    clerk_dir: Path = _SERVICE_ROOT / "artifacts" / "alpaca_clerk"
    # A Paper twin can share the owner's one stock-data connection while
    # retaining its separate trading credentials and broker clock.
    market_status_upstream_url: HttpUrl | None = None

    # The live envelope and ceremony values (ADR 0059 D4). Required when
    # ``mode == "live"``; deliberately no defaults in code — a number nobody
    # chose must never bound real money, and neither may an infinite or
    # NaN one (`inf` satisfies `gt=0`). Sealed into the arming record by
    # slice 6; read here so the service refuses to boot live without them.
    live_loss_fraction: float | None = Field(default=None, gt=0, lt=1, allow_inf_nan=False)
    live_loss_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    live_shadow_sessions: int | None = Field(default=None, ge=1)
    live_arming_max_sessions: int | None = Field(default=None, ge=1)
    # Upper-bounded because 10 000 bps is 100 %: a sell allowance at or past it
    # floors the marketable anchor to zero or below, which is not a price. The
    # anchor refuses such a leg too (`EXTENDED_ANCHOR_UNPRICEABLE`); this stops
    # the configuration from loading at all.
    live_xh_entry_bps: float | None = Field(default=None, ge=0, lt=10_000, allow_inf_nan=False)
    live_xh_exit_bps: float | None = Field(default=None, ge=0, lt=10_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def _enforce_mode_agreement(self) -> AlpacaSettings:
        if self.market_status_upstream_url is not None:
            if self.mode != "paper":
                raise ValueError("Shared market-status sources are supported only for Paper workers.")
            if self.market_status_upstream_url.username or self.market_status_upstream_url.password:
                raise ValueError("Shared market-status URLs must not contain credentials.")
        if self.mode != "live":
            return self
        missing = [
            f"ALPACA_{name.upper()}"
            for name in _LIVE_REQUIRED_FIELDS
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                f"ALPACA_MODE=live requires {', '.join(missing)}; every live envelope "
                "value comes from the environment file and none has a default "
                "(ADR 0059 D4). Refusing to start."
            )
        return self

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def base_url(self) -> str:
        """Base URL derived from mode — the single source of the endpoint."""
        return _BASE_URL_BY_MODE[self.mode]


_settings: AlpacaSettings | None = None


def get_alpaca_settings() -> AlpacaSettings:
    """Return the process-wide Alpaca settings, instantiated on first use.

    Instantiation reads ``.env`` and validates mode agreement; it raises if
    credentials are missing or if live mode is set without every ALPACA_LIVE_*
    envelope value. Callers on the read path translate that into a contract
    error — the service still boots without credentials because settings are
    only read when an endpoint is hit.
    """
    global _settings
    if _settings is None:
        _settings = AlpacaSettings()
    return _settings


def reset_alpaca_settings_for_testing() -> None:
    """Drop cached settings so a test can rebind the environment."""
    global _settings
    _settings = None
