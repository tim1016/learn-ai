"""Pydantic models for the ensure_data contract.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2

Spec-update corrections applied (post-plan review):
- ``include_lean_metadata`` field removed; LEAN metadata is an unconditional
  Phase 0 prerequisite, not gated by a flag.
- ``lean_image_digest`` is required (no default); it is the source of the
  LEAN-image-extracted session calendar and is mandatory for every request.
- ``'quote'`` in ``data_types`` requires ``'trade'`` to also be present;
  quote artifacts are derived from same-day trade bytes.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
_MAX_RANGE_YEARS = 5


class DataRunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    run_type: Literal["python_lab", "lean_lab"]
    requester: str | None = None
    strategy_execution_id: int | None = None

    market: Literal["usa"] = "usa"
    symbols: list[str] = Field(min_length=1)
    start_trading_date: date
    end_trading_date: date

    resolution: Literal["minute"] = "minute"
    data_types: list[Literal["trade", "quote"]] = ["trade"]
    price_adjustment_mode: Literal["raw"] = "raw"
    provider: Literal["polygon"] = "polygon"

    include_factor_files: bool = True
    include_map_files: bool = True
    # lean_image_digest is required — source of the LEAN-image-extracted
    # session calendar used by ensure_data's Phase 0 bootstrap.
    lean_image_digest: str

    force_refresh: bool = False
    fetch_timeout_seconds: int = Field(default=600, ge=10, le=7200)

    @model_validator(mode="after")
    def _validate(self) -> DataRunSpec:
        # Symbols: uppercase canonical.
        for sym in self.symbols:
            if not _SYMBOL_RE.match(sym):
                raise ValueError(f"symbol must match {_SYMBOL_RE.pattern}: {sym!r}")
        # Date ordering.
        if self.start_trading_date > self.end_trading_date:
            raise ValueError(f"start_trading_date {self.start_trading_date} > end_trading_date {self.end_trading_date}")
        # Range cap.
        delta_days = (self.end_trading_date - self.start_trading_date).days
        if delta_days > _MAX_RANGE_YEARS * 366:
            raise ValueError(f"range exceeds {_MAX_RANGE_YEARS}-year cap ({delta_days} days requested)")
        # Quote requires trade: quote artifacts are derived from same-day trade
        # bytes; without a source trade artifact, quote synthesis cannot proceed.
        if "quote" in self.data_types and "trade" not in self.data_types:
            raise ValueError(
                "'quote' in data_types requires 'trade' to also be present — "
                "quote artifacts are derived from same-day trade bytes; without "
                "a source trade artifact, quote synthesis cannot proceed."
            )
        return self


class ArtifactIdentity(BaseModel):
    """Internal identity tuple — what the catalog claim key looks like."""

    artifact_kind: Literal["time_series_bars", "factor_file", "map_file", "metadata"]
    market: str | None = None
    symbol: str | None = None
    trading_date: date | None = None
    resolution: Literal["minute", "hour", "daily"] | None = None
    data_type: Literal["trade", "quote"] | None = None
    provider: str
    price_adjustment_mode: str | None = None


class ArtifactRecord(BaseModel):
    id: int
    artifact_kind: str
    market: str | None
    symbol: str | None
    trading_date: date | None
    resolution: str | None
    data_type: str | None
    provider: str
    price_adjustment_mode: str | None
    data_contract_hash: str
    file_path: str
    file_sha256: str
    row_count: int | None
    first_bar_start_ms: int | None
    last_bar_start_ms: int | None


class ArtifactFailure(BaseModel):
    artifact_kind: str
    symbol: str | None
    trading_date: date | None
    data_type: str | None
    reason: Literal[
        "provider_auth_error",
        "provider_entitlement_error",
        "provider_rate_limited",
        "provider_api_error",
        "provider_no_data",
        "unknown_symbol",
        "validation_failed",
        "io_error",
        "lease_timeout",
        "fetch_timeout",
        "unsupported_resolution",
        "unsupported_artifact_kind",
        "corp_action_revision_mismatch",
        "data_contract_mismatch",
        "internal_error",
    ]
    detail: str | None = None
    provider_status_code: int | None = None
    attempt_count: int = 0


class NonSessionRecord(BaseModel):
    market: str
    trading_date: date
    reason: Literal["weekend", "market_holiday"]


class DataAvailabilityResult(BaseModel):
    request_id: UUID
    overall_status: Literal["complete", "partial", "failed"]
    lean_data_root_path: str
    data_availability_hash: str
    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    skipped_non_sessions: list[NonSessionRecord] = []
    fetched_artifact_count: int = 0
    reused_artifact_count: int = 0
    refreshed_artifact_count: int = 0
    completed_at_ms: int
    duration_ms: int
