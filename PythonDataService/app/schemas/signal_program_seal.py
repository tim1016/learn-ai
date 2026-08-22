"""Versioned immutable identity for one configured Signal Program.

The legacy bot configuration hash remains untouched.  A v2 seal is appended
beside that artifact and binds the resolved signal semantics to the exact bot
configuration that may consume them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.action_plan import ActionPlan

# Canonical home for "how was this parameter's effective value chosen" (CLAUDE.md
# guiding philosophy #5 — one source of truth per concept). Every other module
# that needs this concept imports the alias rather than repeating the Literal.
ParameterOrigin = Literal["registered_default", "deploy_override", "deployment_symbol"]


class ResolvedSignalParameter(BaseModel):
    """One effective parameter with its unit and deploy-time origin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str | int | float | bool
    unit: str
    origin: ParameterOrigin


class SignalDataContract(BaseModel):
    """Closed source-series and bar-semantics contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Qualification lineage: the source that produced the golden EvaluationTrace
    # corpus this program's ``golden_trace_root`` pins — ``polygon`` for
    # ema-crossover-signal/v1, whose corpus comes from the offline
    # ``PolygonReplayMarketDataFeed``. This is NOT an authorization for which
    # live feed a running bot may consume, and nothing treats it as one: live
    # bars come from the single feed wired in ``app/main.py`` and are stamped
    # ``feed_id="ibkr"``. The two differ by design. See PRD §11.6 — and note
    # that introducing a second live provider would make that ambiguity a real
    # safety hole and require revisiting this decision.
    provider: str
    symbol: str
    base_timeframe_ms: int = Field(gt=0)
    decision_timeframe_ms: int = Field(gt=0)
    timestamp_contract: Literal["int64_ms_utc"] = "int64_ms_utc"
    bar_semantics: Literal["closed_end_exclusive"] = "closed_end_exclusive"
    revision_policy: Literal["exact_retained_source_bar"] = "exact_retained_source_bar"


class SignalClockContract(BaseModel):
    """Calendar, warmup, session, pause, and replay semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar: Literal["XNYS"] = "XNYS"
    session_timezone: Literal["America/New_York"] = "America/New_York"
    use_rth: bool
    early_close_policy: Literal["calendar_session_close"] = "calendar_session_close"
    warmup_lookback_days: int = Field(ge=0)
    pause_policy: Literal["OBSERVE_ONLY"] = "OBSERVE_ONLY"
    replay_protocol: Literal["retained_source_bars_in_decision_clock_order"] = (
        "retained_source_bars_in_decision_clock_order"
    )


class ConfiguredSignalProgramSeal(BaseModel):
    """Inner seal: the exact semantic signal program selected by the user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    program_key: str
    program_version: str
    golden_trace_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: dict[str, ResolvedSignalParameter]
    parameters_match_validated_settings: bool
    data: SignalDataContract
    clock: SignalClockContract

    def semantic_hash(self) -> str:
        return semantic_payload_hash(self.model_dump(mode="json"))


class SealedBotProgram(BaseModel):
    """Outer seal: signal identity plus execution-plan and validation choice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    strategy_instance_id: str
    configured_signal: ConfiguredSignalProgramSeal
    configured_signal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    broker: str
    sealed_account_id: str
    mode: Literal["log_only", "dry_run", "trade"]
    action_plan: ActionPlan
    quantity: int = Field(gt=0)
    carryover_policy: Literal["FORBID", "ALLOW"]
    validation_event_id: str
    validation_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_at_ms: int = Field(ge=0)
    bot_configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_nested_hashes(self) -> SealedBotProgram:
        if self.configured_signal.semantic_hash() != self.configured_signal_hash:
            raise ValueError("configured signal hash does not match its payload")
        payload = self.model_dump(mode="json", exclude={"bot_configuration_hash"})
        if semantic_payload_hash(payload) != self.bot_configuration_hash:
            raise ValueError("bot configuration hash does not match its payload")
        return self


def seal_bot_program(**values: Any) -> SealedBotProgram:
    """Build the self-hashed outer seal without exposing hashing order."""
    payload = {**values, "schema_version": 2}
    return SealedBotProgram(
        **payload,
        bot_configuration_hash=semantic_payload_hash(payload),
    )


def _hash_json_default(value: Any) -> Any:
    """``json.dumps`` fallback: serialize a nested model, or fail loudly.

    Must raise rather than return the ``TypeError`` — returning it hands
    ``json.dumps`` a fresh non-serializable object, which calls this
    function again and recurses until ``RecursionError``.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"unsupported semantic value: {type(value).__name__}")


def semantic_payload_hash(payload: object) -> str:
    """Canonical content hash for a JSON-serializable v2 seal payload.

    The single hashing primitive for the v2 seal family — every seal
    boundary (this module and ``app.services.signal_program_admission``)
    calls this function so semantic identity always hashes the same
    encoding.
    """
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_hash_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ConfiguredSignalProgramSeal",
    "ResolvedSignalParameter",
    "SealedBotProgram",
    "SignalClockContract",
    "SignalDataContract",
    "seal_bot_program",
    "semantic_payload_hash",
]
