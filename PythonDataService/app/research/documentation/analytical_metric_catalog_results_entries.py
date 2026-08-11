"""Results-domain entries for the Strategy Lab analytical metric catalog.

These immutable entries are documentation metadata, not a second calculation
path. Numerical ownership remains with the canonical Python producers named on
each entry; Angular only renders the generated catalog.
"""

from __future__ import annotations

from typing import Final

from app.services.run_verdict_service import VERDICT_POLICY_DOCUMENTATION

from .analytical_metric_catalog import MetricCategory, MetricVariant, ValueState

_PLATFORM_UNAVAILABLE = ValueState(
    state="unavailable",
    display="Unavailable",
    scoring_behavior="Not a zero. A required verdict input remains incomplete until the platform producer records it.",
)
_VALID_ZERO = ValueState(
    state="zero",
    display="0",
    scoring_behavior="A computed zero remains a value; it is not missing evidence.",
)
_UNDEFINED = ValueState(
    state="undefined",
    display="Unavailable",
    scoring_behavior="The producer did not have a mathematical result for the recorded inputs; do not substitute zero.",
)
_INFINITE = ValueState(
    state="infinite",
    display="∞",
    scoring_behavior="Preserve the limiting result when the producer can transport it; it is not a zero.",
)

_PLATFORM_TRADER_INTERPRETATION: Final[dict[str, str]] = {
    "net_pnl": "Use it as the run's absolute dollar outcome, then scale it by starting capital and compare it with drawdown and time in market.",
    "initial_cash": "Use it as the capital base for interpreting dollar profit, percentage return, position size, and risk—not as a performance result.",
    "final_equity": "Use it as the ending capital checkpoint; the path taken to reach it still matters for drawdown and risk.",
    "total_fees": "Use it to measure recorded execution friction and compare it with gross profit and turnover before judging whether the edge survives costs.",
    "completed_trades": "Use it as a first check on sample depth before relying on averages, ratios, streaks, or statistical confidence.",
    "winning_trades": "Use it with losing trades and payoff size to understand how the strategy's realized outcomes are distributed.",
    "losing_trades": "Use it with winning trades, loss size, and losing streaks to understand the strategy's realized downside pattern.",
    "profit_factor": "Use it to compare aggregate winning returns with aggregate losing returns; values above one indicate gross wins exceeded gross losses in this sample.",
    "expectancy": "Use it to estimate what one completed trade contributed on average, while checking whether a few outliers dominate the mean.",
    "payoff_ratio": "Use it with win rate to see whether the strategy relies on frequent small wins or less frequent larger wins.",
    "win_rate": "Use it to understand how often trades won, but judge profitability only together with payoff ratio, expectancy, fees, and sample size.",
    "sortino": "Use it to compare return with observed downside variability; read high values cautiously when negative observations are scarce.",
    "maximum_drawdown": "Use it as the deepest recorded peak-to-trough capital decline and pair it with recovery time and the equity path.",
    "cagr": "Use it to compare compound growth rates across runs with compatible duration and capital conventions, not as a forecast.",
    "calmar": "Use it to compare compound growth with the worst recorded drawdown, while inspecting both inputs separately.",
    "annual_volatility": "Use it to understand the annualized dispersion of the selected return series; it measures variability, not direction or tail loss.",
    "recovery_duration": "Use it to estimate how long capital remained below a prior peak and whether the strategy's drawdowns are operationally tolerable.",
    "max_consecutive_losers": "Use it to stress-test sizing, capital reserves, and trader discipline against the longest losing sequence observed.",
    "fee_drag": "Use it to see how much recorded friction consumed gross profit and whether the strategy depends on unrealistically cheap execution.",
    "probabilistic_sharpe": "Use it as model-based evidence about whether the estimated Sharpe clears its benchmark under this return contract, not as a profit forecast.",
    "sample_size": "Use it as an evidence-volume check; more trades help estimation but do not guarantee independent observations or regime coverage.",
    "skepticism_penalty": "Use it as an audit flag that the product policy found unusually strong headline metrics that deserve extra validation.",
    "trade_portfolio_sharpe_gap": "Use it to spot disagreement between trade-level and portfolio-path views of risk-adjusted return and investigate the input curves.",
    "full_run_totals": "Use these as the authoritative whole-run figures even when the interactive trade table shows only a recent slice.",
    "recent_trade_ledger": "Use this bounded slice for inspection and navigation, not to manually reconstruct whole-run totals or statistics.",
    "realized_equity": "Use this curve to follow capital changes booked at completed exits; compare it with mark-to-market evidence for open-position risk.",
    "risk_statistic_input_curve": "Use this curve to understand what path the risk statistic actually measured when it differs from the realized-equity display.",
}

_PLATFORM_TRADER_CAUTION: Final[dict[str, str]] = {
    "net_pnl": "Dollar profit is not comparable across differently funded runs without normalization.",
    "initial_cash": "A larger capital base can make dollar results look larger without improving return quality.",
    "final_equity": "The same endpoint can conceal very different drawdowns and open-position paths.",
    "total_fees": "Only costs represented in the execution model are included; missing spread or slippage assumptions remain outside this value.",
    "completed_trades": "A large count is not automatically a large independent sample when trades share signals or market regimes.",
    "winning_trades": "A high count of wins can still lose money when losses are much larger.",
    "losing_trades": "A low loss count can still hide severe tail losses.",
    "profit_factor": "A very high or infinite result can come from a short sample with few or no losses.",
    "expectancy": "The arithmetic mean can be dominated by a small number of unusually large trades.",
    "payoff_ratio": "A strong payoff ratio does not compensate for an insufficient win rate or excessive costs by itself.",
    "win_rate": "Win rate alone says nothing about the relative size of wins and losses.",
    "sortino": "Few downside observations can make the ratio unstable or unavailable.",
    "maximum_drawdown": "Historical maximum drawdown is an observed outcome, not a bound on future loss.",
    "cagr": "Short windows can turn modest endpoint changes into extreme annualized rates.",
    "calmar": "The ratio can look strong because of an unusually short window or a drawdown that has not yet been stressed.",
    "annual_volatility": "Standard deviation can understate asymmetric jumps and tail losses.",
    "recovery_duration": "An unrecovered drawdown may be censored by the run end rather than fully represented by a completed recovery interval.",
    "max_consecutive_losers": "The next losing streak can exceed the historical maximum.",
    "fee_drag": "A low percentage is only meaningful if the fee, spread, and slippage model is realistic.",
    "probabilistic_sharpe": "The probability is conditional on the estimator and sample assumptions; it is not the chance the next trade wins.",
    "sample_size": "Observation count does not measure market-regime diversity or independence.",
    "skepticism_penalty": "This policy adjustment is an investigation prompt, not proof of overfitting.",
    "trade_portfolio_sharpe_gap": "A gap identifies different measurement contracts; it does not by itself identify which one is wrong.",
    "full_run_totals": "Do not replace the persisted totals with arithmetic over a truncated UI table.",
    "recent_trade_ledger": "The visible rows can overrepresent the latest regime and omit earlier behavior.",
    "realized_equity": "Closed-trade booking can hide adverse movement in positions that remain open.",
    "risk_statistic_input_curve": "Do not assume this curve and the primary chart are point-for-point identical.",
}


# metric_ids whose numeric value is actually asserted by
# strategy-metric-help-golden-v1.json (see tests/fixtures/test_strategy_metric_help_golden.py
# for the exact PortfolioStatistics/TradeStatistics field each key checks).
# Every other platform metric_id -- including Backtest Evidence Grade
# sub-scores and raw result statistics like total_fees or cagr -- is not
# covered by that fixture and must not cite it as a receipt it doesn't have.
_STRATEGY_METRIC_HELP_GOLDEN_METRIC_IDS: Final[frozenset[str]] = frozenset(
    {
        "net_pnl",
        "profit_factor",
        "expectancy",
        "sortino",
        "maximum_drawdown",
        "win_rate",
        "completed_trades",
    }
)


def _evidence_for(metric_id: str, canonical_symbol: str) -> tuple[tuple[str, ...], str | None]:
    """Return the real receipt for each published Results producer."""

    if canonical_symbol == "PythonDataService/app/engine/results/equity_downsample.py::build_realized_equity_envelope":
        return (
            ("PythonDataService/tests/engine/results/test_equity_downsample.py::test_realized_equity_matches_golden_fixture",),
            "PythonDataService/tests/fixtures/golden/engine-results/ENG-006/v1/",
        )
    if canonical_symbol == "PythonDataService/app/services/engine_validation_analytics.py::build_compatibility_equity_curve":
        return (
            ("PythonDataService/tests/services/test_engine_validation_analytics.py::test_compatibility_equity_curve_compounds_common_trade_returns_and_pins_endpoints",),
            None,
        )
    if canonical_symbol.startswith("Backend/"):
        return (
            ("Backend.Tests/Unit/GraphQL/BacktestRunDetailQueryTests.cs",),
            None,
        )
    if "run_verdict_service.py" in canonical_symbol:
        return (
            ("PythonDataService/tests/services/test_run_verdict_parity.py",),
            "PythonDataService/tests/fixtures/golden/run-verdict-v2/fixture.json",
        )
    fixture_or_receipt = (
        "contracts/fixtures/strategy-metric-help-golden-v1.json"
        if metric_id in _STRATEGY_METRIC_HELP_GOLDEN_METRIC_IDS
        else None
    )
    return (("PythonDataService/tests/test_statistics.py",), fixture_or_receipt)


def _platform_metric(
    metric_id: str,
    label: str,
    definition: str,
    *,
    category: MetricCategory,
    input_series: str,
    units: str,
    formatting: str,
    canonical_symbol: str,
    verdict_membership: str | None = None,
    value_states: tuple[ValueState, ...] = (_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    alternative_variant_ids: tuple[str, ...] = (),
) -> MetricVariant:
    validating_tests, fixture_or_receipt = _evidence_for(metric_id, canonical_symbol)
    return MetricVariant(
        metric_id=metric_id,
        variant_id=f"{metric_id}.platform.v1",
        contract_id="platform-results-statistics-v1",
        producer="platform",
        category=category,
        label=label,
        definition=definition,
        interpretation=_PLATFORM_TRADER_INTERPRETATION[metric_id],
        common_misreadings=(
            _PLATFORM_TRADER_CAUTION[metric_id],
            "A familiar label is not interchangeable with a different producer's calculation.",
        ),
        aliases=(label,),
        search_terms=(metric_id.replace("_", " "), "platform", "results"),
        input_series=input_series,
        units=units,
        output_scale="producer-native value",
        formatting=formatting,
        sampling_cadence="Recorded run evidence; see the canonical producer for its observation contract.",
        cost_treatment="Uses persisted platform run evidence; no value is recomputed in the documentation client.",
        timezone_convention="All persisted timestamps are int64 ms UTC; ordinary timestamps render in the viewer's local timezone unless this entry says otherwise.",
        value_states=value_states,
        canonical_symbol=canonical_symbol,
        source_reference=None,
        validating_tests=validating_tests,
        fixture_or_receipt=fixture_or_receipt,
        numerical_tolerance=None,
        results_surfaces=("Strategy Lab Results",),
        verdict_membership=verdict_membership,
        alternative_variant_ids=alternative_variant_ids,
    )


def _verdict_policy(
    key: str,
    label: str,
    thresholds: str,
) -> MetricVariant:
    return MetricVariant(
        metric_id=f"verdict_policy_{key}",
        variant_id=f"verdict_policy.{key}.v2",
        contract_id="readiness-core-v2",
        producer="verdict_policy",
        category="verdict_policy",
        label=label,
        definition=(
            f"Frozen Backtest Evidence Grade v2 policy for {label}. {thresholds} "
            "This policy entry defines a score bucket; it does not redefine the underlying metric."
        ),
        interpretation="Use it to audit the fixed policy receipt, not to infer a cause or permission to trade.",
        common_misreadings=(
            "A policy threshold is not a mathematical definition of the source metric.",
            "An extreme input is an investigation trigger, not a causal diagnosis.",
        ),
        aliases=(key.replace("_", " "), "Backtest Evidence Grade input"),
        search_terms=("17 inputs", "fixed policy", "v2", "evidence grade"),
        # The 17-input policy scores a producer-recorded value; it does not
        # redefine it. Point at the platform variant that owns the definition
        # instead of restating (or, as before this fix, leaking) its raw id.
        input_series=f"The platform's {key.replace('_', ' ')} value, sourced from its {key}.platform.v1 catalog entry.",
        units="0–20 policy points",
        output_scale="fixed sub-score before dimension aggregation",
        formatting="integer points",
        sampling_cadence="Evaluated once from the persisted run's platform evidence.",
        cost_treatment="Uses the canonical platform input as recorded; the policy does not recompute a trading metric.",
        timezone_convention="Not applicable; this is a frozen policy mapping.",
        value_states=(
            ValueState(
                state="unavailable",
                display="Not graded",
                scoring_behavior="The complete 17-input composite is withheld; no available input gains additional weight.",
            ),
        ),
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py",
        source_reference="Product policy: readiness-core-v2.",
        validating_tests=("PythonDataService/tests/services/test_run_verdict_parity.py",),
        fixture_or_receipt="PythonDataService/tests/fixtures/golden/run-verdict-v2/fixture.json",
        numerical_tolerance="exact integer policy buckets",
        results_surfaces=("Strategy Lab Results · Backtest Evidence Grade",),
        verdict_membership="One required input of the fixed 17-input v2 policy.",
    )


def _validation_metric(
    metric_id: str,
    label: str,
    definition: str,
    *,
    input_series: str,
    units: str,
    formatting: str,
    value_states: tuple[ValueState, ...] = (_VALID_ZERO, _PLATFORM_UNAVAILABLE),
) -> MetricVariant:
    trader_interpretation = {
        "trailing_horizon_return": "Compare horizons to see whether performance persists outside one favorable period, but only where the full horizon is covered.",
        "trailing_horizon_coverage": "Use this gate before reading a trailing return; incomplete coverage means the requested horizon was not actually observed.",
        "trailing_horizon_trade_count": "Use it to judge whether a horizon's return and win-rate summaries rest on enough completed trades to inspect.",
        "trailing_horizon_win_rate": "Compare win frequency across recent horizons, then pair it with profit factor and trade count.",
        "trailing_horizon_profit_factor": "Compare recent gross-win efficiency across horizons while checking trade count and no-loss edge states.",
        "timing_cell_trade_count": "Use it to distinguish a repeatable time bucket from a visually strong cell supported by only a few trades.",
        "timing_cell_win_rate": "Use it to investigate whether win frequency changes by entry day and hour, with the cell's trade count beside it.",
        "timing_cell_average_return": "Use it to investigate which entry-time buckets contributed or detracted on average, while checking outliers and count.",
        "calendar_month_observation_count": "Use it to see how many distinct years support a calendar-month pattern before treating seasonality as repeatable.",
        "calendar_month_median_compounded_return": "Use the median to compare the typical observed outcome for a calendar month across years, alongside its observation count.",
        "rolling_trade_average_return": "Use it to see whether average trade quality is stable or drifting across overlapping 20-trade windows.",
        "rolling_trade_win_rate": "Use it to see whether win frequency is stable or drifting across overlapping 20-trade windows.",
    }[metric_id]
    return MetricVariant(
        metric_id=metric_id,
        variant_id=f"{metric_id}.validation_analytics.v1",
        contract_id="engine-validation-analytics-v1",
        producer="validation_analytics",
        category="validation_atlas",
        label=label,
        definition=definition,
        interpretation=trader_interpretation,
        common_misreadings=("This view is descriptive evidence, not an independent confirmation of the backtest.",),
        aliases=(label, "Performance Memory"),
        search_terms=("performance memory", "validation atlas", metric_id.replace("_", " ")),
        input_series=input_series,
        units=units,
        output_scale="producer-native value",
        formatting=formatting,
        sampling_cadence="Computed when the run completes from the recorded equity curve and closed-trade ledger.",
        cost_treatment="Uses closed-trade returns and the run's configured execution costs as already represented in persisted evidence.",
        timezone_convention="Weekday and hour buckets use America/New_York; all other timestamps remain int64 ms UTC until local display.",
        value_states=value_states,
        canonical_symbol="PythonDataService/app/services/engine_validation_analytics.py",
        source_reference="Pardo, The Evaluation and Optimization of Trading Strategies (2e), chapter 4; Bacon, Practical Portfolio Performance Measurement (2e), chapter 2.",
        validating_tests=("PythonDataService/tests/services/test_engine_validation_analytics.py",),
        fixture_or_receipt=None,
        numerical_tolerance="absolute=1e-12, relative=0 in focused analytics tests",
        results_surfaces=("Strategy Lab Results · Performance Memory",),
    )


PLATFORM_HEADLINE_VARIANTS: tuple[MetricVariant, ...] = (
    _platform_metric(
        "net_pnl",
        "Net P&L",
        "Final equity less initial cash after recorded fees.",
        category="returns",
        input_series="Initial cash and final equity from the authoritative full run.",
        units="USD",
        formatting="currency, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics",
    ),
    _platform_metric(
        "initial_cash",
        "Initial cash",
        "Cash recorded at the start of the authoritative run.",
        category="returns",
        input_series="Persisted run configuration.",
        units="USD",
        formatting="currency, two decimal places",
        canonical_symbol="Backend/Models/MarketData/StrategyExecution.cs::InitialCash",
    ),
    _platform_metric(
        "final_equity",
        "Final equity",
        "Equity recorded at the end of the authoritative run.",
        category="returns",
        input_series="Persisted final equity evidence.",
        units="USD",
        formatting="currency, two decimal places",
        canonical_symbol="Backend/Models/MarketData/StrategyExecution.cs::FinalEquity",
    ),
    _platform_metric(
        "total_fees",
        "Total fees",
        "Sum of fees recorded for the authoritative run.",
        category="trade_economics",
        input_series="Persisted execution fee ledger.",
        units="USD",
        formatting="currency, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
    ),
    _platform_metric(
        "completed_trades",
        "Completed trades",
        "Count of completed round trips in the authoritative full-run ledger.",
        category="trade_population",
        input_series="Authoritative full-run ledger of closed trades, not the displayed recent-500 projection.",
        units="trades",
        formatting="integer",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
    ),
    _platform_metric(
        "winning_trades",
        "Winning trades",
        "Completed trades with a return greater than zero.",
        category="trade_population",
        input_series="Authoritative closed-trade return ledger.",
        units="trades",
        formatting="integer",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
    ),
    _platform_metric(
        "losing_trades",
        "Losing trades",
        "Completed trades with a return below zero.",
        category="trade_population",
        input_series="Authoritative closed-trade return ledger.",
        units="trades",
        formatting="integer",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
    ),
    _platform_metric(
        "profit_factor",
        "Profit factor",
        "Gross winning trade returns divided by the absolute gross losing trade returns.",
        category="trade_economics",
        input_series="Completed trade percentage returns.",
        units="ratio",
        formatting="two decimal places or ∞",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
        verdict_membership="Trade Edge required input.",
        value_states=(_VALID_ZERO, _INFINITE, _PLATFORM_UNAVAILABLE),
        alternative_variant_ids=("lean_native.trade.profit_factor.v1",),
    ),
    _platform_metric(
        "expectancy",
        "Expectancy / trade",
        "Arithmetic mean completed-trade percentage return.",
        category="trade_economics",
        input_series="Completed trade percentage returns.",
        units="percentage return",
        formatting="percentage, three decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
        verdict_membership="Trade Edge required input.",
    ),
    _platform_metric(
        "payoff_ratio",
        "Payoff ratio",
        "Average winning return divided by the absolute average losing return.",
        category="trade_economics",
        input_series="Completed trade percentage returns separated by sign.",
        units="ratio",
        formatting="two decimal places or ∞",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
        verdict_membership="Trade Edge required input.",
        value_states=(_VALID_ZERO, _INFINITE, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "win_rate",
        "Win rate",
        "Winning completed trades divided by all completed trades.",
        category="trade_population",
        input_series="Completed trade percentage returns.",
        units="fraction",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_trade_statistics",
        verdict_membership="Trade Edge required input.",
    ),
    _platform_metric(
        "sortino",
        "Sortino ratio",
        "Annualized mean return divided by annualized downside deviation of the selected platform return observations.",
        category="statistical_confidence",
        input_series="Platform return observations selected by the statistics producer.",
        units="ratio",
        formatting="two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::_sortino",
        verdict_membership="Return Quality required input.",
        value_states=(
            _VALID_ZERO,
            ValueState(
                state="unavailable",
                display="Unavailable",
                scoring_behavior="No negative platform return observations makes Sortino unavailable. It is not displayed or scored as zero.",
            ),
        ),
        alternative_variant_ids=("lean_native.portfolio.sortino_ratio.v1",),
    ),
    _platform_metric(
        "maximum_drawdown",
        "Maximum drawdown",
        "Largest fall from a running equity peak to a later trough.",
        category="drawdown",
        input_series="Platform equity curve observations.",
        units="fraction",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::max_drawdown",
        verdict_membership="Risk Control required input.",
        alternative_variant_ids=("lean_native.portfolio.drawdown.v1",),
    ),
    _platform_metric(
        "cagr",
        "CAGR",
        "Platform compound annual growth rate calculated from the producing run's trading-duration convention.",
        category="returns",
        input_series="Platform equity curve or documented compatibility input series.",
        units="fraction",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::compute_portfolio_statistics",
        verdict_membership="Return Quality required input.",
        value_states=(_VALID_ZERO, _UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "calmar",
        "Calmar ratio",
        "Platform CAGR divided by platform maximum drawdown when both inputs are defined and drawdown is positive.",
        category="drawdown",
        input_series="Platform CAGR and maximum drawdown.",
        units="ratio",
        formatting="two decimal places",
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py::_grade_calmar_sub",
        verdict_membership="Return Quality required input.",
        value_states=(_UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "annual_volatility",
        "Annual volatility",
        "Annualized standard deviation provided by the platform statistics producer.",
        category="statistical_confidence",
        input_series="Platform return observations.",
        units="fraction",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py",
        verdict_membership="Return Quality required input.",
    ),
    _platform_metric(
        "recovery_duration",
        "Drawdown recovery",
        "Longest peak-to-recovery calendar duration in whole days on the closed-trade ledger.",
        category="drawdown",
        input_series="Completed trades with entry and exit timestamps.",
        units="days",
        formatting="integer days",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::_drawdown_recovery_days",
        verdict_membership="Risk Control required input.",
        value_states=(_VALID_ZERO, _UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "max_consecutive_losers",
        "Max consecutive losers",
        "Longest contiguous count of completed trades with negative percentage return.",
        category="risk",
        input_series="Completed trade percentage returns in ledger order.",
        units="trades",
        formatting="integer",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::_max_consecutive_losses",
        verdict_membership="Risk Control required input.",
    ),
    _platform_metric(
        "fee_drag",
        "Fee drag on gross",
        "Recorded total fees divided by net profit plus fees when gross profit is positive.",
        category="trade_economics",
        input_series="Persisted net profit and fee total.",
        units="fraction",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py::_grade_fee_drag_sub",
        verdict_membership="Trade Edge required input.",
        value_states=(_UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "probabilistic_sharpe",
        "Probabilistic Sharpe",
        "Platform probability that its unannualized Sharpe exceeds zero under the documented return-vector contract.",
        category="statistical_confidence",
        input_series="The platform return vector, sample standard deviation, skew, and Pearson kurtosis.",
        units="probability",
        formatting="percentage, two decimal places",
        canonical_symbol="PythonDataService/app/engine/results/statistics.py::_probabilistic_sharpe_ratio",
        verdict_membership="Statistical Confidence required input.",
        value_states=(_UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "sample_size",
        "Sample size (trades)",
        "Number of completed trades in the authoritative full-run ledger.",
        category="trade_population",
        input_series="Authoritative full-run closed-trade ledger.",
        units="trades",
        formatting="integer",
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py::_grade_sample_size_sub",
        verdict_membership="Statistical Confidence required input.",
    ),
    _platform_metric(
        "skepticism_penalty",
        "Skepticism penalty",
        "A fixed policy adjustment driven by extreme platform Sharpe, profit factor, or win rate observations.",
        category="trade_population",
        input_series="Platform Sharpe, profit factor, and win rate.",
        units="policy points",
        formatting="integer points",
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py::_grade_skepticism_sub",
        verdict_membership="Statistical Confidence required input.",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
    _platform_metric(
        "trade_portfolio_sharpe_gap",
        "Trade versus portfolio Sharpe gap",
        "Trade Sharpe minus platform portfolio Sharpe when both persisted values are available.",
        category="statistical_confidence",
        input_series="Platform portfolio Sharpe and persisted trade Sharpe.",
        units="ratio difference",
        formatting="two decimal places",
        canonical_symbol="PythonDataService/app/services/run_verdict_service.py::_grade_trade_gap_sub",
        verdict_membership="Statistical Confidence required input.",
        value_states=(_UNDEFINED, _PLATFORM_UNAVAILABLE),
    ),
    _platform_metric(
        "full_run_totals",
        "Authoritative full-run totals",
        "Totals and statistics computed from the complete persisted run, even when the UI projects only recent ledger rows.",
        category="returns",
        input_series="Complete persisted trade and equity evidence.",
        units="context",
        formatting="plain language",
        canonical_symbol="Backend/GraphQL/BacktestRunDetailQuery.cs",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
    _platform_metric(
        "recent_trade_ledger",
        "Recent 500-trade ledger",
        "The most recent up to 500 trades displayed for interaction; it is not the authority for full-run totals or statistics.",
        category="returns",
        input_series="Recent chronological projection of the authoritative closed-trade ledger.",
        units="trades",
        formatting="integer rows",
        canonical_symbol="Backend/GraphQL/BacktestRunDetailQuery.cs",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
    _platform_metric(
        "realized_equity",
        "Realized equity",
        "Equity that books net P&L at completed trade exits for the primary visual narrative.",
        category="returns",
        input_series="Persisted realized-equity curve.",
        units="USD",
        formatting="currency chart",
        canonical_symbol="PythonDataService/app/engine/results/equity_downsample.py::build_realized_equity_envelope",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
    _platform_metric(
        "risk_statistic_input_curve",
        "Risk-statistic input curve",
        "Canonical mark-to-market or compatibility evidence used by the producing risk statistic; it can differ from the realized-equity display curve.",
        category="risk",
        input_series="Persisted mark-to-market or compatibility curve identified by the producing metric contract.",
        units="USD or normalized equity",
        formatting="producer-specific",
        canonical_symbol="PythonDataService/app/services/engine_validation_analytics.py::build_compatibility_equity_curve",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
)


VERDICT_POLICY_VARIANTS: tuple[MetricVariant, ...] = (
    _verdict_policy(
        "sharpe",
        "Sharpe ratio policy input",
        VERDICT_POLICY_DOCUMENTATION["sharpe"],
    ),
    _verdict_policy(
        "sortino",
        "Sortino ratio policy input",
        VERDICT_POLICY_DOCUMENTATION["sortino"],
    ),
    _verdict_policy(
        "cagr",
        "CAGR policy input",
        VERDICT_POLICY_DOCUMENTATION["cagr"],
    ),
    _verdict_policy(
        "calmar",
        "Calmar policy input",
        VERDICT_POLICY_DOCUMENTATION["calmar"],
    ),
    _verdict_policy(
        "annual_volatility",
        "Annual volatility policy input",
        VERDICT_POLICY_DOCUMENTATION["annual_volatility"],
    ),
    _verdict_policy(
        "maximum_drawdown",
        "Maximum drawdown policy input",
        VERDICT_POLICY_DOCUMENTATION["maximum_drawdown"],
    ),
    _verdict_policy(
        "recovery_duration",
        "Drawdown recovery policy input",
        VERDICT_POLICY_DOCUMENTATION["recovery_duration"],
    ),
    _verdict_policy(
        "max_consecutive_losers",
        "Max consecutive losers policy input",
        VERDICT_POLICY_DOCUMENTATION["max_consecutive_losers"],
    ),
    _verdict_policy(
        "profit_factor",
        "Profit factor policy input",
        VERDICT_POLICY_DOCUMENTATION["profit_factor"],
    ),
    _verdict_policy(
        "expectancy",
        "Expectancy policy input",
        VERDICT_POLICY_DOCUMENTATION["expectancy"],
    ),
    _verdict_policy(
        "win_rate",
        "Win rate policy input",
        VERDICT_POLICY_DOCUMENTATION["win_rate"],
    ),
    _verdict_policy(
        "payoff_ratio",
        "Payoff ratio policy input",
        VERDICT_POLICY_DOCUMENTATION["payoff_ratio"],
    ),
    _verdict_policy(
        "fee_drag",
        "Fee drag policy input",
        VERDICT_POLICY_DOCUMENTATION["fee_drag"],
    ),
    _verdict_policy(
        "probabilistic_sharpe",
        "Probabilistic Sharpe policy input",
        VERDICT_POLICY_DOCUMENTATION["probabilistic_sharpe"],
    ),
    _verdict_policy(
        "sample_size",
        "Sample size policy input",
        VERDICT_POLICY_DOCUMENTATION["sample_size"],
    ),
    _verdict_policy(
        "skepticism_penalty",
        "Skepticism policy input",
        VERDICT_POLICY_DOCUMENTATION["skepticism_penalty"],
    ),
    _verdict_policy(
        "trade_portfolio_sharpe_gap",
        "Trade versus portfolio Sharpe-gap policy input",
        VERDICT_POLICY_DOCUMENTATION["trade_portfolio_sharpe_gap"],
    ),
)


PERFORMANCE_MEMORY_VARIANTS: tuple[MetricVariant, ...] = (
    _validation_metric(
        "trailing_horizon_return",
        "Trailing-horizon net return",
        "End equity divided by the equity at the horizon start, minus one. It is absent when the recorded equity curve does not cover the full horizon.",
        input_series="Recorded equity curve between each trailing start and the run end.",
        units="fraction",
        formatting="percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "trailing_horizon_coverage",
        "Trailing-horizon coverage",
        "Whether the recorded equity curve begins on or before the requested trailing-window start.",
        input_series="First recorded equity timestamp and the requested trailing start.",
        units="boolean coverage",
        formatting="Full coverage or Needs more data",
        value_states=(_PLATFORM_UNAVAILABLE,),
    ),
    _validation_metric(
        "trailing_horizon_trade_count",
        "Trailing-horizon trade count",
        "Count of closed trades with exits inside the inclusive trailing window.",
        input_series="Closed-trade exit timestamps and the requested trailing window.",
        units="trades",
        formatting="integer",
    ),
    _validation_metric(
        "trailing_horizon_win_rate",
        "Trailing-horizon win rate",
        "Winning closed trades divided by all closed trades whose exits fall in the trailing window.",
        input_series="Closed-trade returns and exit timestamps inside the trailing window.",
        units="fraction",
        formatting="percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "trailing_horizon_profit_factor",
        "Trailing-horizon profit factor",
        "Gross winning returns divided by absolute gross losing returns for closed trades inside the trailing window.",
        input_series="Closed-trade percentage returns and exit timestamps inside the trailing window.",
        units="ratio",
        formatting="two decimal places or ∞",
        value_states=(_VALID_ZERO, _INFINITE, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "timing_cell_trade_count",
        "Timing-cell trade count",
        "Count of closed trades grouped by entry weekday and entry hour in America/New_York time.",
        input_series="Closed-trade entry timestamps converted locally to America/New_York.",
        units="trades",
        formatting="integer",
    ),
    _validation_metric(
        "timing_cell_win_rate",
        "Timing-cell win rate",
        "Winning closed trades divided by all closed trades in the same ET weekday/hour entry bucket.",
        input_series="Closed-trade returns grouped by America/New_York entry weekday and hour.",
        units="fraction",
        formatting="percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "timing_cell_average_return",
        "Timing-cell average return",
        "Arithmetic mean closed-trade percentage return in one ET weekday/hour entry bucket.",
        input_series="Closed-trade percentage returns grouped by America/New_York entry weekday and hour.",
        units="percentage return",
        formatting="signed percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "calendar_month_observation_count",
        "Calendar-month observation count",
        "Number of distinct year-month compounded-return observations that share a calendar month.",
        input_series="Closed-trade returns grouped by America/New_York exit year and month.",
        units="months",
        formatting="integer",
    ),
    _validation_metric(
        "calendar_month_median_compounded_return",
        "Calendar-month median compounded return",
        "Median of each matching calendar month's compounded closed-trade return across observed years.",
        input_series="Per-year-month products of one plus each closed-trade return, grouped by America/New_York exit month.",
        units="fraction",
        formatting="signed percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "rolling_trade_average_return",
        "Rolling 20-trade average return",
        "Arithmetic mean return of each trailing 20 completed-trade window.",
        input_series="Completed trades in ledger order, using overlapping trailing windows of 20.",
        units="percentage return",
        formatting="signed percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
    _validation_metric(
        "rolling_trade_win_rate",
        "Rolling 20-trade win rate",
        "Winning completed trades divided by 20 for each overlapping trailing 20-trade window.",
        input_series="Completed trades in ledger order, using overlapping trailing windows of 20.",
        units="fraction",
        formatting="percentage, two decimal places",
        value_states=(_VALID_ZERO, _PLATFORM_UNAVAILABLE),
    ),
)


RESULTS_CATALOG_VARIANTS: tuple[dict[str, object], ...] = tuple(
    entry.model_dump(mode="json")
    for entry in (
        *PLATFORM_HEADLINE_VARIANTS,
        *VERDICT_POLICY_VARIANTS,
        *PERFORMANCE_MEMORY_VARIANTS,
    )
)
