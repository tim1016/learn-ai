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


class SignalSeriesContract(BaseModel):
    """One named signal series a program consumes off the sealed data contract.

    PRD §10.1/§11.1: "every signal series, field, and decision stream." A
    program's ``data`` contract seals the *shared* provider/symbol/timeframe
    pair every series reads from; this seals each named series drawn from
    that shared stream — e.g. ``ema_fast``, ``ema_slow``, and ``rsi`` all read
    ``field="close"`` off the same 15-minute decision bar. ``warmup_bars`` is
    the series' own ``is_ready`` threshold (PRD §11.1 "readiness"): the
    ``app.engine.indicators`` base class exposes ``is_ready`` as
    ``samples >= period``, so an EMA's ``warmup_bars`` equals its period; RSI
    overrides this to ``period + 1`` (one extra sample for the first delta).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    indicator: str
    field: Literal["close"]
    period: int = Field(gt=0)
    warmup_bars: int = Field(gt=0)


DuplicatePolicy = Literal["reject_conflicting_payload_else_idempotent"]
OutOfOrderPolicy = Literal["reject_non_monotonic_end_ms"]
GapPolicy = Literal["tolerated_not_filled"]
HistoryLiveWatermarkPolicy = Literal["history_before_live_monotonic_seam"]
SessionCloseOwnershipPolicy = Literal["single_router_forced_flush_at_session_close"]


class SignalBarIntegrityContract(BaseModel):
    """Duplicate/gap/out-of-order, watermark, and session-close ownership facts.

    PRD §11.1 lists these beside ``revision_policy`` (``SignalDataContract``)
    as sealed identity, not runtime evidence. Today every field is a single
    system-wide policy — not a per-program choice — enforced by two modules
    outside this schema:

    * ``app.services.source_bar_ledger.SourceBarLedger`` fails closed on a
      same-identity-different-payload bar (``SOURCE_BAR_IDENTITY_CONFLICT``),
      is idempotent on an exact repeat, fails closed on a non-monotonic
      ``end_ms`` (``SOURCE_BAR_NON_MONOTONIC_LIVE``/``_HISTORY``), tolerates a
      missing bar rather than filling or rejecting it, and refuses
      ``append_history`` once live delivery has started for that
      provider/symbol seam (``SOURCE_BAR_HISTORY_AFTER_LIVE`` — the
      history/live watermark).
    * ``app.services.bot_trade_strategy._signal_strategy_evaluations`` is the
      one router that owns bucket closing: it force-flushes the trailing
      partial consolidator bucket exactly at the canonical calendar's
      ``session_close_ms_utc`` boundary rather than leaving it stranded
      until the next session's bars arrive (FR-011).

    A second policy would need its own Literal member and a per-program field
    here, not a silent default change on these.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    duplicate_policy: DuplicatePolicy = "reject_conflicting_payload_else_idempotent"
    out_of_order_policy: OutOfOrderPolicy = "reject_non_monotonic_end_ms"
    gap_policy: GapPolicy = "tolerated_not_filled"
    history_live_watermark_policy: HistoryLiveWatermarkPolicy = "history_before_live_monotonic_seam"
    session_close_ownership: SessionCloseOwnershipPolicy = "single_router_forced_flush_at_session_close"


class ExitEligibilityContract(BaseModel):
    """Level/countdown exit rule — the evidence future carryover work would read.

    PRD §11.1: "level/countdown exit eligibility evidence where carryover may
    later be considered." ``ema_crossover_signal`` exits on a fixed
    decision-clock countdown (``EmaCrossoverSignalAlgorithm.commit_signal_decision``
    sets ``_bars_until_exit = 5`` at entry). ``countdown_state_persistable``
    records a real, checked fact about today's implementation, not an
    aspiration: ``EmaCrossoverSignalAlgorithm.report_state_for_persistence``
    returns ``None`` whenever the strategy is mid-position, so an in-flight
    countdown cannot currently survive a Pause/Resume — carryover work must
    either change that or treat mid-countdown Resume as unsupported.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: Literal["fixed_bar_count_countdown"] = "fixed_bar_count_countdown"
    countdown_decision_clocks: int = Field(gt=0)
    countdown_state_persistable: bool


class NumericalProvenanceContract(BaseModel):
    """The Math Provenance Contract (CLAUDE.md #2/#5), sealed as program identity.

    Mirrors the ``Formula``/``Reference``/``Canonical implementation``/
    ``Validated against`` block already required by the
    ``learn-ai-validation`` skill and present in
    ``ema_crossover_signal.py``'s own module docstring; this is that same
    fact, made part of the immutable seal rather than living only in prose
    that could drift unnoticed. ``tolerance_atol``/``tolerance_rtol`` are
    ``None`` at ``equivalence_level="bit_exact"`` — the trace/decision
    identity in ``signal_program.py`` is Decimal-exact and SHA-256-compared,
    not tolerance-compared; the documented ``1e-9`` absolute tolerance in
    ``docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md``
    applies one level down, to the EMA/RSI *value* parity against LEAN.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    formula: str
    reference: str
    canonical_implementation: str
    validated_against: str
    equivalence_level: Literal["bit_exact", "strict_float", "behavioral"]
    tolerance_atol: float | None = None
    tolerance_rtol: float | None = None
    parity_fixture_ids: tuple[str, ...] = ()


class SignalClockContract(BaseModel):
    """Calendar, warmup, session, pause, and replay semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar: Literal["XNYS"] = "XNYS"
    session_timezone: Literal["America/New_York"] = "America/New_York"
    use_rth: bool
    early_close_policy: Literal["calendar_session_close"] = "calendar_session_close"
    warmup_lookback_days: int = Field(ge=0)
    pause_policy: Literal["OBSERVE_ONLY"] = "OBSERVE_ONLY"
    # The complete legal evaluation-mode set (PRD §11.1 "readiness and DECIDE
    # semantics"), sourced from ``app.engine.strategy.signal_program.EvaluationMode``
    # — every source bar an active program observes captures exactly one of
    # these two, never a third. ``pause_policy`` above names which mode Pause
    # forces; this seals the complete set DECIDE is drawn from.
    evaluation_modes: tuple[Literal["DECIDE", "OBSERVE_ONLY"], ...] = ("DECIDE", "OBSERVE_ONLY")
    replay_protocol: Literal["retained_source_bars_in_decision_clock_order"] = (
        "retained_source_bars_in_decision_clock_order"
    )


class ConfiguredSignalProgramSeal(BaseModel):
    """Inner seal: the exact semantic signal program selected by the user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    program_key: str
    program_version: str
    # Distinct from both ``schema_version`` (this seal's own shape) and
    # ``program_version`` (the decision math). Identifies the shape of the
    # program's construction/session protocol — PRD §12's staged
    # open/advance/settle contract (``EvaluationMode``, ``Settlement``,
    # ``EvaluationTrace``) — which could change independently of either.
    # Sourced from ``SignalSession.PROTOCOL_VERSION``, the one
    # place that shape is declared.
    protocol_version: str
    # FR-002: the parameter schema's own legal type/range/unit contract is
    # versioned so a later change to ``EmaCrossoverSignalParams``' field
    # constraints is a provable identity change, without duplicating every
    # field's ``ge``/``le`` bound into this seal (that would be a second
    # hand-maintained copy of the Pydantic schema itself). Sourced from
    # ``EmaCrossoverSignalParams.PARAMETER_SCHEMA_VERSION``.
    parameter_schema_version: str
    golden_trace_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameters: dict[str, ResolvedSignalParameter]
    parameters_match_validated_settings: bool
    data: SignalDataContract
    clock: SignalClockContract
    signals: tuple[SignalSeriesContract, ...]
    decision_streams: tuple[str, ...]
    bar_integrity: SignalBarIntegrityContract
    exit_eligibility: ExitEligibilityContract
    numerical_provenance: NumericalProvenanceContract

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
    "ExitEligibilityContract",
    "NumericalProvenanceContract",
    "ResolvedSignalParameter",
    "SealedBotProgram",
    "SignalBarIntegrityContract",
    "SignalClockContract",
    "SignalDataContract",
    "SignalSeriesContract",
    "seal_bot_program",
    "semantic_payload_hash",
]
