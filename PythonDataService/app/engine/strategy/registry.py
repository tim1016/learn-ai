"""Engine strategy registry.

The registry is engine-level authority: routers render it, live deploy gates
read it, and the runner uses it to resolve the algorithm class. Keeping it out
of ``app.routers.engine`` prevents live runtime code from importing private
router symbols just to answer deploy/start safety questions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from app.engine.pine_generators import (
    generate_strategy_a_pine,
    generate_strategy_b_pine,
    generate_strategy_c_pine,
)
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationConsecutiveGreen,
)
from app.engine.strategy.algorithms.ema_crossover_2_bps import (
    EmaCrossover2BpsAlgorithm,
)
from app.engine.strategy.algorithms.ema_crossover_signal import (
    EmaCrossoverSignalAlgorithm,
)
from app.engine.strategy.algorithms.rsi_mean_reversion import (
    RsiMeanReversionAlgorithm,
)
from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)
from app.engine.strategy.algorithms.spy_ema_crossover_options import (
    SpyEmaCrossoverOptionsAlgorithm,
)
from app.engine.strategy.algorithms.spy_orb import SpyOpeningRangeBreakout
from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm
from app.engine.strategy.algorithms.spy_strategy_b import SpyStrategyBAlgorithm
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm
from app.engine.strategy.base import Strategy
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import (
    SignalProgram,
    SignalSession,
)
from app.schemas.signal_program_seal import (
    ExitEligibilityContract,
    NumericalProvenanceContract,
    SignalBarIntegrityContract,
    SignalSeriesContract,
)


class StrategyParamsBase(BaseModel):
    """Base for every strategy's parameter model.

    Subclasses declare the strategy's own fields. A strategy with no
    parameters can simply reuse this class directly.
    """

    model_config = {"extra": "forbid"}


class EmaCrossoverParams(StrategyParamsBase):
    """EMA crossover signal parameters.

    Shares the exact indicator / gap / RSI logic as the LEAN-parity SPY
    reference run, but lets the user pick the *signal stream* at request time.
    Defaults to SPY so the out-of-the-box run matches the bit-exact
    reference fixture; other symbols (QQQ, IWM, etc.) can be substituted
    without touching the strategy. A live Action Plan independently selects
    the stock to trade; Engine Lab backtests bind the one loaded price stream
    to both roles.
    """

    symbol: str = Field(
        "SPY",
        min_length=1,
        max_length=20,
        description="Signal-stream ticker. The live Action Plan selects the traded stock separately.",
    )


class EmaCrossoverSignalParams(EmaCrossoverParams):
    """EMA-crossover *signal* strategy gates, exposed as parameters.

    Defaults preserve the validated LEAN-parity point exactly (absolute gap
    0.20, RSI band 50–70); the Recency Chart sweeps them.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. A
    # ``ClassVar`` is invisible to Pydantic's field machinery, so it never
    # becomes part of the JSON schema or a constructor argument.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "ema-crossover-signal-params/v1"

    gap: float = Field(
        0.20,
        ge=0.0,
        allow_inf_nan=False,
        title="Crossover gap",
        description="Minimum EMA(5) − EMA(10) gap, in absolute price, required for entry.",
    )
    rsi_min: float = Field(
        50.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI lower gate",
        description="Inclusive lower RSI(14) value allowed for entry.",
    )
    rsi_max: float = Field(
        70.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI upper gate",
        description="Inclusive upper RSI(14) value allowed for entry.",
    )

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossoverSignalParams:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self


class EmaCrossover2BpsParams(EmaCrossoverParams):
    """Configurable gates shared by the Python strategy and its LEAN twin."""

    gap_bps: float = Field(
        2.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="Crossover gap (bps)",
        description="Minimum EMA(5) minus EMA(10) crossover gap, measured in basis points of EMA(10).",
    )
    rsi_min: float = Field(
        50.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI lower gate",
        description="Inclusive lower RSI(14) value allowed for entry.",
    )
    rsi_max: float = Field(
        70.0,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        title="RSI upper gate",
        description="Inclusive upper RSI(14) value allowed for entry.",
    )

    @model_validator(mode="after")
    def _validate_rsi_band(self) -> EmaCrossover2BpsParams:
        if self.rsi_min >= self.rsi_max:
            raise ValueError("rsi_min must be less than rsi_max")
        return self


class SmaCrossoverParams(StrategyParamsBase):
    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``EmaCrossoverSignalParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "sma-crossover-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20)
    short_window: int = Field(10, ge=2, le=500)
    long_window: int = Field(30, ge=3, le=1000)
    resolution_minutes: int = Field(15, ge=1, le=1440)

    @model_validator(mode="after")
    def _validate_window_order(self) -> SmaCrossoverParams:
        # Mirrors SmaCrossoverAlgorithm.__init__'s own guard: a schema-valid
        # payload that violates this would otherwise pass admission and
        # persist immutably before crashing at strategy construction.
        if self.long_window <= self.short_window:
            raise ValueError("long_window must be strictly greater than short_window")
        return self


class DailySmaCrossoverParams(StrategyParamsBase):
    """Daily-resolution SMA crossover — no ``resolution_minutes`` field.

    The bar cadence is fixed to 1 day by the registry's build function
    (which sets ``resolution_minutes=1440`` on the underlying algorithm)
    because the strategy runs directly against LEAN daily zips. Window
    sizes are in *days* here: a 50/200 is the classic long-term golden
    cross.
    """

    symbol: str = Field("AAPL", min_length=1, max_length=20)
    short_window: int = Field(50, ge=2, le=500)
    long_window: int = Field(200, ge=3, le=1000)

    @model_validator(mode="after")
    def _validate_window_order(self) -> DailySmaCrossoverParams:
        if self.long_window <= self.short_window:
            raise ValueError("long_window must be strictly greater than short_window")
        return self


class RsiMeanReversionParams(StrategyParamsBase):
    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "rsi-mean-reversion-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20)
    window: int = Field(14, ge=2, le=500)
    oversold: float = Field(30.0, gt=0, lt=100)
    overbought: float = Field(70.0, gt=0, lt=100)
    resolution_minutes: int = Field(15, ge=1, le=1440)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> RsiMeanReversionParams:
        # Mirrors RsiMeanReversionAlgorithm.__init__'s own guard: a
        # schema-valid payload that violates this would otherwise pass
        # admission and persist immutably before crashing at construction.
        if not self.oversold < self.overbought:
            raise ValueError("oversold must be strictly less than overbought")
        return self


class OrbParams(StrategyParamsBase):
    """Opening Range Breakout parameters — dynamic ticker.

    Zero-warmup, price-action-only strategy. Each trading day resets
    independently so the parameter set carries over cleanly between
    tickers (SPY, QQQ, IWM, etc.) without re-tuning.

    * ``orb_bars`` — number of 15-min bars forming the opening range
      (3 = 45 minutes, the default).
    * ``hold_bars`` — bars to hold after entry before flattening
      (5 = 75 minutes, the default).
    * ``min_range_pct`` / ``max_range_pct`` — accept only days whose
      opening-range size, expressed as a percentage of the range-low
      price, falls inside this band. Filters out both flat-open days
      (nothing to break out of) and gap-open days (too stretched).
    """

    symbol: str = Field("SPY", min_length=1, max_length=20)
    orb_bars: int = Field(3, ge=1, le=6)
    hold_bars: int = Field(5, ge=1, le=50)
    min_range_pct: float = Field(0.30, ge=0.0, le=10.0)
    max_range_pct: float = Field(1.50, ge=0.1, le=20.0)


class DeploymentValidationParams(StrategyParamsBase):
    """Deployment-validation strategy with configurable signal/trade tickers."""

    symbol: str = Field("SPY", min_length=1, max_length=20)
    trade_symbol: str | None = Field(None, min_length=1, max_length=20)


class EmaCrossoverOptionsParams(StrategyParamsBase):
    """EMA crossover options spread strategy parameters.

    Same signal engine as the equity EMA crossover, but trades bull call
    or bull put spreads on the underlying's option chain instead.
    """

    # Signal parameters (same defaults as equity strategy)
    symbol: str = Field("SPY", min_length=1, max_length=20)
    ema_fast_period: int = Field(5, ge=2, le=200)
    ema_slow_period: int = Field(10, ge=3, le=500)
    rsi_period: int = Field(14, ge=2, le=200)
    ema_gap_min: float = Field(0.20, ge=0)
    rsi_min: float = Field(50.0, ge=0, lt=100)
    rsi_max: float = Field(70.0, gt=0, le=100)
    timeframe_minutes: int = Field(15, ge=1, le=1440)
    bars_to_hold: int = Field(5, ge=1, le=200)

    # Options parameters
    spread_type: str = Field(
        "BULL_CALL",
        description="BULL_CALL or BULL_PUT",
    )
    min_dte: int = Field(7, ge=0, le=365)
    max_dte: int = Field(30, ge=1, le=365)
    long_call_delta_target: float = Field(0.60, ge=0, le=1)
    short_call_delta_target: float = Field(0.30, ge=0, le=1)
    short_put_delta_target: float = Field(-0.30, ge=-1, le=0)
    long_put_delta_target: float = Field(-0.15, ge=-1, le=0)
    min_open_interest: int = Field(100, ge=0)
    min_volume: int = Field(10, ge=0)
    max_bid_ask_spread_pct: float = Field(0.20, ge=0, le=1)
    contracts_per_trade: int = Field(1, ge=1, le=100)
    max_positions: int = Field(1, ge=1, le=10)
    contract_multiplier: int = Field(100, ge=1)

    # Pricing parameters
    pricing_mode: str = Field(
        "quantlib_only",
        description="quantlib_only, market_preferred, or market_required",
    )
    pricing_engine: str = Field("analytic_bs")
    risk_free_rate: float = Field(0.05, ge=0, le=1)
    dividend_yield: float = Field(0.0, ge=0, le=1)
    default_iv: float = Field(0.20, ge=0.01, le=5.0)
    half_spread_pct: float = Field(0.01, ge=0, le=0.5)


class RsiRangeStrategyAParams(StrategyParamsBase):
    """Strategy A — EMA-gap + MACD + RSI-range, ADX-exit.

    All thresholds and indicator periods are configurable. Entry requires
    RSI to sit inside the ``[rsi_low_gate, rsi_high_gate]`` range AND the
    EMA gap to exceed ``ema_gap_threshold`` AND MACD line > 0, all at the
    same bar while flat. Pyramiding=1 prevents re-entry while holding.
    """

    # FR-002: versions this schema's own legal type/unit/range contract —
    # sealed as ``ConfiguredSignalProgramSeal.parameter_schema_version`` so a
    # future change to the ``ge``/``le`` bounds below is a provable identity
    # change without duplicating every bound into the seal itself. Mirrors
    # ``SmaCrossoverParams.PARAMETER_SCHEMA_VERSION``'s pattern.
    PARAMETER_SCHEMA_VERSION: ClassVar[str] = "rsi-range-strategy-a-params/v1"

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    ema_fast_period: int = Field(20, ge=2, le=500, description="Fast EMA period.")
    ema_slow_period: int = Field(50, ge=3, le=1000, description="Slow EMA period.")
    ema_gap_threshold: float = Field(
        0.5,
        ge=0,
        description=(
            "Minimum absolute gap between fast and slow EMAs "
            "(EMA_fast − EMA_slow > threshold). Default 0.5 is a reasonable "
            "SPY 15-minute trend-confirmation threshold. Other tickers scale "
            "with price — tune accordingly."
        ),
    )
    macd_fast: int = Field(12, ge=2, le=200, description="MACD fast EMA period.")
    macd_slow: int = Field(26, ge=3, le=500, description="MACD slow EMA period.")
    macd_signal: int = Field(9, ge=2, le=200, description="MACD signal-line EMA period.")
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(
        38.0,
        ge=0,
        lt=100,
        description="Lower bound of the RSI entry range — RSI must be ≥ this to enter.",
    )
    rsi_high_gate: float = Field(
        70.0,
        gt=0,
        le=100,
        description="Upper bound of the RSI entry range — RSI must be ≤ this to enter.",
    )
    adx_period: int = Field(14, ge=2, le=200, description="ADX period (Wilders smoothing).")
    adx_exit_threshold: float = Field(
        15.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 15 for Strategy A.",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution. Default 15 minutes.")

    @model_validator(mode="after")
    def _validate_rsi_gate_order(self) -> RsiRangeStrategyAParams:
        # Mirrors RsiRangeStrategy.__init__'s own guard (the shared A/B/C
        # base class): a schema-valid payload that violates this would
        # otherwise pass admission and persist immutably before crashing
        # at construction.
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError("rsi_low_gate must be strictly less than rsi_high_gate")
        return self


class RsiRangeStrategyBParams(StrategyParamsBase):
    """Strategy B — Supertrend + ADX-entry + MACD + RSI-range, ADX-exit."""

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    supertrend_atr_period: int = Field(
        10, ge=2, le=200, description="ATR period for Supertrend. Default 10 (Pine default)."
    )
    supertrend_multiplier: float = Field(
        3.0,
        gt=0,
        description="Supertrend ATR multiplier. Default 3 (Pine default).",
    )
    adx_entry_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Require ADX > this threshold at entry. Default 20.",
    )
    macd_fast: int = Field(12, ge=2, le=200, description="MACD fast EMA period.")
    macd_slow: int = Field(26, ge=3, le=500, description="MACD slow EMA period.")
    macd_signal: int = Field(9, ge=2, le=200, description="MACD signal-line EMA period.")
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(38.0, ge=0, lt=100, description="Lower bound of RSI entry range.")
    rsi_high_gate: float = Field(70.0, gt=0, le=100, description="Upper bound of RSI entry range.")
    adx_period: int = Field(14, ge=2, le=200, description="ADX period.")
    adx_exit_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 20 for Strategy B.",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution.")

    @model_validator(mode="after")
    def _validate_rsi_gate_order(self) -> RsiRangeStrategyBParams:
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError("rsi_low_gate must be strictly less than rsi_high_gate")
        return self


class RsiRangeStrategyCParams(StrategyParamsBase):
    """Strategy C — ADX-entry + ADX-rising + RSI-range, ADX-exit.

    No MACD, no Supertrend — the simplest of the three. Entry requires
    RSI inside the range filter, ADX above a threshold, AND ADX rising
    bar-over-bar.
    """

    symbol: str = Field("SPY", min_length=1, max_length=20, description="Underlying ticker.")
    adx_entry_threshold: float = Field(
        20.0,
        ge=0,
        le=100,
        description="Require ADX > this threshold at entry. Default 20.",
    )
    rsi_period: int = Field(14, ge=2, le=200, description="RSI period (Wilders smoothing).")
    rsi_low_gate: float = Field(38.0, ge=0, lt=100, description="Lower bound of RSI entry range.")
    rsi_high_gate: float = Field(70.0, gt=0, le=100, description="Upper bound of RSI entry range.")
    adx_period: int = Field(14, ge=2, le=200, description="ADX period.")
    adx_exit_threshold: float = Field(
        15.0,
        ge=0,
        le=100,
        description="Exit when ADX drops below this threshold. Default 15 (same as Strategy A).",
    )
    resolution_minutes: int = Field(15, ge=1, le=1440, description="Bar resolution.")

    @model_validator(mode="after")
    def _validate_rsi_gate_order(self) -> RsiRangeStrategyCParams:
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError("rsi_low_gate must be strictly less than rsi_high_gate")
        return self


@dataclass(frozen=True, slots=True)
class ChartParamRef:
    """Reference to one validated strategy parameter in a chart recipe."""

    field: str


@dataclass(frozen=True, slots=True)
class StrategyChartIndicator:
    """Declarative indicator recipe owned by the registered strategy."""

    name: str
    params: dict[str, int | float | ChartParamRef]


@dataclass(frozen=True, slots=True)
class StrategyBarCadence:
    """Declarative bar cadence resolved from validated strategy parameters."""

    timespan: Literal["minute", "day"]
    multiplier: int | ChartParamRef


@dataclass(frozen=True, slots=True)
class SignalProgramContract:
    """Versioned qualification inputs for one registered Signal Program.

    Artifact paths are relative to ``PythonDataService`` and intentionally
    enumerate the executable math/session surface.  The qualification job
    hashes their bytes and binds that digest to the golden trace root.

    This is the single canonical description of a Signal Program's semantic
    surface (issue #1728 sibling finding: the v2 seal was materially
    incomplete against PRD §11.1 because half of it had no declared source at
    all). ``app.services.signal_program_admission.build_start_program_seal``
    copies the ``signals``/``decision_streams``/``bar_integrity``/
    ``exit_eligibility``/``numerical_provenance`` values straight from this
    contract into ``ConfiguredSignalProgramSeal`` — the same objects, not a
    re-derived or hand-duplicated copy — so a semantic field added here is
    automatically part of the sealed identity rather than a second list that
    can silently fall out of sync.
    """

    program_version: str
    # Distinct from program_version (the decision math) — identifies the
    # shape of the session's construction/staging protocol. Mirrors
    # SignalSession.PROTOCOL_VERSION rather than re-declaring it, so the
    # two constants cannot drift apart unnoticed (see
    # tests/engine/strategy/test_registry_signal_program_identity.py).
    protocol_version: str
    parameter_schema_version: str
    golden_trace_root: str
    provider: str
    base_timeframe_ms: int
    decision_timeframe_ms: int
    warmup_lookback_days: int
    signals: tuple[SignalSeriesContract, ...]
    decision_streams: tuple[str, ...]
    bar_integrity: SignalBarIntegrityContract
    exit_eligibility: ExitEligibilityContract
    numerical_provenance: NumericalProvenanceContract
    parameter_units: dict[str, str]
    validated_settings: dict[str, str | int | float | bool]
    validated_symbols: tuple[str, ...]
    artifact_paths: tuple[str, ...]


@dataclass
class StrategyRegistration:
    display_name: str
    description: str
    param_schema: type[StrategyParamsBase]
    build: Callable[[StrategyParamsBase], Strategy]
    # VCR-0004 / Phase 2 — the algorithm class the runner constructs. The
    # registry key is the module name (``app.engine.strategy.algorithms.{key}``);
    # ``class_name`` names the class inside that module. Together they retire
    # the ``<PascalKey>Algorithm`` convention so a future class rename
    # (``DeploymentValidationAlgorithm = DeploymentValidationConsecutiveGreen``
    # was the smoking gun) cannot silently break the runner's class lookup.
    class_name: str = ""
    # Which data resolutions the strategy can run against. Defaults to
    # minute-only because every currently-ported strategy consolidates
    # minute bars via a ``TradeBarConsolidator``. Daily-native strategies
    # explicitly declare ``{"daily"}``.
    supported_resolutions: set[str] = dc_field(default_factory=lambda: {"minute"})
    # Human-readable pseudocode snippet, shown under the strategy in the
    # frontend picker. Keep it short and accurate — the point is to let
    # users see the rules at a glance without opening the source file.
    algorithm_pseudocode: str = ""
    # Parity-critical gotchas and implementation quirks. Surfaced in the
    # UI so they're not rediscovered by trial and error on the next
    # ticker / strategy combination. Each entry is one short paragraph
    # or bullet; render as a list on the frontend.
    gotchas: list[str] = dc_field(default_factory=list)
    # Optional Pine v6 generator — takes validated params, returns a
    # complete Pine script. When present, the frontend can download the
    # script via ``GET /api/engine/strategies/{name}/pine``.
    pine_generator: Callable[[StrategyParamsBase], str] | None = None
    # ADR 0009 § 6 — which boundary sizes this strategy.
    # ``"policy"`` (default) — the execution boundary targets via
    # ``set_holdings``;
    # the deploy-form sizing selector is enabled, and
    # ``live_config.sizing`` ∈ {FixedShares, FixedNotional, SetHoldings}
    # governs the magnitude.
    # ``"explicit"`` — the strategy supplies its own quantity/contracts
    # (``market_order``, ``contracts_per_trade``, internal accounting); the
    # deploy-form sizing selector is disabled + labelled "self-sized" and
    # the required ``live_config.sizing`` is ``StrategyExplicit``.
    sizing_surface: Literal["policy", "explicit"] = "policy"
    # Fields accepted only by non-Engine-Lab construction paths. They remain in
    # ``param_schema`` so the live runner can validate its internal injection,
    # but are hidden from ``GET /strategies`` and rejected by normal backtests.
    hidden_params: set[str] = dc_field(default_factory=set)
    # ADR 0012 / PRD #593 Slice 1A — which boundary chooses the
    # *instrument* this strategy trades. ``"explicit"`` means the strategy code
    # itself names the instrument (e.g. ``set_holdings("SPY", 1.0)``),
    # and ``live_config.action`` is informational only. ``"policy"``
    # is active for strategies that emit a generic ``SignalIntent`` and let the
    # deploy-time Action Plan resolve concrete instruments. The deploy boundary
    # stores this field in the run ledger and rejects unsupported Action Plans
    # for policy strategies.
    instrument_surface: Literal["policy", "explicit"] = "explicit"
    # The live Action Plan shape this strategy requires to start. This is
    # intentionally independent from ``instrument_surface``: a legacy explicit
    # strategy may still receive its trade symbol from a stock Action Plan.
    # ``"single_long_stock"`` means exactly one long stock entry and a
    # matching close-leg exit; ``"none"`` means no deploy/start requirement.
    action_plan_contract: Literal["none", "single_long_stock"] = "none"
    # Signal-only strategies must declare how the live runner binds their
    # asset-free ENTER/EXIT intents. ``signal_symbol`` preserves the original
    # same-symbol behavior for legacy ledgers; ``action_plan_stock`` delegates
    # the asset selection to the validated Action Plan contract.
    signal_intent_binding: Literal["none", "signal_symbol", "action_plan_stock"] = "none"
    # Compatibility registrations remain runnable for existing ledgers but
    # must not appear as duplicate choices in the Engine Lab strategy picker.
    catalog_visible: bool = True
    # Engine Lab parity — the LEAN trusted-sample template that implements
    # the same rules as this strategy, if one exists. When set, every raw
    # minute-resolution Python run auto-spawns a LEAN validating companion
    # sharing a parity_group_id (see app/services/parity_companion.py).
    # None → honest "parity unavailable — no LEAN counterpart".
    lean_twin: str | None = None
    # Validated strategy parameters that the LEAN twin consumes. The parity
    # dispatcher copies these exact resolved values into the companion request;
    # an empty tuple means the LEAN template is parameter-free.
    lean_parameter_names: tuple[str, ...] = ()
    # Evidence-chart recipe resolved from the same validated parameter model
    # the strategy executes. Keeping this on the registration prevents the UI
    # from guessing strategy semantics from parameter names.
    chart_indicators: tuple[StrategyChartIndicator, ...] = ()
    strategy_bars: StrategyBarCadence = StrategyBarCadence("minute", 1)
    # A Signal Program is optional because existing strategies may still use
    # the legacy event-handler lifecycle. When present, this is the one
    # construction authority for the program and its staged SignalSession.
    signal_program_factory: Callable[[StrategyParamsBase], SignalProgram] | None = None
    # Admission metadata is deliberately adjacent to the construction seam.
    # A factory without a contract is an unqualified program and cannot be
    # sealed for Start/Resume.
    signal_program_contract: SignalProgramContract | None = None


def public_params_schema(reg: StrategyRegistration, *, extra_hidden: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return this registration's parameter JSON schema with hidden fields removed.

    ``extra_hidden`` lets one construction path (e.g. the Alpaca deploy form,
    where ``symbol`` is deploy-authoritative rather than a tunable) hide
    additional fields without every registration declaring them in its own
    ``hidden_params``.
    """
    hidden = reg.hidden_params | extra_hidden
    schema = reg.param_schema.model_json_schema()
    if not hidden:
        return schema
    schema = dict(schema)
    properties = dict(schema.get("properties") or {})
    for name in hidden:
        properties.pop(name, None)
    schema["properties"] = properties
    if "required" in schema:
        schema["required"] = [name for name in schema["required"] if name not in hidden]
    return schema


def hidden_params_present(
    reg: StrategyRegistration,
    params: dict[str, Any],
    *,
    extra_hidden: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the submitted parameter names this registration hides, sorted."""
    hidden = reg.hidden_params | extra_hidden
    return sorted(hidden.intersection(params))


# One declaration of this program's sealed identity, referenced by both its
# construction seam (below, where it becomes the evaluation_id hash input)
# and its registry contract (where it becomes the build-proof admission
# identity). Declared once rather than restated as a literal in each place:
# a factory whose program_version drifts from its contract's would produce
# a bot whose evaluation traces claim a different program than its seal,
# and neither hash would look wrong on its own.
# test_registry_signal_program_identity.py enforces the relationship for
# every registered program, so a new program that restates a literal here
# instead of following this pattern fails loudly rather than silently.
_EMA_SIGNAL_PROGRAM_KEY = "ema_crossover_signal"
_EMA_SIGNAL_PROGRAM_VERSION = "ema-crossover-signal/v1"


def _build_ema_crossover_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral EMA Signal Program from registry params."""
    typed = params
    assert isinstance(typed, EmaCrossoverSignalParams)
    strategy = EmaCrossoverSignalAlgorithm(
        symbol=typed.symbol,
        gap=typed.gap,
        rsi_min=typed.rsi_min,
        rsi_max=typed.rsi_max,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_EMA_SIGNAL_PROGRAM_KEY,
        program_version=_EMA_SIGNAL_PROGRAM_VERSION,
        # Fixed cadence: this program exposes no resolution parameter, and
        # its registration declares StrategyBarCadence("minute", 15).
        timeframe_ms=15 * 60_000,
    )
    strategy.signal_program = program
    return program


# Same pattern as the EMA identity above (issue #1730 Slice 5, first
# additional promotion): declared once here, referenced by both the
# construction seam and the registry contract below.
_SMA_SIGNAL_PROGRAM_KEY = "sma_crossover"
_SMA_SIGNAL_PROGRAM_VERSION = "sma-crossover/v1"


def _build_sma_crossover_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral SMA Signal Program from registry params."""
    typed = params
    assert isinstance(typed, SmaCrossoverParams)
    strategy = SmaCrossoverAlgorithm(
        symbol=typed.symbol,
        short_window=typed.short_window,
        long_window=typed.long_window,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_SMA_SIGNAL_PROGRAM_KEY,
        program_version=_SMA_SIGNAL_PROGRAM_VERSION,
        # Derived from the resolved parameter, not the contract's validated
        # value: this program's decision clock is deploy-time configurable,
        # and a session pinned to the validated 15 minutes would reject
        # every bar of a bot deployed at any other resolution.
        timeframe_ms=typed.resolution_minutes * 60_000,
    )
    strategy.signal_program = program
    return program


# Same pattern as the EMA/SMA identities above (issue #1730 Slice 5, second
# additional promotion): declared once here, referenced by both the
# construction seam and the registry contract below.
_RSI_MEAN_REVERSION_SIGNAL_PROGRAM_KEY = "rsi_mean_reversion"
_RSI_MEAN_REVERSION_SIGNAL_PROGRAM_VERSION = "rsi-mean-reversion/v1"


def _build_rsi_mean_reversion_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral RSI Signal Program from registry params."""
    typed = params
    assert isinstance(typed, RsiMeanReversionParams)
    strategy = RsiMeanReversionAlgorithm(
        symbol=typed.symbol,
        window=typed.window,
        oversold=typed.oversold,
        overbought=typed.overbought,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_RSI_MEAN_REVERSION_SIGNAL_PROGRAM_KEY,
        program_version=_RSI_MEAN_REVERSION_SIGNAL_PROGRAM_VERSION,
        # Derived from the resolved parameter, not the contract's validated
        # value: this program's decision clock is deploy-time configurable,
        # and a session pinned to the validated 15 minutes would reject
        # every bar of a bot deployed at any other resolution.
        timeframe_ms=typed.resolution_minutes * 60_000,
    )
    strategy.signal_program = program
    return program


# Same pattern as the identities above (issue #1730 Slice 5): declared once
# here, referenced by both the construction seam and the registry contract.
_SPY_STRATEGY_A_SIGNAL_PROGRAM_KEY = "spy_strategy_a"
_SPY_STRATEGY_A_SIGNAL_PROGRAM_VERSION = "spy-strategy-a/v1"


def _build_spy_strategy_a_signal_program(params: StrategyParamsBase) -> SignalProgram:
    """Construct the sole broker-neutral Strategy A Signal Program from registry params."""
    typed = params
    assert isinstance(typed, RsiRangeStrategyAParams)
    strategy = SpyStrategyAAlgorithm(
        symbol=typed.symbol,
        ema_fast_period=typed.ema_fast_period,
        ema_slow_period=typed.ema_slow_period,
        ema_gap_threshold=typed.ema_gap_threshold,
        macd_fast=typed.macd_fast,
        macd_slow=typed.macd_slow,
        macd_signal=typed.macd_signal,
        rsi_period=typed.rsi_period,
        rsi_low_gate=typed.rsi_low_gate,
        rsi_high_gate=typed.rsi_high_gate,
        adx_period=typed.adx_period,
        adx_exit_threshold=typed.adx_exit_threshold,
        resolution_minutes=typed.resolution_minutes,
    )
    program = SignalProgram.create(
        strategy,
        program_key=_SPY_STRATEGY_A_SIGNAL_PROGRAM_KEY,
        program_version=_SPY_STRATEGY_A_SIGNAL_PROGRAM_VERSION,
        # Derived from the resolved parameter, not the contract's validated
        # value — same reasoning as sma_crossover's factory above: this
        # program's decision clock is deploy-time configurable, and a
        # session pinned to the validated 15 minutes would reject every bar
        # of a bot deployed at any other resolution.
        timeframe_ms=typed.resolution_minutes * 60_000,
    )
    strategy.signal_program = program
    return program


_STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    _EMA_SIGNAL_PROGRAM_KEY: StrategyRegistration(
        display_name="EMA Crossover Signal",
        class_name="EmaCrossoverSignalAlgorithm",
        signal_program_contract=SignalProgramContract(
            program_version=_EMA_SIGNAL_PROGRAM_VERSION,
            protocol_version=SignalSession.PROTOCOL_VERSION,
            parameter_schema_version=EmaCrossoverSignalParams.PARAMETER_SCHEMA_VERSION,
            golden_trace_root="82b81f82b5690919871e50a6c9ac39f26fa28d2c09b96dad4a777d4615cd6179",
            provider="polygon",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=15 * 60_000,
            warmup_lookback_days=5,
            # EmaCrossoverSignalAlgorithm.initialize() constructs exactly
            # these three named series (EMA5, EMA10, RSI14), each fed
            # bar.close at bar.end_ms. Periods are fixed at construction, not
            # parameterized, so they are facts about this program version,
            # not user-configurable settings. warmup_bars mirrors each
            # indicator's own is_ready threshold
            # (app/engine/indicators/base.py: samples >= period; RSI
            # overrides to period + 1 for its first delta) — any drift
            # between these constants and the real indicators would already
            # change the golden trace root and fail
            # test_validated_ema_settings_corpus_has_a_pinned_trace_root, so
            # this cannot silently go stale.
            signals=(
                SignalSeriesContract(name="ema_fast", indicator="ema", field="close", period=5, warmup_bars=5),
                SignalSeriesContract(name="ema_slow", indicator="ema", field="close", period=10, warmup_bars=10),
                SignalSeriesContract(
                    name="rsi", indicator="rsi_wilders", field="close", period=14, warmup_bars=15
                ),
            ),
            # SignalIntentKind's complete member set (signal_intent.py) —
            # every decision this program can stage collapses to one of
            # these two named streams.
            decision_streams=tuple(kind.value for kind in SignalIntentKind),
            # System-wide today (app.services.source_bar_ledger /
            # app.services.bot_trade_strategy own these facts, not this
            # program) — see SignalBarIntegrityContract's docstring for the
            # exact enforcing code. Explicit here rather than relying on the
            # schema default so a future program that needs a different
            # policy must set its own value, not silently inherit this one.
            bar_integrity=SignalBarIntegrityContract(),
            # EmaCrossoverSignalAlgorithm.commit_signal_decision hardcodes
            # self._bars_until_exit = 5 at entry (75 minutes on 15-minute
            # bars); report_state_for_persistence returns None while
            # _in_position, so a mid-countdown position cannot currently
            # survive Pause/Resume.
            exit_eligibility=ExitEligibilityContract(
                countdown_decision_clocks=5,
                countdown_state_persistable=False,
            ),
            numerical_provenance=NumericalProvenanceContract(
                formula=(
                    "Long-only EMA(5)/EMA(10) crossover on 15-minute signal bars with an "
                    "RSI(14) filter. Entry: fresh EMA5 > EMA10 crossover AND "
                    "(EMA5 - EMA10) >= 0.20 AND 50 <= RSI <= 70. Exit: 5 consolidated bars "
                    "(75 minutes) after entry."
                ),
                reference=(
                    "Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs (Apr 2026 revision); "
                    "TradingView Pine validation docs/validation/SPY_EMA_Crossover_RSI.pine; "
                    "validation report docs/validation/SPY_EMA_Crossover_Validation_Report.pdf"
                ),
                canonical_implementation=(
                    "app/engine/strategy/algorithms/ema_crossover_signal.py::EmaCrossoverSignalAlgorithm"
                ),
                validated_against=(
                    "tests/engine/strategy/algorithms/test_signal_only_ema_crossover.py; "
                    "tests/engine/strategy/test_ema_signal_program.py::"
                    "test_validated_ema_settings_corpus_has_a_pinned_trace_root; "
                    "docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md"
                ),
                # The trace/decision identity is Decimal-exact and
                # SHA-256-compared (signal_program.py), not
                # tolerance-compared — see test_validated_ema_settings_corpus_
                # has_a_pinned_trace_root's byte-exact trace_root assertion.
                equivalence_level="bit_exact",
                # One level down (the EMA/RSI *value* parity against LEAN,
                # not the trace-identity hash above): documented absolute
                # tolerance from the reconciliation report.
                tolerance_atol=1e-9,
                tolerance_rtol=0.0,
                parity_fixture_ids=(
                    "tests/fixtures/golden/ema-signal-session/v1/trace-corpus.json",
                    "tests/fixtures/golden/cross-engine-studies/cells/SPY_W3mo_2026-02-02_to_2026-04-30",
                    "tests/fixtures/golden/cross-engine-studies/cells/QQQ_W3mo_2026-02-02_to_2026-04-30",
                    "tests/fixtures/golden/cross-engine-studies/cells/SPY_W6mo_2025-11-03_to_2026-04-30",
                    "tests/fixtures/golden/cross-engine-studies/cells/QQQ_W6mo_2025-11-03_to_2026-04-30",
                ),
            ),
            parameter_units={
                "symbol": "ticker",
                "gap": "quote_currency",
                "rsi_min": "rsi_points",
                "rsi_max": "rsi_points",
            },
            validated_settings={"gap": 0.20, "rsi_min": 50.0, "rsi_max": 70.0},
            validated_symbols=("AAPL", "QQQ", "SPY", "TSLA"),
            # Issue #1728 defect 2: this is not "every file the module
            # graph reaches" — it is that transitive first-party import
            # closure of the two roots below, MINUS the files proven (in
            # scripts/run_signal_program_build_qualification.py's exclusion
            # list) to be unreachable from evaluate_signal_bar()'s decision
            # math: SignalSession.advance() (signal_program.py)
            # builds and stores the EvaluationTrace *before* settle() ever
            # calls commit_signal_decision(), so execution/fill/sizing/
            # commission/Insight-publication bytes reached only through the
            # commit path cannot retroactively change a trace that already
            # exists. test_signal_decision_digest_closure.py recomputes the
            # closure from these paths and fails the build if a newly
            # introduced import isn't triaged into this list or that one.
            artifact_paths=(
                "app/engine/strategy/algorithms/ema_crossover_signal.py",
                "app/engine/strategy/base.py",
                "app/engine/strategy/signal_intent.py",
                "app/engine/strategy/signal_program.py",
                "app/engine/indicators/base.py",
                "app/engine/indicators/ema.py",
                "app/engine/indicators/rsi.py",
                "app/engine/indicators/sma.py",
                "app/engine/consolidators/trade_bar_consolidator.py",
                "app/engine/data/trade_bar.py",
                "app/engine/live/indicator_state.py",
                "app/lean_sidecar/trading_calendar.py",
                "app/utils/timestamps.py",
            ),
        ),
        description=(
            "Long-only intraday EMA signal generator. Bit-exact against the "
            "LEAN C# reference when the default SPY signal stream is used; "
            "any cached ticker (or Polygon-fetched ticker) can supply signals "
            "without changing the strategy.\n"
            "\n"
            "The strategy reads minute signal bars, consolidates them into "
            "15-minute bars, tracks EMA(5), EMA(10) and Wilders RSI(14), "
            "and goes long for exactly five 15-minute bars (75 min) every "
            "time a fresh fast-over-slow crossover lines up with a 0.20 "
            "minimum gap and an RSI in the 50–70 trend-confirmation band. "
            "There is no stop, no target, no scaling, and at most one "
            "position lifecycle open at a time. In live runs, the Action Plan "
            "selects the stock to trade; the strategy does not."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    signal_symbol = configurable (default SPY)\n"
            "    resolution = 15-minute bars consolidated from minute data\n"
            "\n"
            "Indicators (Alpha — updated each 15m bar close at bar.end_time)\n"
            "    EMA_fast = ExponentialMovingAverage(5)\n"
            "    EMA_slow = ExponentialMovingAverage(10)\n"
            "    RSI      = RelativeStrengthIndex(14, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    no signals fire until EMA_fast.is_ready and EMA_slow.is_ready\n"
            "    and RSI.is_ready (at least 14 closes for RSI; EMAs are\n"
            "    SMA-seeded over their period). The crossover-state flag is\n"
            "    primed during warmup so the first eligible bar is not a\n"
            "    spurious 'fresh' cross.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = EMA_fast > EMA_slow\n"
            "                  AND EMA_fast[-1] <= EMA_slow[-1]\n"
            "    gap_ok      = (EMA_fast - EMA_slow) >= 0.20\n"
            "    rsi_ok      = 50 <= RSI <= 70\n"
            "    ⇒ if all three: emit Insight(UP, period=5 bars); emit ENTER\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new bar\n"
            "    ⇒ when bars_held == 5: emit EXIT\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — no stop-loss, no take-profit, no signal-flip exit.\n"
            "    A losing trade is held to the time-stop. A reversed crossover\n"
            "    inside the 5-bar window is ignored.\n"
            "\n"
            "Action Plan / Portfolio Construction\n"
            "    live execution selects the stock from its Action Plan\n"
            "    Engine Lab binds intents to its single signal stream\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close → fills at the signal bar's close\n"
            "                                   (matches LEAN parity fixture)\n"
            "              | next_bar_open    → fills at the next bar's open\n"
            "                                   (matches TradingView trade-list)\n"
            "\n"
            "End of algorithm\n"
            "    if still in position at the last bar: emit EXIT"
        ),
        gotchas=[
            "EMA warmup is seeded from the SMA of the first N samples — "
            "LEAN's convention, not the Pine/most-libraries default of "
            "seeding from the first sample alone. Porting EMA to a new "
            "language without matching this will produce a different "
            "EMA trajectory for the first ~4×N bars.",
            "RSI uses Wilders smoothing with period+1 warmup. The first "
            "average gain/loss is a plain SMA at sample N+1; "
            "`is_ready` flips true at sample ≥ period+1.",
            "Indicators are updated with bar.close at bar.end_time, NOT at "
            "bar.time. Off-by-one-bar timestamps break downstream timing.",
            "The _prev_ema5_above_ema10 crossover-state flag must be updated "
            "on warmup bars too — LEAN's C# sets it before the early return. "
            "Skipping warmup updates produces a spurious cross on the first "
            "post-warmup bar.",
            "15-min consolidator must be wall-clock / epoch-anchored (bars "
            "landing on :00 :15 :30 :45). A first-bar-anchored consolidator "
            "phase-shifts every bar and ruins parity.",
            "Prices must be Python Decimal end-to-end. Float drift over two "
            "years of recursive EMA is enough to misprint at 4-decimal "
            "display precision.",
            "Use ROUND_HALF_UP for display strings to match C# "
            'decimal.ToString("F2"). Python\'s default Decimal.quantize '
            "uses banker's rounding (ROUND_HALF_EVEN) and disagrees on "
            "midpoints like 515.045 → 515.05.",
            "TradingView exports timestamps in the viewer's LOCAL timezone; "
            "Engine Lab writes proper UTC in bar.end_time. Cross-system "
            "diffs must normalize to a single reference frame (see SPY EMA "
            "validation report §5.1).",
            "The Action Plan may trade a different stock from the signal "
            "stream. That stock selection and sizing are execution concerns; "
            "this strategy emits only ENTER and EXIT intents.",
            "TV labels trades by bar START, Engine Lab by bar END — a fixed "
            "15-minute label offset that is cosmetic, not a fill-time bug.",
        ],
        param_schema=EmaCrossoverSignalParams,
        chart_indicators=(
            StrategyChartIndicator("ema", {"length": 5}),
            StrategyChartIndicator("ema", {"length": 10}),
            StrategyChartIndicator("rsi", {"length": 14}),
        ),
        strategy_bars=StrategyBarCadence("minute", 15),
        build=lambda p: _build_ema_crossover_signal_program(p).strategy,  # type: ignore[return-value]
        signal_program_factory=_build_ema_crossover_signal_program,
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
        lean_twin="ema_crossover_signal",
    ),
    "sma_crossover": StrategyRegistration(
        display_name="SMA Crossover",
        class_name="SmaCrossoverAlgorithm",
        signal_program_contract=SignalProgramContract(
            program_version=_SMA_SIGNAL_PROGRAM_VERSION,
            protocol_version=SignalSession.PROTOCOL_VERSION,
            parameter_schema_version=SmaCrossoverParams.PARAMETER_SCHEMA_VERSION,
            golden_trace_root="b0a136f7b485179bc37c7998df430480b94b0866d9bc58dbead636fa84a320e9",
            provider="polygon",
            base_timeframe_ms=60_000,
            # Matches validated_settings' resolution_minutes=15 below, the
            # only resolution this contract is qualified for. SmaCrossoverParams
            # also accepts a different resolution_minutes (1-1440) for
            # backtesting, but a deploy that resolves a non-default value
            # today seals with this fixed 15-minute figure regardless --
            # build_start_program_seal (app/services/signal_program_admission.py)
            # sources SignalDataContract.decision_timeframe_ms from the
            # contract, not the resolved parameter. parameters_match_validated_settings
            # already reports False for such a deploy (the resolved
            # resolution_minutes disagrees with validated_settings), so the
            # divergence is visible evidence, not a silent one -- but the
            # seal's decision_timeframe_ms field itself is inaccurate for
            # that case. This is a pre-existing gap in the seal (latent
            # since ema_crossover_signal never exposed a tunable
            # resolution), newly reachable now that a tunable-resolution
            # program is sealed; fixing it means sourcing
            # decision_timeframe_ms from the resolved parameter in
            # signal_program_admission.py, an admission-service change
            # outside this registration's scope.
            decision_timeframe_ms=15 * 60_000,
            # SmaCrossoverAlgorithm.initialize() builds SMA(long_window=30),
            # needing 30 consolidated 15-minute bars (450 minutes) ~= 2
            # trading sessions (390 min/session, NYSE regular hours) before
            # is_ready. 7 calendar days is a comfortable buffer over that
            # 2-session minimum, covering weekends/holidays -- the same
            # margin-over-minimum convention as ema_crossover_signal's 5-day
            # buffer over its own (smaller, ~0.6-session) RSI(14) warmup.
            warmup_lookback_days=7,
            # SmaCrossoverAlgorithm.initialize() constructs exactly these two
            # named series (SMA{short_window}, SMA{long_window}), each fed
            # bar.close at bar.end_ms. SimpleMovingAverage has no recursive
            # seeding (app/engine/indicators/sma.py): is_ready flips at
            # samples >= period like every other indicator
            # (app/engine/indicators/base.py), so warmup_bars == period for
            # both series -- unlike EMA's RSI, there is no period+1 warmup
            # override to account for.
            signals=(
                SignalSeriesContract(name="sma_short", indicator="sma", field="close", period=10, warmup_bars=10),
                SignalSeriesContract(name="sma_long", indicator="sma", field="close", period=30, warmup_bars=30),
            ),
            decision_streams=tuple(kind.value for kind in SignalIntentKind),
            bar_integrity=SignalBarIntegrityContract(),
            # SmaCrossoverAlgorithm.evaluate_signal_bar exits the instant its
            # relation (a fresh death cross) is true on a decision clock --
            # there is no held countdown the way EMA's fixed 5-bar exit
            # timer works, so this seals as "level_true"
            # (ExitEligibilityContract's second rule, added for this
            # promotion) rather than restating EMA's countdown rule
            # dishonestly. countdown_state_persistable=False for the
            # stronger reason that SmaCrossoverAlgorithm has not implemented
            # report_state_for_persistence/restore_state_from_persistence/
            # validate_state_payload at all yet -- no state, flat or
            # otherwise, currently survives a Pause/Resume, not just a
            # mid-exit one.
            exit_eligibility=ExitEligibilityContract(
                rule="level_true",
                countdown_state_persistable=False,
            ),
            numerical_provenance=NumericalProvenanceContract(
                formula=(
                    "Long-only golden-cross / death-cross. Enter long on a fresh SMA(short_window) "
                    "> SMA(long_window) crossover; exit on a fresh SMA(short_window) < "
                    "SMA(long_window) crossover."
                ),
                reference=(
                    "Internal strategy retained from the retired pandas-ta service implementation; "
                    "LEAN inspiration but no line-for-line port. No LEAN or TradingView "
                    "reconciliation exists for this promotion -- Polygon live data is unavailable, "
                    "so per PRD direction this program is qualified against its own deterministic "
                    "replay of IBKR-sourced minute bars only, with no cross-engine parity claim."
                ),
                canonical_implementation=(
                    "app/engine/strategy/algorithms/sma_crossover.py::SmaCrossoverAlgorithm"
                ),
                validated_against=(
                    "app/engine/tests/test_sma_crossover_parity.py; "
                    "app/engine/strategy/spec/tests/test_spec_sma_parity.py; "
                    "tests/engine/strategy/test_sma_signal_program.py::"
                    "test_validated_sma_settings_corpus_has_a_pinned_trace_root"
                ),
                # The trace/decision identity is Decimal-exact and
                # SHA-256-compared (signal_program.py), not
                # tolerance-compared -- see
                # test_validated_sma_settings_corpus_has_a_pinned_trace_root's
                # byte-exact trace_root assertion. Unlike EMA, there is no
                # second, one-level-down LEAN-value-parity claim here (no
                # tolerance_atol/tolerance_rtol/parity_fixture_ids) --
                # nothing beyond this corpus's own self-consistency has been
                # established for this promotion.
                equivalence_level="bit_exact",
            ),
            parameter_units={
                "symbol": "ticker",
                "short_window": "bars",
                "long_window": "bars",
                "resolution_minutes": "minutes",
            },
            validated_settings={"short_window": 10, "long_window": 30, "resolution_minutes": 15},
            validated_symbols=("AAPL", "QQQ", "SPY", "TSLA"),
            # Same triage rule as ema_crossover_signal's artifact_paths
            # (issue #1728 defect 2): the transitive first-party import
            # closure of the root below, MINUS the files in
            # _SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
            # (scripts/run_signal_program_build_qualification.py) that are
            # provably unreachable from evaluate_signal_bar()'s decision
            # math. test_sma_signal_decision_digest_closure.py recomputes
            # the closure from these paths and fails the build if a newly
            # introduced import isn't triaged into one bucket or the other.
            artifact_paths=(
                "app/engine/strategy/algorithms/sma_crossover.py",
                "app/engine/strategy/base.py",
                "app/engine/strategy/signal_intent.py",
                "app/engine/strategy/signal_program.py",
                "app/engine/indicators/base.py",
                "app/engine/indicators/sma.py",
                "app/engine/consolidators/trade_bar_consolidator.py",
                "app/engine/data/trade_bar.py",
                "app/engine/live/indicator_state.py",
                "app/lean_sidecar/trading_calendar.py",
                "app/utils/timestamps.py",
            ),
        ),
        description=(
            "Classic golden-cross / death-cross. Enters long when the short "
            "SMA crosses above the long SMA, exits on the opposite cross. "
            "Configurable symbol, window sizes (in bars), and bar resolution."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    SMA_short = SimpleMovingAverage(short_window bars)\n"
            "    SMA_long  = SimpleMovingAverage(long_window bars)\n"
            "\n"
            "Warmup\n"
            "    no signals until both SMAs have window-size samples.\n"
            "    SMA has no recursion (just a rolling mean), so there is\n"
            "    no warmup parity drift between LEAN and Pine.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = SMA_short > SMA_long\n"
            "                  AND SMA_short[-1] <= SMA_long[-1]\n"
            "    ⇒ if fresh_cross: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    death_cross = SMA_short < SMA_long\n"
            "    ⇒ if death_cross: emit EXIT intent\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — exit is signal-driven only. Re-entry requires a\n"
            "    fresh crossover, which prevents the same-bar re-entry\n"
            "    that would otherwise produce many spurious trades.\n"
            "\n"
            "Portfolio Construction\n"
            "    action plan binds ENTER/EXIT to one long stock leg\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (LEAN default) | next_bar_open"
        ),
        gotchas=[
            "Window sizes are in BARS, not minutes. With "
            "resolution_minutes=15 and short_window=10, the short SMA "
            "covers 150 minutes (10 × 15).",
            "Re-entry requires a FRESH crossover (short was ≤ long on "
            "the prior bar and is strictly > long on this bar). The "
            "strategy cannot re-enter on the same side of the cross it "
            "just exited from, so the multi-trade-per-day issue that hit "
            "ORB doesn't apply here.",
            "Simple SMA has no recursion — it's just a rolling mean — so "
            "warmup parity across implementations is much easier than EMA. "
            "Both LEAN and Pine produce identical SMA values once they "
            "have window-size samples.",
            "Same TV timestamp conventions as other strategies: viewer's "
            "local timezone in TV exports, proper UTC in Engine Lab, "
            "bar-start vs bar-end labeling differs by the bar length.",
            "On the daily variant (see daily_sma_crossover) a 50/200 "
            "cross produces very few signals per year — 1-year backtests "
            "will often show only 1–4 trades.",
        ],
        param_schema=SmaCrossoverParams,
        chart_indicators=(
            StrategyChartIndicator("sma", {"length": ChartParamRef("short_window")}),
            StrategyChartIndicator("sma", {"length": ChartParamRef("long_window")}),
        ),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("resolution_minutes")),
        build=lambda p: _build_sma_crossover_signal_program(p).strategy,  # type: ignore[return-value]
        signal_program_factory=_build_sma_crossover_signal_program,
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
    ),
    "daily_sma_crossover": StrategyRegistration(
        display_name="Daily SMA Crossover",
        class_name="SmaCrossoverAlgorithm",
        description=(
            "Long-term golden-cross / death-cross run against LEAN daily "
            "bars (one daily zip per symbol). Defaults "
            "to the classic 50/200 on AAPL. Same SmaCrossoverAlgorithm as "
            "the intraday variant - only the bar cadence differs."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default AAPL)\n"
            "    resolution = daily (read from the LEAN daily equity zip for the symbol)\n"
            "\n"
            "Indicators (Alpha - updated each daily bar close)\n"
            "    SMA_short = SimpleMovingAverage(short_window days, default 50)\n"
            "    SMA_long  = SimpleMovingAverage(long_window  days, default 200)\n"
            "\n"
            "Warmup\n"
            "    no signals until both SMAs have window-size samples\n"
            "    (typically ~200 trading days for the 50/200 default).\n"
            "\n"
            "Alpha - bar entry conditions (evaluated while flat)\n"
            "    golden_cross = SMA_short > SMA_long\n"
            "                   AND SMA_short[-1] <= SMA_long[-1]\n"
            "    ⇒ if golden_cross: emit Insight(UP); enter long\n"
            "\n"
            "Alpha - bar exit conditions (evaluated while in trade)\n"
            "    death_cross = SMA_short < SMA_long\n"
            "    ⇒ if death_cross: Liquidate(symbol)\n"
            "\n"
            "Risk Management - position survival rules\n"
            "    none - overnight gap risk is not modeled by the fill model;\n"
            "    holding through earnings, splits, and dividends is the\n"
            "    intended behavior of the long-term cross.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (end-of-day fill at the\n"
            "    cross bar's close)"
        ),
        gotchas=[
            "Uses the LEAN daily-bar reader, not a consolidator. Bars come "
            "one-per-day from the LEAN daily equity zip - make sure "
            "those files exist for your chosen symbol.",
            "resolution_minutes is hardcoded to 1440 in the registry's "
            "build function; exposing it as a parameter would let users "
            "break parity with the intended daily cadence.",
            "Classic 50/200 on liquid US equities produces ~1–4 crosses "
            "per year. Choose a 5–10 year window for enough trades to "
            "evaluate performance meaningfully.",
            "No intraday timing — fills are at each signal bar's close "
            "(end of trading day). Slippage/overnight-gap risk is real "
            "but not modeled by the fill model.",
            "Because bars are daily, timezone reporting conventions don't "
            "apply the same way as intraday strategies — TradingView "
            "exports a date (no time) for daily fills.",
        ],
        param_schema=DailySmaCrossoverParams,
        chart_indicators=(
            StrategyChartIndicator("sma", {"length": ChartParamRef("short_window")}),
            StrategyChartIndicator("sma", {"length": ChartParamRef("long_window")}),
        ),
        strategy_bars=StrategyBarCadence("day", 1),
        build=lambda p: SmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            short_window=p.short_window,  # type: ignore[attr-defined]
            long_window=p.long_window,  # type: ignore[attr-defined]
            # 1440 min = 1 day. TradeBarConsolidator is reference-rounded
            # to midnight ET and passes daily bars through 1:1 as long as
            # consecutive inputs are separated by >= 1 day, which is
            # always true for LEAN daily zip rows.
            resolution_minutes=1440,
        ),
        supported_resolutions={"daily"},
    ),
    "rsi_mean_reversion": StrategyRegistration(
        display_name="RSI Mean Reversion",
        class_name="RsiMeanReversionAlgorithm",
        signal_program_contract=SignalProgramContract(
            program_version=_RSI_MEAN_REVERSION_SIGNAL_PROGRAM_VERSION,
            protocol_version=SignalSession.PROTOCOL_VERSION,
            parameter_schema_version=RsiMeanReversionParams.PARAMETER_SCHEMA_VERSION,
            golden_trace_root="46d0b13b5e3307d983b4d5b4a1a21ba790db5e5644d164364a787bb2a12acde7",
            provider="polygon",
            base_timeframe_ms=60_000,
            # Matches validated_settings' resolution_minutes=15 below, the
            # only resolution this contract is qualified for -- same known
            # gap as sma_crossover's own comment here:
            # build_start_program_seal (app/services/signal_program_admission.py)
            # sources SignalDataContract.decision_timeframe_ms from the
            # contract, not the resolved parameter, so a deploy at a
            # non-default resolution_minutes seals a decision_timeframe_ms
            # that disagrees with its own running decision clock (visible via
            # parameters_match_validated_settings=False, but not corrected by
            # it). Outside this registration's scope; sourcing
            # decision_timeframe_ms from the resolved parameter is an
            # admission-service change.
            decision_timeframe_ms=15 * 60_000,
            # RsiMeanReversionAlgorithm.initialize() builds RSI(window=14),
            # needing 15 consolidated 15-minute bars (period + 1, same
            # Wilders warmup override as ema_crossover_signal's own RSI
            # series) before is_ready. 5 calendar days is the same
            # margin-over-minimum buffer ema_crossover_signal uses for that
            # identical ~0.6-session warmup.
            warmup_lookback_days=5,
            # RsiMeanReversionAlgorithm.initialize() constructs exactly this
            # one named series (RSI{window}), fed bar.close at bar.end_ms.
            # warmup_bars is period + 1 -- RelativeStrengthIndex.is_ready
            # overrides the base Indicator's samples >= period to
            # samples >= period + 1 (app/engine/indicators/rsi.py), the same
            # override ema_crossover_signal's own "rsi" series declares.
            signals=(
                SignalSeriesContract(name="rsi", indicator="rsi_wilders", field="close", period=14, warmup_bars=15),
            ),
            decision_streams=tuple(kind.value for kind in SignalIntentKind),
            bar_integrity=SignalBarIntegrityContract(),
            # RsiMeanReversionAlgorithm.evaluate_signal_bar exits the instant
            # its relation (RSI strictly above overbought) is true on a
            # decision clock while in position -- there is no held countdown,
            # so this seals as "level_true" (the same rule sma_crossover
            # uses) rather than restating ema_crossover_signal's fixed 5-bar
            # countdown dishonestly. countdown_state_persistable=False for
            # the same reason as sma_crossover: RsiMeanReversionAlgorithm has
            # not implemented report_state_for_persistence/
            # restore_state_from_persistence/validate_state_payload at all
            # yet -- no state, flat or otherwise, currently survives a
            # Pause/Resume.
            exit_eligibility=ExitEligibilityContract(
                rule="level_true",
                countdown_state_persistable=False,
            ),
            numerical_provenance=NumericalProvenanceContract(
                formula=(
                    "Long-only RSI(window) mean reversion. Entry: RSI(window) drops strictly "
                    "below oversold; exit: RSI(window) rises strictly above overbought. No "
                    "time-based exit -- position is held for as long as RSI stays inside the band."
                ),
                reference=(
                    "Internal strategy retained from the retired pandas-ta service implementation; "
                    "LEAN inspiration but no line-for-line port. No LEAN or TradingView "
                    "reconciliation exists for this promotion -- Polygon live data is unavailable, "
                    "so per PRD direction this program is qualified against its own deterministic "
                    "replay of IBKR-sourced minute bars only, with no cross-engine parity claim."
                ),
                canonical_implementation=(
                    "app/engine/strategy/algorithms/rsi_mean_reversion.py::RsiMeanReversionAlgorithm"
                ),
                validated_against=(
                    "app/engine/tests/test_rsi_mean_reversion_parity.py; "
                    "app/engine/strategy/spec/tests/test_spec_rsi_mean_reversion_parity.py; "
                    "tests/engine/strategy/test_rsi_signal_program.py::"
                    "test_validated_rsi_mean_reversion_settings_corpus_has_a_pinned_trace_root"
                ),
                # The trace/decision identity is Decimal-exact and
                # SHA-256-compared (signal_program.py), not
                # tolerance-compared -- see
                # test_validated_rsi_mean_reversion_settings_corpus_has_a_pinned_trace_root's
                # byte-exact trace_root assertion. Same as sma_crossover:
                # no second, one-level-down LEAN-value-parity claim here --
                # nothing beyond this corpus's own self-consistency has been
                # established for this promotion.
                equivalence_level="bit_exact",
            ),
            parameter_units={
                "symbol": "ticker",
                "window": "bars",
                "oversold": "rsi_points",
                "overbought": "rsi_points",
                "resolution_minutes": "minutes",
            },
            validated_settings={"window": 14, "oversold": 30.0, "overbought": 70.0, "resolution_minutes": 15},
            validated_symbols=("AAPL", "QQQ", "SPY", "TSLA"),
            # Same triage rule as ema_crossover_signal's and sma_crossover's
            # artifact_paths (issue #1728 defect 2): the transitive
            # first-party import closure of the root below, MINUS the files
            # in _RSI_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
            # (scripts/run_signal_program_build_qualification.py) that are
            # provably unreachable from evaluate_signal_bar()'s decision
            # math. test_rsi_signal_decision_digest_closure.py recomputes the
            # closure from these paths and fails the build if a newly
            # introduced import isn't triaged into one bucket or the other.
            artifact_paths=(
                "app/engine/strategy/algorithms/rsi_mean_reversion.py",
                "app/engine/strategy/base.py",
                "app/engine/strategy/signal_intent.py",
                "app/engine/strategy/signal_program.py",
                "app/engine/indicators/base.py",
                "app/engine/indicators/rsi.py",
                "app/engine/consolidators/trade_bar_consolidator.py",
                "app/engine/data/trade_bar.py",
                "app/engine/live/indicator_state.py",
                "app/lean_sidecar/trading_calendar.py",
                "app/utils/timestamps.py",
            ),
        ),
        description=(
            "Long-only RSI threshold strategy. Buys oversold (RSI < oversold), "
            "sells overbought (RSI > overbought). Configurable symbol, window, "
            "thresholds, and resolution. No time-based exit — position is held "
            "as long as RSI stays inside the band."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    RSI = RelativeStrengthIndex(window, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    no signals until RSI is_ready (sample ≥ window+1).\n"
            "    First avg gain/loss is a plain SMA at sample window+1,\n"
            "    Wilders recursion thereafter.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    oversold = RSI < oversold_threshold\n"
            "    ⇒ if oversold: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    overbought = RSI > overbought_threshold\n"
            "    ⇒ if overbought: emit EXIT intent\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — no stop, no time-stop, no max drawdown guard.\n"
            "    Re-entry IS allowed on the same day if RSI dips back\n"
            "    below oversold after an exit (intentional).\n"
            "\n"
            "Portfolio Construction\n"
            "    action plan binds ENTER/EXIT to one long stock leg\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close | next_bar_open"
        ),
        gotchas=[
            "Uses Wilders smoothing — first average gain/loss is a plain "
            "SMA at sample window+1, then the recursion thereafter. Same "
            "warmup convention as the EMA strategy's RSI.",
            "No time-based exit. Position is held for as long as RSI stays "
            "between oversold and overbought. On grinding sideways markets "
            "this can be many bars; on sharp reversals it can be one bar.",
            "No per-day trade limit — once flat, the strategy will re-enter "
            "if RSI drops back below oversold. Multiple entries per day are "
            "possible (this is intentional, unlike the ORB defect).",
            "Default thresholds (30/70) may need adjustment by ticker. "
            "Higher-volatility names rarely reach 30 or 70 on the default "
            "14-period window.",
            "Same TV timestamp conventions as other intraday strategies "
            "(viewer-local-tz exports, bar-start vs bar-end labeling).",
        ],
        param_schema=RsiMeanReversionParams,
        chart_indicators=(StrategyChartIndicator("rsi", {"length": ChartParamRef("window")}),),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("resolution_minutes")),
        build=lambda p: _build_rsi_mean_reversion_signal_program(p).strategy,  # type: ignore[return-value]
        signal_program_factory=_build_rsi_mean_reversion_signal_program,
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
    ),
    "spy_orb": StrategyRegistration(
        display_name="Opening Range Breakout",
        class_name="SpyOpeningRangeBreakout",
        description=(
            "Pure price-action Opening Range Breakout — zero indicator "
            "warmup. The opening range (default: first 3 fifteen-minute "
            "bars of the regular session, i.e. 9:30–10:15 ET) defines a "
            "high/low channel. The first bar that closes above the ORB "
            "high triggers a long entry, provided the range size as a "
            "percent of price falls inside [min_range_pct, max_range_pct]. "
            "Position is held for exactly hold_bars bars (default 5 = 75 "
            "minutes), then flattened. One trade per day max. "
            "\n\n"
            "Designed as a cross-system validation primitive: because "
            "every day starts with no carried state, two implementations "
            "of the same rules cannot drift the way recursive-indicator "
            "strategies (EMA, RSI, MACD) do. Symbol-parameterized, so the "
            "same code runs against SPY, QQQ, IWM, etc."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY; SPY/QQQ/IWM trade cleanly)\n"
            "    resolution = 15-minute bars consolidated from minute data\n"
            "    session    = RTH only (09:30–16:00 America/New_York)\n"
            "\n"
            "Indicators\n"
            "    none — pure price action, zero recursive state across days\n"
            "\n"
            "Warmup\n"
            "    none — every trading day starts with no carried state, which\n"
            "    is what makes ORB ideal as a cross-system validation primitive\n"
            "    (no chance of indicator-state drift between implementations).\n"
            "\n"
            "Alpha — opening-range construction (first orb_bars of each RTH day)\n"
            "    bar_of_day in [1..orb_bars]:\n"
            "        orb_high = max(orb_high, bar.high)\n"
            "        orb_low  = min(orb_low,  bar.low)\n"
            "    on the orb_bars-th bar:\n"
            "        range_pct = (orb_high - orb_low) / orb_low * 100\n"
            "        orb_valid = min_range_pct <= range_pct <= max_range_pct\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat, after ORB completes)\n"
            "    breakout = bar.close > orb_high\n"
            "    ⇒ if orb_valid AND breakout AND NOT traded_today:\n"
            "        emit Insight(UP); enter long; traded_today = True\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new bar\n"
            "    ⇒ when bars_held == hold_bars: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    one trade per day — traded_today flag prevents re-entry\n"
            "    after the time-stop exit (regression test:\n"
            "    test_spy_orb_one_trade_per_day.py).\n"
            "    On days where the hold window crosses 16:00 ET, the exit\n"
            "    bar is the next session's first RTH bar (overnight hold).\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (matches LEAN-style entries)"
        ),
        gotchas=[
            "ONE trade per day. The _traded_today flag was added to the "
            "Python port on 2026-04-18 — prior versions re-entered after "
            "every 5-bar hold exit, producing ~3× the intended trade "
            "count. Regression test: test_spy_orb_one_trade_per_day.py.",
            "RTH-only. The strategy filters on _is_rth(bar.end_time) "
            "against 9:30–16:00 ET hardcoded. If feeding extended-hours "
            "data the pre/post-market bars are ignored by the algorithm "
            "but may show up in data availability checks.",
            "15-min consolidator alignment must be wall-clock / epoch-"
            "anchored (bars at :00 :15 :30 :45) so that the first 3 bars "
            "of each day reliably correspond to 9:30–10:15.",
            "Range filter is percentage-based ((high - low) / low × 100), "
            "so it ports cleanly across tickers — 0.30–1.50% works for "
            "both SPY and QQQ without re-tuning.",
            "On days where a late entry's 5-bar hold crosses the 16:00 ET "
            "close, the exit bar is the next day's first RTH bar. Engine "
            "Lab handles this cleanly; some brokerages would reject the "
            "overnight hold.",
            "Pine Script port: `time(tf, session, tz)` returns the CURRENT "
            "bar's timestamp when in session, not the session's start. "
            'Use `time("D")` change-detection for new-day reset, NOT '
            "change-detection on the session-time value. Earlier Pine "
            "versions had this bug and produced 0 ORBs completed.",
        ],
        param_schema=OrbParams,
        strategy_bars=StrategyBarCadence("minute", 15),
        build=lambda p: SpyOpeningRangeBreakout(
            symbol=p.symbol,  # type: ignore[attr-defined]
            orb_bars=p.orb_bars,  # type: ignore[attr-defined]
            hold_bars=p.hold_bars,  # type: ignore[attr-defined]
            min_range_pct=p.min_range_pct,  # type: ignore[attr-defined]
            max_range_pct=p.max_range_pct,  # type: ignore[attr-defined]
        ),
    ),
    "deployment_validation": StrategyRegistration(
        display_name="Deployment Validation",
        class_name="DeploymentValidationConsecutiveGreen",
        description=(
            "Minute-bar lifecycle validation strategy. Starting with the "
            "09:45 ET minute close, it watches for two consecutive green "
            "minute bars (close > open). After the second green bar it "
            "queues a long entry intended for next-bar-open execution, holds "
            "through the third, fourth, and fifth bars, submits a liquidation "
            "on the fifth bar, resets the detector, and repeats. At 15:45 ET "
            "it stops detecting new entries and flattens any open position. "
            "This is a deployment-validation primitive, not an alpha model."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    signal symbol = configurable (default SPY)\n"
            "    trade symbol  = configurable (defaults to signal symbol)\n"
            "    resolution = 1-minute bars\n"
            "    session    = regular session data; strategy starts at 09:45 ET\n"
            "\n"
            "Indicators\n"
            "    none — only bar.open and bar.close are inspected\n"
            "\n"
            "Entry pattern\n"
            "    green = bar.close > bar.open\n"
            "    if two consecutive eligible green bars occur while flat:\n"
            "        SetHoldings(symbol, 1.0)\n"
            "\n"
            "Hold / exit\n"
            "    intended fill mode is next_bar_open, so the entry fills on\n"
            "    the third bar's open after the two green confirmation bars.\n"
            "    Count bar closes while in position, including the entry bar.\n"
            "    On the fifth bar: Liquidate(symbol)\n"
            "\n"
            "Reset\n"
            "    after each exit fill, reset the green-bar detector. Bars from\n"
            "    the open trade cannot seed the next entry pattern.\n"
            "\n"
            "Session barrier\n"
            "    at 15:45 ET: stop detecting entries and liquidate any exposure\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0) — one open position at a time\n"
            "\n"
            "Execution\n"
            "    intended fill_mode = next_bar_open"
        ),
        gotchas=[
            "Run this strategy with fill_mode=next_bar_open. The global "
            "Engine Lab default is signal_bar_close for legacy LEAN parity, "
            "but this validation strategy's timing spec is next-bar-open.",
            "A green bar means close > open, not close > previous close.",
            "Detection begins on the 09:45 ET bar close and stops at 15:45 ET. "
            "The 15:45 barrier also flattens any open position.",
            "The detector resets after exit, so bars that occurred during an "
            "open position cannot contribute to the next entry pattern.",
        ],
        lean_twin="deployment_validation",
        param_schema=DeploymentValidationParams,
        strategy_bars=StrategyBarCadence("minute", 1),
        hidden_params={"trade_symbol"},
        action_plan_contract="single_long_stock",
        build=lambda p: DeploymentValidationConsecutiveGreen(
            symbol=p.symbol,  # type: ignore[attr-defined]
            trade_symbol=p.trade_symbol,  # type: ignore[attr-defined]
        ),
    ),
    "spy_ema_crossover_options": StrategyRegistration(
        display_name="EMA Crossover Options",
        class_name="SpyEmaCrossoverOptionsAlgorithm",
        description=(
            "This strategy uses the exact same EMA crossover signal as the equity version "
            "— same entry times, same exit times, same 5-bar hold — but instead of buying "
            "the stock, it opens an options spread on each signal. "
            "\n\n"
            "HOW IT WORKS: Every 15-minute bar, EMA(5) and EMA(10) are computed. When "
            "EMA(5) crosses above EMA(10) with at least a 0.20 gap and RSI(14) is between "
            "50-70, the signal fires. The strategy then builds a bull call spread (or bull "
            "put spread, configurable) by selecting two option contracts by delta targeting: "
            "a long leg near 0.60 delta and a short leg near 0.30 delta. The spread is held "
            "for exactly 5 bars (75 minutes) and then closed. "
            "\n\n"
            "BULL CALL SPREAD: Buy the lower-strike call (higher delta, more expensive), "
            "sell the higher-strike call (lower delta, cheaper). You pay a net debit. "
            "Max profit = spread width minus debit. Max loss = the debit paid. Profits "
            "when the underlying moves up. "
            "\n\n"
            "BULL PUT SPREAD: Sell the higher-strike put, buy the lower-strike put. You "
            "receive a net credit. Max profit = the credit received. Max loss = spread "
            "width minus credit. Also profits when the underlying stays flat or moves up. "
            "\n\n"
            "PRICING: In QuantLib-only mode (default), option prices and Greeks are computed "
            "synthetically using Black-Scholes via QuantLib — no market data needed, fast to "
            "backtest. Market-preferred mode uses real Polygon option snapshots when available, "
            "falling back to QuantLib. Market-required mode only trades when real data exists. "
            "\n\n"
            "TRADE PARITY: Signal timing is identical to the equity EMA Crossover strategy. "
            "Every entry and exit bar matches 1:1. The only difference is what is traded — "
            "a defined-risk options spread instead of the underlying stock."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    underlying     = configurable (default SPY)\n"
            "    derivative set = same-underlying option chain, calls & puts\n"
            "    resolution     = 15-min bars consolidated from minute data\n"
            "\n"
            "Indicators (Alpha — identical to ema_crossover, on the underlying)\n"
            "    EMA_fast = ExponentialMovingAverage(ema_fast_period)\n"
            "    EMA_slow = ExponentialMovingAverage(ema_slow_period)\n"
            "    RSI      = RelativeStrengthIndex(rsi_period, Wilders smoothing)\n"
            "\n"
            "Warmup\n"
            "    same as ema_crossover — wait for all three to be is_ready,\n"
            "    prime the prev-cross flag during warmup bars.\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    fresh_cross = EMA_fast > EMA_slow\n"
            "                  AND EMA_fast[-1] <= EMA_slow[-1]\n"
            "    gap_ok      = (EMA_fast - EMA_slow) >= ema_gap_min\n"
            "    rsi_ok      = rsi_min <= RSI <= rsi_max\n"
            "    ⇒ if all three: emit Insight(UP); proceed to spread selection\n"
            "\n"
            "Universe Selection (per signal — narrow the chain)\n"
            "    candidates = chain.filter(min_dte <= DTE <= max_dte,\n"
            "                              open_interest >= min_open_interest,\n"
            "                              volume >= min_volume,\n"
            "                              bid_ask_spread_pct <= max_bid_ask_spread_pct)\n"
            "    if no liquid contract: skip signal (logged)\n"
            "\n"
            "Portfolio Construction (per spread_type)\n"
            "    BULL_CALL: long  call ≈ long_call_delta_target  (0.60)\n"
            "               short call ≈ short_call_delta_target (0.30)\n"
            "               net debit; max profit = width − debit\n"
            "    BULL_PUT:  short put  ≈ short_put_delta_target  (-0.30)\n"
            "               long  put  ≈ long_put_delta_target   (-0.15)\n"
            "               net credit; max profit = credit\n"
            "    sizing = contracts_per_trade (default 1) × contract_multiplier (100)\n"
            "    cap    = max_positions concurrently open\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    bars_held += 1 each new 15-min bar\n"
            "    ⇒ when bars_held == bars_to_hold: close BOTH legs\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    none — defined-risk by construction (max loss = debit\n"
            "    paid for bull-call, width − credit for bull-put). No early\n"
            "    assignment handling; assumes European-style settlement for\n"
            "    the backtest window (acceptable for 7–30 DTE SPY options).\n"
            "\n"
            "Execution / Pricing\n"
            "    pricing_mode = quantlib_only      (synthetic BS via QuantLib)\n"
            "                 | market_preferred   (Polygon snapshot if available)\n"
            "                 | market_required    (skip if no market data)\n"
            "    fills land at the spread's mid ± half_spread_pct"
        ),
        gotchas=[
            "Signal timing is BIT-IDENTICAL to ema_crossover (same EMAs, "
            "same RSI, same gap filter). Differences in trade outcomes vs "
            "the equity strategy come purely from option pricing and "
            "spread mechanics, not from signal differences.",
            "QuantLib-only pricing mode is the default — option prices "
            "and Greeks are computed synthetically via Black-Scholes "
            "with default_iv and risk_free_rate. Switch to "
            "market_preferred or market_required to use real Polygon "
            "option snapshots when available.",
            "Bull-call spread: pay net debit upfront. Max profit = spread width − debit. Max loss = the debit paid.",
            "Bull-put spread: receive net credit upfront. Max profit = the credit. Max loss = spread width − credit.",
            "Delta targeting picks the closest available strike — exact "
            "delta values won't match the targets exactly. The "
            "long_call_delta_target=0.60 / short_call_delta_target=0.30 "
            "defaults give a roughly 1:2 risk:reward profile.",
            "min_open_interest, min_volume, and max_bid_ask_spread_pct "
            "filters can cause some signals to skip if no liquid contract "
            "is available. The skip count is reported in the engine logs.",
            "All EMA/RSI gotchas from ema_crossover apply here unchanged.",
        ],
        param_schema=EmaCrossoverOptionsParams,
        chart_indicators=(
            StrategyChartIndicator("ema", {"length": ChartParamRef("ema_fast_period")}),
            StrategyChartIndicator("ema", {"length": ChartParamRef("ema_slow_period")}),
            StrategyChartIndicator("rsi", {"length": ChartParamRef("rsi_period")}),
        ),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("timeframe_minutes")),
        build=lambda p: SpyEmaCrossoverOptionsAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            ema_fast_period=p.ema_fast_period,  # type: ignore[attr-defined]
            ema_slow_period=p.ema_slow_period,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            ema_gap_min=p.ema_gap_min,  # type: ignore[attr-defined]
            rsi_min=p.rsi_min,  # type: ignore[attr-defined]
            rsi_max=p.rsi_max,  # type: ignore[attr-defined]
            timeframe_minutes=p.timeframe_minutes,  # type: ignore[attr-defined]
            bars_to_hold=p.bars_to_hold,  # type: ignore[attr-defined]
            spread_type=p.spread_type,  # type: ignore[attr-defined]
            min_dte=p.min_dte,  # type: ignore[attr-defined]
            max_dte=p.max_dte,  # type: ignore[attr-defined]
            long_call_delta_target=p.long_call_delta_target,  # type: ignore[attr-defined]
            short_call_delta_target=p.short_call_delta_target,  # type: ignore[attr-defined]
            short_put_delta_target=p.short_put_delta_target,  # type: ignore[attr-defined]
            long_put_delta_target=p.long_put_delta_target,  # type: ignore[attr-defined]
            min_open_interest=p.min_open_interest,  # type: ignore[attr-defined]
            min_volume=p.min_volume,  # type: ignore[attr-defined]
            max_bid_ask_spread_pct=p.max_bid_ask_spread_pct,  # type: ignore[attr-defined]
            contracts_per_trade=p.contracts_per_trade,  # type: ignore[attr-defined]
            max_positions=p.max_positions,  # type: ignore[attr-defined]
            contract_multiplier=p.contract_multiplier,  # type: ignore[attr-defined]
            pricing_mode=p.pricing_mode,  # type: ignore[attr-defined]
            pricing_engine=p.pricing_engine,  # type: ignore[attr-defined]
            risk_free_rate=p.risk_free_rate,  # type: ignore[attr-defined]
            dividend_yield=p.dividend_yield,  # type: ignore[attr-defined]
            default_iv=p.default_iv,  # type: ignore[attr-defined]
            half_spread_pct=p.half_spread_pct,  # type: ignore[attr-defined]
        ),
        # ADR 0009 § 6 — this strategy sizes itself via
        # ``contracts_per_trade`` (an internal options-accounting surface);
        # ``live_config.sizing`` cannot meaningfully override it. The deploy
        # form disables the sizing control + labels it "self-sized".
        sizing_surface="explicit",
    ),
    "spy_strategy_a": StrategyRegistration(
        display_name="Strategy A — EMA-gap + MACD + RSI-range",
        class_name="SpyStrategyAAlgorithm",
        signal_program_contract=SignalProgramContract(
            program_version=_SPY_STRATEGY_A_SIGNAL_PROGRAM_VERSION,
            protocol_version=SignalSession.PROTOCOL_VERSION,
            parameter_schema_version=RsiRangeStrategyAParams.PARAMETER_SCHEMA_VERSION,
            golden_trace_root="7ee88013046505a9df88fa9c83b94e1f5f4c4de92718dd874ca157b5eea25f5f",
            provider="polygon",
            base_timeframe_ms=60_000,
            # Matches validated_settings' resolution_minutes=15 below, the
            # only resolution this contract is qualified for. Same known gap
            # as sma_crossover's contract above: build_start_program_seal
            # sources SignalDataContract.decision_timeframe_ms from this
            # fixed contract value, not the resolved parameter, so a deploy
            # at a non-default resolution_minutes seals with an inaccurate
            # decision_timeframe_ms even though
            # parameters_match_validated_settings already reports False for
            # it. Pre-existing gap, not introduced here — fixing it means an
            # admission-service change outside this registration's scope.
            decision_timeframe_ms=15 * 60_000,
            # RsiRangeStrategy.initialize() + SpyStrategyAAlgorithm's
            # _init_extra_indicators() build five indicators; the longest
            # warmup is EMA_slow(50) = 50 consolidated 15-minute bars = 750
            # minutes, ~1.92 trading sessions (390 min/session, NYSE regular
            # hours). That is much closer to the 2-session boundary (780 min)
            # than sma_crossover's 30-bar/450-minute warmup (58% of 2
            # sessions vs. this program's 96%), so a single early close
            # inside the lookback window could push the true requirement
            # past 2 full sessions -- 9 calendar days is a slightly larger
            # buffer than sma_crossover's 7 for that reason, still covering
            # weekends/holidays.
            warmup_lookback_days=9,
            # SpyStrategyAAlgorithm.initialize() and _init_extra_indicators()
            # construct exactly these five named indicators, each fed
            # bar.close (RSI, EMA_fast, EMA_slow, MACD) or the full bar
            # (ADX) at bar.end_ms. warmup_bars mirrors each indicator's own
            # is_ready threshold (app/engine/indicators/base.py: samples >=
            # period for Indicator/BarIndicator; RelativeStrengthIndex
            # overrides to period + 1; AverageDirectionalIndex overrides to
            # 2 * period; MovingAverageConvergenceDivergence's own `period`
            # bookkeeping value already equals slow_period + signal_period -
            # 1, the sample count at which its internal signal EMA becomes
            # ready) -- any drift between these constants and the real
            # indicators would already change the golden trace root and fail
            # test_validated_spy_strategy_a_settings_corpus_has_a_pinned_trace_root,
            # so this cannot silently go stale.
            signals=(
                SignalSeriesContract(name="ema_fast", indicator="ema", field="close", period=20, warmup_bars=20),
                SignalSeriesContract(name="ema_slow", indicator="ema", field="close", period=50, warmup_bars=50),
                SignalSeriesContract(name="macd", indicator="macd", field="close", period=34, warmup_bars=34),
                SignalSeriesContract(
                    name="rsi", indicator="rsi_wilders", field="close", period=14, warmup_bars=15
                ),
                # ADX reads high/low/close off the full bar (Wilder's
                # directional-movement calculation needs the high/low swing,
                # not just the close) -- "close" would misdescribe what this
                # series actually consumes, hence the wider field literal.
                SignalSeriesContract(
                    name="adx", indicator="adx_wilders", field="high_low_close", period=14, warmup_bars=28
                ),
            ),
            decision_streams=tuple(kind.value for kind in SignalIntentKind),
            bar_integrity=SignalBarIntegrityContract(),
            # SpyStrategyAAlgorithm/RsiRangeStrategy.evaluate_signal_bar exits
            # the instant its exit relation (ADX < adx_exit_threshold) is
            # true on a decision clock -- there is no held countdown, so this
            # seals as "level_true" (same rule sma_crossover's promotion
            # added) rather than dishonestly restating EMA's fixed-countdown
            # rule. countdown_state_persistable=False because
            # RsiRangeStrategy has not implemented
            # report_state_for_persistence/restore_state_from_persistence/
            # validate_state_payload at all -- no state, flat or otherwise,
            # currently survives a Pause/Resume.
            exit_eligibility=ExitEligibilityContract(
                rule="level_true",
                countdown_state_persistable=False,
            ),
            numerical_provenance=NumericalProvenanceContract(
                formula=(
                    "Long-only trend-follower. Entry (while flat): rsi_low_gate <= RSI(rsi_period) <= "
                    "rsi_high_gate AND (EMA(ema_fast_period) - EMA(ema_slow_period)) > ema_gap_threshold AND "
                    "MACD(macd_fast, macd_slow, macd_signal) line > 0. Exit (while in position): "
                    "ADX(adx_period) < adx_exit_threshold."
                ),
                reference=(
                    "Internal strategy, RsiRangeStrategy family (app/engine/strategy/algorithms/"
                    "_rsi_range_base.py); RSI/ADX per Wilder (1978). No LEAN or TradingView "
                    "reconciliation exists for THIS Signal Program promotion -- Polygon live data is "
                    "unavailable, so per PRD direction this program is qualified against its own "
                    "deterministic replay of IBKR-sourced minute bars only, with no cross-engine parity "
                    "claim. This is distinct from golden fixture ENG-008 (tests/fixtures/"
                    "test_strategy_parity_fixtures.py), which pins the base class's own pre/post #1700 "
                    "SignalIntent-port self-equivalence and predates -- and does not establish -- this "
                    "promotion's qualification."
                ),
                canonical_implementation=(
                    "app/engine/strategy/algorithms/spy_strategy_a.py::SpyStrategyAAlgorithm"
                ),
                validated_against=(
                    "app/engine/tests/ (gate-wiring unit tests); golden fixture ENG-008 "
                    "(tests/fixtures/test_strategy_parity_fixtures.py); "
                    "tests/engine/strategy/test_spy_strategy_a_signal_program.py::"
                    "test_validated_spy_strategy_a_settings_corpus_has_a_pinned_trace_root"
                ),
                # The trace/decision identity is Decimal-exact and
                # SHA-256-compared (signal_program.py), not
                # tolerance-compared -- see
                # test_validated_spy_strategy_a_settings_corpus_has_a_pinned_trace_root's
                # byte-exact trace_root assertion. Same as sma_crossover:
                # nothing beyond this corpus's own self-consistency has been
                # established for this promotion, so no
                # tolerance_atol/tolerance_rtol/parity_fixture_ids either.
                equivalence_level="bit_exact",
            ),
            parameter_units={
                "symbol": "ticker",
                "ema_fast_period": "bars",
                "ema_slow_period": "bars",
                "ema_gap_threshold": "quote_currency",
                "macd_fast": "bars",
                "macd_slow": "bars",
                "macd_signal": "bars",
                "rsi_period": "bars",
                "rsi_low_gate": "rsi_points",
                "rsi_high_gate": "rsi_points",
                "adx_period": "bars",
                "adx_exit_threshold": "adx_points",
                "resolution_minutes": "minutes",
            },
            validated_settings={
                "ema_fast_period": 20,
                "ema_slow_period": 50,
                "ema_gap_threshold": 0.5,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "rsi_period": 14,
                "rsi_low_gate": 38.0,
                "rsi_high_gate": 70.0,
                "adx_period": 14,
                "adx_exit_threshold": 15.0,
                "resolution_minutes": 15,
            },
            validated_symbols=("AAPL", "QQQ", "SPY", "TSLA"),
            # Same triage rule as sma_crossover's artifact_paths (issue #1728
            # defect 2): the transitive first-party import closure of the two
            # roots below, MINUS the files in
            # _SPY_STRATEGY_A_SIGNAL_DECISION_CLOSURE_EXCLUSIONS
            # (scripts/run_signal_program_build_qualification.py) that are
            # provably unreachable from evaluate_signal_bar()'s decision
            # math. test_spy_strategy_a_signal_decision_digest_closure.py
            # recomputes the closure from these paths and fails the build if
            # a newly introduced import isn't triaged into one bucket or the
            # other.
            artifact_paths=(
                "app/engine/strategy/algorithms/spy_strategy_a.py",
                "app/engine/strategy/algorithms/_rsi_range_base.py",
                "app/engine/strategy/base.py",
                "app/engine/strategy/signal_intent.py",
                "app/engine/strategy/signal_program.py",
                "app/engine/indicators/base.py",
                "app/engine/indicators/ema.py",
                "app/engine/indicators/sma.py",
                "app/engine/indicators/macd.py",
                "app/engine/indicators/rsi.py",
                "app/engine/indicators/adx.py",
                "app/engine/consolidators/trade_bar_consolidator.py",
                "app/engine/data/trade_bar.py",
                "app/engine/live/indicator_state.py",
                "app/lean_sidecar/trading_calendar.py",
                "app/utils/timestamps.py",
            ),
        ),
        description=(
            "Long-only 15-minute trend-follower. On each bar while flat, enters "
            "long if RSI is inside the [rsi_low_gate, rsi_high_gate] range AND "
            "(EMA_fast − EMA_slow) exceeds a threshold AND MACD line > 0. Exits "
            "when ADX drops below 15. Pyramiding=1 prevents re-entry while "
            "holding. Validated against TradingView via trade-by-trade CSV "
            "reconciliation."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    EMA_fast = ExponentialMovingAverage(ema_fast_period, default 20)\n"
            "    EMA_slow = ExponentialMovingAverage(ema_slow_period, default 50)\n"
            "    MACD     = MovingAverageConvergenceDivergence(\n"
            "                   macd_fast, macd_slow, macd_signal)\n"
            "    RSI      = RelativeStrengthIndex(rsi_period, Wilders smoothing)\n"
            "    ADX      = AverageDirectionalIndex(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~200 bars by default (longest indicator chain is\n"
            "    EMA_slow=50 plus a buffer for steady state).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    gap_ok       = (EMA_fast - EMA_slow) > ema_gap_threshold\n"
            "    macd_bull    = MACD.line > 0     (line, NOT histogram, NOT signal)\n"
            "    ⇒ if all three: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 15)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in / re-entry while holding.\n"
            "    no SL / no TP — drawdown is unbounded until ADX collapses.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = next_bar_open  (TradingView-parity setting;\n"
            "    signals fire at bar close, fill price = next bar's open)"
        ),
        gotchas=[
            "EMA gap threshold is ticker-scaled. Default 0.5 is a reasonable "
            "SPY 15-min starting point; other tickers scale with price.",
            "MACD gate is the MACD line itself (fast_EMA − slow_EMA), NOT the histogram and NOT the signal line.",
            "Fills land on NEXT_BAR_OPEN — the strategy signals at the bar "
            "close, the fill price is the next bar's open. Set the engine's "
            "fill mode accordingly for TV parity.",
            "Warmup is 200 bars by default — longest indicator chain is EMA_slow(50) plus buffer.",
            "No SL/TP — TV defaults. Worst-case drawdown per trade is unbounded until ADX drops below 15.",
        ],
        param_schema=RsiRangeStrategyAParams,
        chart_indicators=(
            StrategyChartIndicator("ema", {"length": ChartParamRef("ema_fast_period")}),
            StrategyChartIndicator("ema", {"length": ChartParamRef("ema_slow_period")}),
            StrategyChartIndicator(
                "macd",
                {
                    "fast": ChartParamRef("macd_fast"),
                    "slow": ChartParamRef("macd_slow"),
                    "signal": ChartParamRef("macd_signal"),
                },
            ),
            StrategyChartIndicator("rsi", {"length": ChartParamRef("rsi_period")}),
            StrategyChartIndicator("adx", {"length": ChartParamRef("adx_period")}),
        ),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("resolution_minutes")),
        pine_generator=generate_strategy_a_pine,
        build=lambda p: _build_spy_strategy_a_signal_program(p).strategy,  # type: ignore[return-value]
        signal_program_factory=_build_spy_strategy_a_signal_program,
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
    ),
    "spy_strategy_b": StrategyRegistration(
        display_name="Strategy B — Supertrend + ADX + MACD + RSI-range",
        class_name="SpyStrategyBAlgorithm",
        description=(
            "Long-only 15-minute momentum strategy. Same RSI-range filter as "
            "Strategy A. On each bar while flat, enters long if RSI is in "
            "range, Supertrend is long, ADX > 20 (strong trend), and MACD "
            "line > 0. Exits when ADX drops below 20."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    Supertrend = Supertrend(supertrend_atr_period,\n"
            "                            supertrend_multiplier)   default (10, 3)\n"
            "    MACD       = MACD(macd_fast, macd_slow, macd_signal)\n"
            "    RSI        = RSI(rsi_period, Wilders)\n"
            "    ADX        = ADX(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~50 bars minimum (Supertrend = atr_period bars,\n"
            "    MACD ≈ 34 bars to converge, RSI ≈ 15, plus buffer).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range  = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    trend_long    = Supertrend.is_long\n"
            "    trend_strong  = ADX > adx_entry_threshold (default 20)\n"
            "    macd_bull     = MACD.line > 0\n"
            "    ⇒ if all four: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 20)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in while holding.\n"
            "    no SL / no TP — exit is purely trend-strength-driven.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (default) | next_bar_open"
        ),
        gotchas=[
            "Supertrend here uses the pandas-ta direction convention "
            "(1 = uptrend / bullish, −1 = downtrend / bearish). Pine's "
            "ta.supertrend() returns the *opposite* sign. The `is_long` "
            "property abstracts this away — read that, not the raw "
            "direction integer.",
            "Supertrend default (ATR=10, multiplier=3) matches the Pine defaults. pandas-ta's default is ATR=7.",
            "ADX entry threshold and ADX exit threshold are independent "
            "parameters — the default pair (20 in / 20 out) means the "
            "strategy exits as soon as trend strength weakens below its "
            "own entry condition.",
            "Supertrend needs at least `atr_period` bars of warmup before "
            "emitting a direction. Combined with MACD's 34-bar warmup and "
            "RSI's 15-bar warmup, plan for ~50 bars minimum before the "
            "first possible trade.",
        ],
        param_schema=RsiRangeStrategyBParams,
        chart_indicators=(
            StrategyChartIndicator(
                "supertrend",
                {
                    "length": ChartParamRef("supertrend_atr_period"),
                    "multiplier": ChartParamRef("supertrend_multiplier"),
                },
            ),
            StrategyChartIndicator(
                "macd",
                {
                    "fast": ChartParamRef("macd_fast"),
                    "slow": ChartParamRef("macd_slow"),
                    "signal": ChartParamRef("macd_signal"),
                },
            ),
            StrategyChartIndicator("rsi", {"length": ChartParamRef("rsi_period")}),
            StrategyChartIndicator("adx", {"length": ChartParamRef("adx_period")}),
        ),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("resolution_minutes")),
        pine_generator=generate_strategy_b_pine,
        build=lambda p: SpyStrategyBAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            supertrend_atr_period=p.supertrend_atr_period,  # type: ignore[attr-defined]
            supertrend_multiplier=p.supertrend_multiplier,  # type: ignore[attr-defined]
            adx_entry_threshold=p.adx_entry_threshold,  # type: ignore[attr-defined]
            macd_fast=p.macd_fast,  # type: ignore[attr-defined]
            macd_slow=p.macd_slow,  # type: ignore[attr-defined]
            macd_signal=p.macd_signal,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            rsi_low_gate=p.rsi_low_gate,  # type: ignore[attr-defined]
            rsi_high_gate=p.rsi_high_gate,  # type: ignore[attr-defined]
            adx_period=p.adx_period,  # type: ignore[attr-defined]
            adx_exit_threshold=p.adx_exit_threshold,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
    ),
    "spy_strategy_c": StrategyRegistration(
        display_name="Strategy C — ADX-rising + RSI-range",
        class_name="SpyStrategyCAlgorithm",
        description=(
            "Long-only 15-minute strategy with the simplest gate: on each "
            "bar while flat, enter long if RSI is in range, ADX > 20 AND "
            "ADX is rising bar-over-bar (ADX[i] > ADX[i-1]). Exits when "
            "ADX drops below 15 (same exit rule as Strategy A)."
        ),
        algorithm_pseudocode=(
            "Universe\n"
            "    symbol     = configurable (default SPY)\n"
            "    resolution = consolidated to resolution_minutes (default 15m)\n"
            "\n"
            "Indicators (Alpha — updated each bar at bar.end_time)\n"
            "    RSI = RelativeStrengthIndex(rsi_period, Wilders)\n"
            "    ADX = AverageDirectionalIndex(adx_period, Wilders)\n"
            "\n"
            "Warmup\n"
            "    ~30 bars (longest dependency is ADX, Wilders-smoothed).\n"
            "\n"
            "Alpha — bar entry conditions (evaluated while flat)\n"
            "    rsi_in_range = rsi_low_gate <= RSI <= rsi_high_gate\n"
            "    trend_strong = ADX > adx_entry_threshold (default 20)\n"
            "    adx_rising   = ADX > ADX[-1]    (strict; flat ADX does NOT count)\n"
            "    ⇒ if all three: emit Insight(UP); enter long\n"
            "\n"
            "Alpha — bar exit conditions (evaluated while in trade)\n"
            "    trend_weak = ADX < adx_exit_threshold (default 15)\n"
            "    ⇒ if trend_weak: Liquidate(symbol)\n"
            "\n"
            "Risk Management — position survival rules\n"
            "    pyramiding=1 — no scale-in while holding.\n"
            "    no SL / no TP. Cleanest test of the RSI-range + ADX-strength\n"
            "    combination — no MACD or Supertrend confounds.\n"
            "\n"
            "Portfolio Construction\n"
            "    SetHoldings(symbol, 1.0)  — single-position, all-in\n"
            "\n"
            "Execution\n"
            "    fill_mode = signal_bar_close (default) | next_bar_open"
        ),
        gotchas=[
            "'ADX rising' is strictly greater than the prior bar. Flat ADX "
            "(exactly equal to prior) does NOT count as rising.",
            "No MACD, no Supertrend — this is the cleanest test of the RSI-range + ADX-strength combination.",
            "Same exit threshold as Strategy A by default (15). The user's "
            "TV screenshot had C's exit rule cropped; we inherit A's.",
        ],
        param_schema=RsiRangeStrategyCParams,
        chart_indicators=(
            StrategyChartIndicator("rsi", {"length": ChartParamRef("rsi_period")}),
            StrategyChartIndicator("adx", {"length": ChartParamRef("adx_period")}),
        ),
        strategy_bars=StrategyBarCadence("minute", ChartParamRef("resolution_minutes")),
        pine_generator=generate_strategy_c_pine,
        build=lambda p: SpyStrategyCAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            adx_entry_threshold=p.adx_entry_threshold,  # type: ignore[attr-defined]
            rsi_period=p.rsi_period,  # type: ignore[attr-defined]
            rsi_low_gate=p.rsi_low_gate,  # type: ignore[attr-defined]
            rsi_high_gate=p.rsi_high_gate,  # type: ignore[attr-defined]
            adx_period=p.adx_period,  # type: ignore[attr-defined]
            adx_exit_threshold=p.adx_exit_threshold,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
        instrument_surface="policy",
        action_plan_contract="single_long_stock",
        signal_intent_binding="action_plan_stock",
    ),
}

# Keep the new strategy structurally coupled to the canonical EMA signal
# registration. Only its gap rule, names, and LEAN twin identity differ.
_ema_signal_registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
_STRATEGY_REGISTRY["ema_crossover_2_bps"] = replace(
    _ema_signal_registration,
    display_name="EMA Crossover 2 bps",
    class_name="EmaCrossover2BpsAlgorithm",
    description=_ema_signal_registration.description.replace(
        "a 0.20 minimum gap",
        "a configurable relative gap (2 basis points by default)",
    ),
    algorithm_pseudocode=_ema_signal_registration.algorithm_pseudocode.replace(
        "gap_ok      = (EMA_fast - EMA_slow) >= 0.20",
        "gap_bps     = 10,000 * (EMA_fast - EMA_slow) / EMA_slow\n"
        "    gap_ok      = gap_bps >= gap_bps_parameter (default 2)",
    ),
    gotchas=[
        *_ema_signal_registration.gotchas,
        "The gap threshold is relative to EMA(10): 2 bps means 0.02%, "
        "not $0.02 and not 2%. At a $500 slow EMA, the boundary is $0.10.",
    ],
    param_schema=EmaCrossover2BpsParams,
    build=lambda p: EmaCrossover2BpsAlgorithm(
        symbol=p.symbol,  # type: ignore[attr-defined]
        gap_bps=p.gap_bps,  # type: ignore[attr-defined]
        rsi_min=p.rsi_min,  # type: ignore[attr-defined]
        rsi_max=p.rsi_max,  # type: ignore[attr-defined]
    ),
    lean_twin="ema_crossover_2_bps",
    lean_parameter_names=("gap_bps", "rsi_min", "rsi_max"),
    # Issue #1728 defect 1: dataclasses.replace() shallow-copies every field
    # not explicitly overridden, including signal_program_contract and
    # signal_program_factory. This strategy uses a relative basis-point gap
    # (EmaCrossover2BpsAlgorithm, param gap_bps) — genuinely different math
    # from the canonical EMA Signal Program's absolute-price gap — and has
    # never been through its own golden qualification. Left inherited, both
    # bots on this key falsely claimed EMA's golden_trace_root as proof of
    # their own build, AND signal_program_factory would hand
    # _build_ema_crossover_signal_program a params instance it isn't typed
    # for. Clearing both routes admission's prove_running_program_build to
    # its NOT_APPLICABLE state (no registered Signal Program), which
    # run_admission.py does not gate on, keeping this strategy's existing
    # execution path working. Promoting it to a real sealed Signal Program
    # is issue #1730 / Slice 5.
    signal_program_contract=None,
    signal_program_factory=None,
)

# Historical ledgers use this key and import path. Keep it executable but do
# not offer it as a second picker option beside the migrated signal strategy.
_STRATEGY_REGISTRY["spy_ema_crossover"] = replace(
    _STRATEGY_REGISTRY["ema_crossover_signal"],
    display_name="EMA Crossover (legacy compatibility)",
    class_name="SpyEmaCrossoverAlgorithm",
    build=lambda p: SpyEmaCrossoverAlgorithm(symbol=p.symbol),  # type: ignore[attr-defined]
    instrument_surface="explicit",
    action_plan_contract="none",
    signal_intent_binding="signal_symbol",
    catalog_visible=False,
    lean_twin="ema_crossover",
    # Issue #1728 defect 1: see the identical comment on ema_crossover_2_bps
    # above. This legacy-compatibility key builds SpyEmaCrossoverAlgorithm
    # (action_plan_contract="none"), not the canonical EMA Signal Program —
    # inheriting its contract/factory via dataclasses.replace() let bots on
    # this key falsely claim EMA's golden_trace_root, and
    # prove_running_program_build's receipt lookup (keyed on
    # binding.strategy_key) could never match, permanently bricking Start
    # and Resume for every such bot. Clearing both is what routes it back to
    # NOT_APPLICABLE — no registered Signal Program — instead of UNPROVEN.
    signal_program_contract=None,
    signal_program_factory=None,
)


__all__ = [
    "_STRATEGY_REGISTRY",
    "ChartParamRef",
    "StrategyBarCadence",
    "StrategyChartIndicator",
    "StrategyParamsBase",
    "StrategyRegistration",
]
