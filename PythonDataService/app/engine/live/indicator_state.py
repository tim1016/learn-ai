"""Indicator state persistence — envelope, payload, policy, receipt, validation.

Generic envelope, strategy-specific payload. The envelope's identity
fields (strategy_key, symbol, consolidator_period_min) double as the
sidecar's lookup key on the filesystem.

The validation ladder (consumed by hydrate(), implemented in this
module in a later task) runs six checks in order; first failure stops
and populates ``ValidationResult.failure_reason``. See
``docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md``
§4.1 for the ladder definition.

Decimal-safe: indicator internal numeric state serializes as quoted
strings in the payload (passed through as ``dict[str, Any]`` from this
module's POV; per-strategy ``validate_state_payload`` enforces the
shape). int64 ms UTC for every timestamp at the wire boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HydratePolicy(StrEnum):
    """Tri-state policy for indicator-state hydration on start.

    Default for the B2 dry-run gate and paper-week operation is REQUIRE.
    OPTIONAL is for seed days. DISABLED is the explicit operator escape
    hatch (--allow-cold-start) that skips the read entirely but still
    writes at end-of-session so today seeds tomorrow.
    """

    REQUIRE = "require"
    OPTIONAL = "optional"
    DISABLED = "disabled"


FailureReason = Literal[
    "disabled_by_operator",
    "missing",
    "schema_mismatch",
    "identity_mismatch",
    "calendar_stale",
    "payload_mismatch",
    "indicators_unready",
    "lifecycle_not_flat",
]


class ValidationResult(BaseModel):
    """Per-check booleans + the first failure reason."""

    model_config = ConfigDict(frozen=True)

    schema_version_ok: bool = True
    identity_ok: bool = True
    calendar_ok: bool = True
    payload_shape_ok: bool = True
    indicators_ready_ok: bool = True
    lifecycle_flat_ok: bool = True
    failure_reason: FailureReason | None = None

    @classmethod
    def all_passed(cls) -> ValidationResult:
        return cls()

    @classmethod
    def failed(cls, reason: FailureReason, **flag_overrides: bool) -> ValidationResult:
        return cls(failure_reason=reason, **flag_overrides)


CapturedReason = Literal["force_flat", "shutdown"]


class IndicatorStateEnvelope(BaseModel):
    """Generic envelope wrapping a strategy-specific payload.

    Identity tuple = (strategy_key, symbol, consolidator_period_min).
    Used both as the sidecar's filesystem key and as the validation
    ladder's identity check (#2).

    Timestamps are int64 ms UTC. Payload is opaque to this module —
    each strategy is responsible for its own payload shape via
    ``validate_state_payload``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_key: str
    symbol: str
    consolidator_period_min: int = Field(gt=0)
    last_consolidated_bar_end_ms: int = Field(gt=0)
    captured_at_ms: int = Field(gt=0)
    captured_reason: CapturedReason
    code_sha: str
    strategy_spec_sha: str
    payload: dict[str, Any]


class HydrationReceipt(BaseModel):
    """Per-run forensic record of what happened at hydrate time.

    Always written, regardless of accepted=true/false. The reconcile
    hash manifest picks it up if present.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    hydrated_at_ms: int
    policy: HydratePolicy
    global_path: str
    global_sha256: str | None
    accepted: bool
    strategy_key: str
    symbol: str
    consolidator_period_min: int
    sidecar_last_consolidated_bar_end_ms: int | None = None
    expected_prev_session_close_ms: int | None = None
    calendar: str = "NYSE"
    validation: ValidationResult


class IndicatorStateHydrationError(RuntimeError):
    """Raised when hydrate() is called under REQUIRE policy and validation fails.

    Carries the receipt the runner just wrote so callers can surface
    the failure reason without re-reading the file.
    """

    def __init__(self, receipt: HydrationReceipt) -> None:
        self.receipt = receipt
        super().__init__(
            f"indicator state hydration failed under {receipt.policy.value} policy: {receipt.validation.failure_reason}"
        )
