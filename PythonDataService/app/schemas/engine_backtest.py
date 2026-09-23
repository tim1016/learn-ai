"""Wire contract for the in-process engine backtest (``POST /api/engine/backtest``).

The request, its canonical ``DataPolicy`` block, and the response shapes are
shared by the router (request validation, response shape) and
:mod:`app.services.engine_backtest_service` (the workflow that fills them), so
they live here rather than in either (#1999).
"""

from __future__ import annotations

from datetime import time as time_of_day
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.models.responses import LeanStatisticsResponse
from app.schemas.engine_validation import EngineValidationAnalyticsResponse
from app.schemas.run_verdict import RunVerdict


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class EngineBacktestRequest(BaseModel):
    """Backtest request for the in-process LEAN-compatible engine.

    Does NOT inherit ``TickerRequest`` because:
      - Dates are *optional* overrides (``None`` lets the strategy use
        its own defaults), not required.
      - The engine uses ``resolution: Literal["minute", "daily"]``
        instead of the base's ``timespan: Literal["minute","hour","day"]``
        (no "hour", and the field name differs).
      - Symbol is strategy-owned (set by the strategy's Initialize-equivalent),
        not a top-level form field.
      - No ``multiplier`` / ``session`` concepts at this layer.

    What does change in this PR: ``start_date`` / ``end_date`` are
    renamed to ``from_date`` / ``to_date`` to align with the canonical
    naming. The legacy names continue to be accepted via Pydantic
    ``AliasChoices`` during the PR (ii) → (iii) transition window;
    they're removed in PR (iii).
    """

    model_config = ConfigDict(populate_by_name=True)

    strategy_name: str = Field(..., description="Registered strategy identifier")
    requested_engine: Literal["python", "both"] = Field(
        "python",
        description="Operator-selected execution mode, persisted for exact History rehydration.",
    )
    fill_mode: str = Field(
        "signal_bar_close",
        description="Fill mode: signal_bar_close or next_bar_open",
    )
    commission_per_order: float = Field(1.0, ge=0)
    slippage_per_share: float = Field(
        0.0,
        ge=0,
        description=(
            "Per-share slippage applied against the trade direction at fill. "
            "Defaults to 0 to preserve LEAN-parity for bit-exact runs; pass a "
            "non-zero value (e.g. 0.02 = 2 ticks for US equities) to model a "
            "more realistic execution cost."
        ),
    )
    session_entry_cutoff: time_of_day | None = Field(
        None,
        description=(
            "After this time-of-day, entry orders (those that would grow "
            "|position|) are dropped. Exits still fill. Interpreted in the "
            "timezone of the bar data. Example: '15:55:00' for ET data."
        ),
    )
    force_flat_at: time_of_day | None = Field(
        None,
        description=(
            "At the first minute bar whose wall-clock time reaches this "
            "value, the engine cancels all queued / deferred orders, clears "
            "active TP/SL brackets, closes every open position at that "
            "minute's close, and calls strategy.on_force_flat(). Once per "
            "calendar day. Example: '15:58:00' for ET data."
        ),
    )
    limit_penetration: float = Field(
        0.0,
        ge=0,
        description=(
            "Dollar amount the bar must penetrate past a resting limit's "
            "price before the fill is recognized. Measured against the "
            "adverse extreme — low for buy limits, high for sell limits. "
            "Default 0 = TradingView-style touch fill; 0.02 for US "
            "equities is a realistic 2-tick queue-position model."
        ),
    )
    # Optional overrides — when omitted, the strategy's own defaults (set in
    # its Initialize equivalent) are used.
    from_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: start_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: end_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    # Warmup / evaluation-window semantics (PRD #1925, "the gate"). Bars from
    # ``warmup_from_date`` up to ``from_date`` prime indicators and stateful
    # primitives; at ``from_date`` the engine resets execution state and every
    # reported figure — trades, curve, statistics, bars, insights, logs — is
    # scoped to ``[from_date, to_date]``. Omitted (the default) is the
    # historical single-phase run, byte-identical to before this field existed.
    warmup_from_date: str | None = Field(
        None,
        description=(
            "YYYY-MM-DD. Optional earlier data boundary used only to prime "
            "indicators; must precede from_date, which then marks where "
            "scoring starts. Every reported figure is scoped to the "
            "evaluation window [from_date, to_date]."
        ),
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    # Study persistence is unconditional by default because Strategy Lab's
    # history depends on it. A sweep that keeps its own summary rows opts out
    # (PRD #1926: "The per-backtest study save is suppressed").
    save_study: bool = Field(
        True,
        description=(
            "Persist this run as a Strategy Lab study and dispatch its parity "
            "companion. False keeps the engine result only — for callers such "
            "as Grid Search that record their own summary rows."
        ),
    )
    # A summary run answers the question a sweep cell asks — statistics, trade
    # list, counts — without the per-bar artifacts that question never reads:
    # the response's equity curve, chart bars, insights, LEAN-parity
    # statistics and validation analytics are left empty, and the engine does
    # not retain the bars it iterated (``bars_consumed`` carries the count).
    # At minute resolution over two years those artifacts are ~194k points a
    # cell held only to discard (#1941). Statistics are computed from the same
    # equity samples either way and are byte-identical to a full run.
    summary_only: bool = Field(
        False,
        description=(
            "Omit the per-bar response artifacts (equity curve, chart bars, "
            "insights, LEAN statistics, validation analytics) and retain no "
            "bars. Statistics and trades are identical to a full run; "
            "bars_consumed carries the bar count the curve would have had."
        ),
    )
    initial_cash: float | None = Field(None, ge=0)
    # Strategy-specific parameters — validated per-strategy against the
    # corresponding ``StrategyParamsBase`` subclass in the registry. Left
    # untyped here because the schema varies per strategy.
    params: dict[str, Any] = Field(default_factory=dict)
    # Data resolution the engine will read. ``"minute"`` feeds
    # ``LeanMinuteDataReader`` (the Phase 1 default, used by every
    # intraday strategy that consolidates up to 15m/1h/etc.).
    # ``"daily"`` feeds ``LeanDailyDataReader`` and is reserved for
    # strategies that declare themselves daily-native.
    resolution: Literal["minute", "daily"] = Field(
        "minute",
        description="Data resolution: 'minute' (default) or 'daily'",
    )
    # When true, the router will materialize any missing data for the
    # run's symbol + date range into the cache root before starting the
    # engine. Defaults to false so the SPY fixture path (which should
    # always hit the reference mount) is never accidentally fetched.
    auto_fetch: bool = Field(
        False,
        description="Fetch missing data from Polygon into the cache before running",
    )
    compatibility_profile: Literal["us-equity-raw-ibkr-v1"] | None = Field(
        None,
        description=(
            "Pinned cross-engine execution contract. The v1 profile requires raw "
            "regular-session minute bars and enables LEAN SetHoldings sizing, IBKR "
            "equity fees, and stale session-close next-open fills."
        ),
    )

    # PR B (2026-05-19) — canonical DataPolicy block. Optional on the wire
    # so legacy callers (any pre-PR-B UI build) still work; when omitted, a
    # default block is synthesized from ``params.symbol`` + ``resolution``.
    # The shape mirrors ``app.lean_sidecar.data_policy.DataPolicy`` so the
    # GraphQL/engine compare-view sees an identical schema on both engines.
    data_policy: _EngineDataPolicyModel | None = Field(
        None,
        description=(
            "Canonical DataPolicy block (PR B). When omitted, synthesized "
            "from ``params.symbol`` + ``resolution`` with ``adjusted=true`` "
            "and ``session='regular'``. Required when ``params.symbol`` is "
            "absent (no source of truth for the synthesizer)."
        ),
    )

    @model_validator(mode="after")
    def _synthesize_legacy_data_policy(self) -> EngineBacktestRequest:
        """Synthesize a default ``DataPolicy`` when the caller omits it.

        One-deprecation-cycle compat. The pre-PR-B engine wire shape
        carried ``symbol`` inside ``params`` and the resolution in the
        top-level field; we synthesize a canonical block from those two
        signals so the row written to ``StrategyExecution`` always has a
        ``DataPolicyJson``. Synthesis defaults match the engine's actual
        runtime behavior today (Polygon-sourced, pre-adjusted, regular
        session, m/1 → m/1; the strategy's own consolidator handles any
        further intra-strategy timeframe).

        When BOTH ``data_policy`` and ``params.symbol`` are absent we
        leave ``data_policy=None`` rather than raising. Legacy clients
        that POST ``params={}`` rely on the strategy registry's default
        symbol (e.g., SPY) being resolved downstream; failing
        validation here would short-circuit one-cycle compat. Downstream
        consumers (``persist_engine_response_sync``, response serialization) already
        treat ``data_policy is None`` as "policy unknown at request
        time" and emit a null data policy; the run record then
        synthesizes a legacy block from the symbol in that case (see
        ``app.research.backtest_runs.records.synthesize_legacy_data_policy``).
        """
        if self.data_policy is not None:
            return self
        symbol = self.params.get("symbol") if isinstance(self.params, dict) else None
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            # Legacy compat: defer synthesis to downstream code once the
            # strategy registry has resolved its default symbol.
            return self
        timespan = "day" if self.resolution == "daily" else "minute"
        self.data_policy = _EngineDataPolicyModel(
            source="polygon",
            symbol=symbol.strip().upper(),
            adjusted=True,
            session="regular",
            input_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
            strategy_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
        )
        return self

    @model_validator(mode="after")
    def _validate_warmup_window(self) -> EngineBacktestRequest:
        """A warmup boundary needs an evaluation start to hand over to."""
        if self.warmup_from_date is None:
            return self
        if self.from_date is None:
            raise ValueError("warmup_from_date requires from_date: the evaluation window needs an explicit start")
        if self.warmup_from_date >= self.from_date:
            raise ValueError(
                f"warmup_from_date={self.warmup_from_date!r} must precede from_date={self.from_date!r}; "
                "the warmup phase ends where scoring begins"
            )
        return self

    @model_validator(mode="after")
    def _validate_symbol_agreement(self) -> EngineBacktestRequest:
        """Reject a request whose two symbol surfaces name different instruments.

        The engine reads the instrument from two places that nothing else
        reconciles: the Python strategy is built from ``params.symbol``, while
        ``_pin_compatibility_fixture``, ``_record_actual_strategy_bars``, the
        persisted ``DataPolicyJson``, and the LEAN parity companion all read
        ``data_policy.symbol``. A caller that supplies both may legally send
        ``params.symbol="AAPL"`` with ``data_policy.symbol="SPY"``, and the run
        would then execute AAPL while pinning SPY's bar fixture and dispatching
        a SPY companion -- a parity verdict comparing different instruments,
        which is worse than no verdict because it looks like a finding
        (#1917 review).

        Only an explicitly supplied policy can disagree: the synthesizer above
        derives the policy's symbol from ``params.symbol``. Comparison is on
        the trimmed uppercase form, because that is exactly the normalization
        the synthesizer applies.
        """
        if self.data_policy is None:
            return self
        supplied = self.params.get("symbol") if isinstance(self.params, dict) else None
        if not isinstance(supplied, str) or not supplied.strip():
            return self
        requested = supplied.strip().upper()
        policy_symbol = self.data_policy.symbol.strip().upper()
        if requested != policy_symbol:
            raise ValueError(
                f"params.symbol={requested!r} disagrees with data_policy.symbol={policy_symbol!r}; "
                "both name the instrument this run trades and must match"
            )
        return self

    @model_validator(mode="after")
    def _validate_compatibility_profile(self) -> EngineBacktestRequest:
        if self.compatibility_profile is None:
            return self
        if self.data_policy is None or self.data_policy.adjusted:
            raise ValueError("us-equity-raw-ibkr-v1 requires raw bars (adjusted=false)")
        if self.data_policy.session != "regular" or self.resolution != "minute":
            raise ValueError("us-equity-raw-ibkr-v1 requires regular-session minute bars")
        if self.fill_mode != "signal_bar_close":
            raise ValueError("us-equity-raw-ibkr-v1 requires fill_mode=signal_bar_close")
        if (
            self.slippage_per_share != 0
            or self.limit_penetration != 0
            or self.session_entry_cutoff is not None
            or self.force_flat_at is not None
        ):
            raise ValueError("us-equity-raw-ibkr-v1 does not permit execution overrides")
        return self

    @model_validator(mode="after")
    def _validate_summary_only_does_not_persist(self) -> EngineBacktestRequest:
        """A summary run cannot be persisted: the study row would be wrong at rest.

        ``summary_only`` exists for a caller that reads the statistics and
        discards the per-bar evidence — a Grid Search cell (#1941). A study
        row is the opposite contract: it persists the equity curve, chart
        bars and LEAN statistics the summary run leaves empty. Allowed
        through, ``_strict_equity_report`` would narrow the stored window to
        first-trade→last-trade on the empty curve (a real value silently
        becoming a wrong one) or fail into ``study_id=None`` with no signal,
        and a ``requested_engine="both"`` run would then grade a parity
        companion against that row. The one caller that pairs the flags
        correctly (Grid Search) already sends ``save_study=False``.
        """
        if self.summary_only and self.save_study:
            raise ValueError(
                "summary_only cannot be combined with save_study: a persisted study needs the "
                "per-bar evidence a summary run omits; send save_study=false"
            )
        return self


# ---------------------------------------------------------------------------
# DataPolicy + BarsSpec pydantic shapes (engine-side mirror)
# ---------------------------------------------------------------------------
# PR B (2026-05-19) — the engine surface accepts the canonical DataPolicy
# block on its inbound request. Mirrors ``app.lean_sidecar.data_policy.DataPolicy``
# and the leading-underscore models in ``app.routers.lean_sidecar`` (kept
# local here to avoid importing a leading-underscore name across modules).
class _EngineBarsSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timespan: Literal["minute", "hour", "day"]
    multiplier: int = Field(..., ge=1)


class _EngineDataPolicyModel(BaseModel):
    """Pydantic shape for the canonical ``DataPolicy`` block on the engine surface.

    Identical to ``_DataPolicyModel`` in ``app.routers.lean_sidecar`` (and
    to ``app.lean_sidecar.data_policy.DataPolicy``); duplicated here only
    because the lean_sidecar module is leading-underscore private. A
    future PR can extract a shared neutral module.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["synthetic", "polygon"]
    symbol: str
    adjusted: bool = True
    session: Literal["regular", "extended"]
    input_bars: _EngineBarsSpecModel
    strategy_bars: _EngineBarsSpecModel
    timestamp_policy: Literal["bar_close_ms_utc"] = "bar_close_ms_utc"
    timezone: Literal["America/New_York"] = "America/New_York"
    provider_kind: Literal["live", "fixture"] = "live"
    fixture_id: str | None = None
    fixture_sha256: str | None = None


EngineBacktestRequest.model_rebuild()


class EngineTradeResponse(BaseModel):
    trade_number: int
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    # Filled share/contract count from the engine's fill model. Required for
    # downstream dollar-PnL persistence — without it, ``BacktestTrade.Quantity``
    # defaults to 1 on the .NET side and the persisted PnL silently diverges
    # from the actual run by a factor of ``quantity``. See
    # ``.claude/rules/numerical-rigor.md`` → ``QUANTITY_MISMATCH``.
    quantity: int
    # Per-trade indicator snapshot captured at the entry signal. Keys depend
    # on the strategy — e.g. SPY returns ``ema5``/``ema10``/``rsi``, SMA
    # crossover returns ``sma_10``/``sma_30``. The frontend renders these
    # dynamically rather than expecting a fixed shape.
    indicators: dict[str, float] = Field(default_factory=dict)
    pnl_pts: float
    pnl_pct: float
    result: str
    signal_reason: str = ""
    is_synthetic_exit: bool = False


class EngineEvaluationWindowResponse(BaseModel):
    """The interval table a primed run actually executed (PRD #1926 F11).

    ``data_start_ms`` is the ET-midnight anchor of the first date the engine
    read; ``evaluation_start_ms`` is where scoring began and execution state
    was reset; ``evaluation_end_ms`` is the half-open end (ET midnight after
    the last evaluated date). Every figure on the response describes
    ``[evaluation_start_ms, evaluation_end_ms)``. For an ordinary run the two
    starts coincide and ``warmup_primed`` is false. All ``int64 ms UTC``.
    """

    data_start_ms: int
    evaluation_start_ms: int
    evaluation_end_ms: int
    warmup_primed: bool


class EngineBacktestResponse(BaseModel):
    success: bool
    strategy_name: str
    fill_mode: str
    initial_cash: float
    final_equity: float
    net_profit: float
    total_fees: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    # Extended statistics — computed from the trade log. See
    # app/engine/results/statistics.py for the full list of keys.
    statistics: dict[str, Any] = Field(default_factory=dict)
    # Full LEAN-parity statistics (portfolio + trade + runtime).
    lean_statistics: LeanStatisticsResponse | None = None
    trades: list[EngineTradeResponse] = []
    log_lines: list[str] = []
    equity_curve: list[dict] = Field(default_factory=list)
    # Bars iterated over the scored window — the count a summary consumer
    # needs without the curve: on a ``summary_only`` run the curve is empty
    # and this is the count it would have had (#1941). Equals
    # ``len(equity_curve)`` on a full run.
    bars_consumed: int = Field(
        0,
        description=(
            "Bars iterated over the scored window. Equals len(equity_curve) on a "
            "full run; on a summary_only run the curve is empty and this field "
            "carries the count it would have had."
        ),
    )
    # Consolidated OHLCV bars for the price chart (15-min or daily depending
    # on the strategy's consolidator). Much smaller than the full minute-bar
    # stream retained in BacktestResult.bars.
    chart_bars: list[dict] = Field(default_factory=list)
    # Phase 1: Insight tracking — per-prediction scoring and aggregate analytics.
    insights: list[dict] = Field(default_factory=list)
    insight_summary: dict[str, Any] = Field(default_factory=dict)
    # Persisted run id, populated synchronously before returning so the
    # Engine Lab can immediately enable the Replay tab without a second
    # round-trip. Null when persistence failed (the run still succeeded —
    # persistence is best-effort).
    study_id: int | None = None
    error: str | None = None
    # PR B (2026-05-19) — echo of the post-normalization DataPolicy so the
    # frontend can render the policy that was actually used by the engine
    # (which may differ from the request when the legacy synthesizer kicked
    # in). Never null on a successful run.
    data_policy: _EngineDataPolicyModel | None = None
    run_verdict: RunVerdict | None = None
    validation_analytics: EngineValidationAnalyticsResponse | None = None
    # The lake state this run materialized against. For exactly what the hash
    # covers, see ``app.data_lake.run_materialization.materialize_engine_run``.
    # Null when the data lake is off, or when the run materialized nothing:
    # a run that read the pre-lake policy cache has no lake bytes to name.
    lake_data_availability_hash: str | None = None
    # Absent only on a failed run: the window the figures above describe.
    evaluation_window: EngineEvaluationWindowResponse | None = None
