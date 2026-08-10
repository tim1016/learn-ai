"""LEAN-native entries for the Strategy Lab analytical metric catalog.

This module is deliberately content-only.  ``analytical_metric_catalog`` owns
the Pydantic contract and deterministic export; it validates these dictionaries
when the aggregate catalog is assembled.  Keeping the exhaustive LEAN inventory
here makes the oracle key set, source pin, and documentation evidence auditable
without turning the catalog builder into a large conditional registry.

The entries describe existing LEAN-compatible calculations.  They do not
compute a metric, transform a result value, or provide a second numerical
authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

LEAN_SOURCE_COMMIT: Final = "261366a7e26ae942df858ab20df4fef8fa07de67"
LEAN_SOURCE_REFERENCE: Final = (
    "QuantConnect/Lean Common/Statistics/StatisticsBuilder.cs, "
    "PortfolioStatistics.cs, TradeStatistics.cs, and Statistics.cs "
    f"@ {LEAN_SOURCE_COMMIT}"
)
LEAN_ORACLE_FIXTURE: Final = "PythonDataService/tests/fixtures/golden/lean-statistics-oracle-v1/"
LEAN_ORACLE_TEST: Final = (
    "PythonDataService/tests/test_lean_statistics.py::"
    "test_every_native_portfolio_and_trade_metric_matches_oracle"
)
LEAN_SENTINEL_TEST: Final = (
    "PythonDataService/tests/research/documentation/test_analytical_metric_catalog_entries.py::"
    "test_lean_trade_sentinels_match_the_canonical_reproducer"
)
LEAN_ORACLE_TOLERANCE: Final = "abs <= 0.0000500001, rtol = 0 (native four-decimal oracle)"

LEAN_PORTFOLIO_ORACLE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "alpha",
        "annualStandardDeviation",
        "annualVariance",
        "averageLossRate",
        "averageWinRate",
        "beta",
        "compoundingAnnualReturn",
        "drawdown",
        "drawdownRecovery",
        "endEquity",
        "expectancy",
        "informationRatio",
        "lossRate",
        "portfolioTurnover",
        "probabilisticSharpeRatio",
        "profitLossRatio",
        "sharpeRatio",
        "sortinoRatio",
        "startEquity",
        "totalNetProfit",
        "trackingError",
        "treynorRatio",
        "valueAtRisk95",
        "valueAtRisk99",
        "winRate",
    }
)

LEAN_TRADE_ORACLE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "averageEndTradeDrawdown",
        "averageLosingTradeDuration",
        "averageLoss",
        "averageMAE",
        "averageMFE",
        "averageProfit",
        "averageProfitLoss",
        "averageTradeDuration",
        "averageWinningTradeDuration",
        "endDateTime",
        "largestLoss",
        "largestMAE",
        "largestMFE",
        "largestProfit",
        "lossRate",
        "maxConsecutiveLosingTrades",
        "maxConsecutiveWinningTrades",
        "maximumClosedTradeDrawdown",
        "maximumDrawdownDuration",
        "maximumEndTradeDrawdown",
        "maximumIntraTradeDrawdown",
        "medianLosingTradeDuration",
        "medianTradeDuration",
        "medianWinningTradeDuration",
        "numberOfLosingTrades",
        "numberOfWinningTrades",
        "profitFactor",
        "profitLossDownsideDeviation",
        "profitLossRatio",
        "profitLossStandardDeviation",
        "profitToMaxDrawdownRatio",
        "sharpeRatio",
        "sortinoRatio",
        "startDateTime",
        "totalFees",
        "totalLoss",
        "totalNumberOfTrades",
        "totalProfit",
        "totalProfitLoss",
        "winLossRatio",
        "winRate",
    }
)

LEAN_RUNTIME_SOURCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Equity",
        "Fees",
        "Holdings",
        "Net Profit",
        "Probabilistic Sharpe Ratio",
        "Return",
        "Unrealized",
        "Volume",
        "Total Orders",
    }
)


@dataclass(frozen=True, slots=True)
class _MetricSpec:
    key: str
    metric_id: str
    label: str
    definition: str
    formula_latex: str | None
    variables: tuple[tuple[str, str], ...]
    input_series: str
    units: str
    output_scale: str
    formatting: str
    edge_behavior: str
    aliases: tuple[str, ...] = ()
    annualization: str | None = None
    benchmark: str | None = None
    risk_free_rate: str | None = None
    alternatives: tuple[str, ...] = ()


def _variables(items: tuple[tuple[str, str], ...]) -> tuple[dict[str, str], ...]:
    return tuple({"symbol": symbol, "definition": definition} for symbol, definition in items)


def _snake_case(value: str) -> str:
    acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", acronym_split)
    return re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")


_SEMANTIC_METRIC_IDS: Final[dict[str, str]] = {
    "drawdown": "maximum_drawdown",
    "sortinoRatio": "sortino",
    "profitFactor": "profit_factor",
    "sharpeRatio": "sharpe",
    "winRate": "win_rate",
}


def _semantic_metric_id(spec: _MetricSpec) -> str:
    # AnalyticalMetricCatalog's own validator requires a declared alternative
    # pair to share metric_id, so a spec that names its platform alternative
    # (e.g. portfolio Sortino -> sortino.platform.v1) must keep the shared
    # semantic id. Every other portable LEAN key keeps its own unique,
    # section-qualified spec.metric_id (e.g. "lean_trade_sortino_ratio")
    # instead of collapsing to the same id as its portfolio/trade
    # counterpart or the tracer-owned sharpe.lean_native.v1 -- two
    # lean_native entries must not collide on (metric_id, producer).
    if spec.alternatives:
        return _SEMANTIC_METRIC_IDS.get(spec.key, _snake_case(spec.key))
    return spec.metric_id


def _native_category(spec: _MetricSpec, section: Literal["portfolio", "trade"]) -> str:
    """Return the Python-authored navigation taxonomy for a native entry."""

    text = f"{spec.key} {spec.label} {spec.definition}".lower()
    if "benchmark" in text or spec.key in {"alpha", "beta", "informationRatio", "trackingError", "treynorRatio"}:
        return "benchmark"
    if "drawdown" in text:
        return "drawdown"
    if "duration" in text:
        return "duration"
    if any(token in text for token in ("mae", "mfe", "excursion")):
        return "excursion"
    if any(token in text for token in ("sharpe", "sortino", "variance", "deviation")):
        return "statistical_confidence"
    if section == "trade" and any(token in text for token in ("number", "count", "win rate", "loss rate")):
        return "trade_population"
    if section == "trade":
        return "trade_economics"
    if "risk" in text or "valueatrisk" in text:
        return "risk"
    return "returns"


def _value_states(*, sentinel: str | None = None) -> tuple[dict[str, str], ...]:
    states = [
        {
            "state": "zero",
            "display": "0 (a valid native numeric result)",
            "scoring_behavior": "Preserve zero; never relabel it as unavailable.",
        },
        {
            "state": "unavailable",
            "display": "Native value unavailable because the retained result primitive or receipt is absent.",
            "scoring_behavior": "Do not substitute zero or infer a formula from the display label.",
        },
    ]
    if sentinel is not None:
        states.append(
            {
                "state": "sentinel",
                "display": sentinel,
                "scoring_behavior": "Preserve the finite engine sentinel; it is neither infinity, zero, nor unavailable.",
            }
        )
        states.append(
            {
                "state": "infinite",
                "display": "∞ is not emitted for this denominator edge; LEAN emits its documented finite sentinel instead.",
                "scoring_behavior": "Do not convert the sentinel to infinity.",
            }
        )
    return tuple(states)


# Single mapping for both the sentinel prose and its validating-test
# attachment below, so a fourth sentinel metric added to only one of the two
# places is impossible -- it would produce an entry with a sentinel state and
# no validating test, silently unverified.
_TRADE_SENTINELS: Final[dict[str, str]] = {
    "profitFactor": "10 when total profit is positive and there is no loss denominator.",
    "winLossRatio": "10 when there are winning trades and no losing-trade count denominator.",
    "profitToMaxDrawdownRatio": (
        "10 when total P&L is positive and maximum closed-trade drawdown has no denominator."
    ),
}


def _native_entry(spec: _MetricSpec, *, section: Literal["portfolio", "trade"]) -> dict[str, object]:
    if section == "portfolio":
        canonical_symbol = "PythonDataService/app/engine/results/lean_statistics.py::reproduce_lean_total_performance"
        source_files = "PortfolioStatistics.cs, Statistics.cs, StatisticsBuilder.cs"
        cadence = (
            "Daily LEAN equity/performance samples after StatisticsBuilder preprocessing: "
            "discard day 0 and day 1 return samples; align the corresponding benchmark changes."
        )
        cost_treatment = (
            "Use retained LEAN native primitives as emitted. Do not subtract fees a second time from equity or returns."
        )
    else:
        canonical_symbol = "PythonDataService/app/engine/results/lean_statistics.py::_reproduce_lean_trade_statistics"
        source_files = "TradeStatistics.cs, Statistics.cs"
        cadence = "One ordered observation per closed trade from LEAN totalPerformance.closedTrades."
        cost_treatment = "Trade P&L and totalFees retain the account-currency values emitted in each closed-trade primitive."

    return {
        "metric_id": _semantic_metric_id(spec),
        "variant_id": f"lean_native.{section}.{_snake_case(spec.key)}.v1",
        "contract_id": "lean-native-statistics-oracle-v1",
        "producer": "lean_native",
        "category": _native_category(spec, section),
        "label": spec.label,
        "definition": spec.definition,
        "interpretation": "Read this as a LEAN-native statistic. It is not interchangeable with a similarly named platform metric unless you deliberately compare their contracts.",
        "common_misreadings": (spec.edge_behavior,),
        "aliases": spec.aliases,
        "source_keys": (spec.key,),
        "search_terms": (spec.key, spec.label, section, "LEAN", "native statistics", "StatisticsBuilder"),
        "formula_latex": spec.formula_latex,
        "variables": _variables(spec.variables),
        "input_series": spec.input_series,
        "units": spec.units,
        "output_scale": spec.output_scale,
        "formatting": spec.formatting,
        "sampling_cadence": cadence,
        "annualization": spec.annualization,
        "benchmark": spec.benchmark,
        "risk_free_rate": spec.risk_free_rate,
        "cost_treatment": cost_treatment,
        "timezone_convention": "Source timestamps are int64 ms UTC; timestamp display uses the viewer's local timezone.",
        "value_states": _value_states(sentinel=_TRADE_SENTINELS.get(spec.key)),
        "canonical_symbol": canonical_symbol,
        "source_reference": f"{LEAN_SOURCE_REFERENCE}; {source_files}",
        "validating_tests": (
            LEAN_ORACLE_TEST,
            *((LEAN_SENTINEL_TEST,) if spec.key in _TRADE_SENTINELS else ()),
        ),
        "fixture_or_receipt": LEAN_ORACLE_FIXTURE,
        "numerical_tolerance": LEAN_ORACLE_TOLERANCE,
        "results_surfaces": ("strategy_lab.results.lean_native_statistics",),
        "verdict_membership": None,
        "alternative_variant_ids": spec.alternatives,
    }


_PORTFOLIO_SPECS: Final[tuple[_MetricSpec, ...]] = (
    _MetricSpec("alpha", "lean_portfolio_alpha", "Alpha", "CAPM-style annual excess return over the aligned benchmark.", r"\alpha=R_a-[r_f+\beta(R_b-r_f)]", (("R_a", "annual strategy performance"), ("R_b", "annual benchmark performance"), ("r_f", "average dated LEAN primary-credit rate"), ("\\beta", "aligned daily-return beta")), "Preprocessed daily strategy and benchmark return vectors plus dated LEAN interest-rate observations.", "fractional annual return", "ratio", "3 decimal places", "Returns zero when beta is zero in the pinned implementation.", annualization="Daily observations, 252 trading days per year.", benchmark="Aligned LEAN benchmark return series.", risk_free_rate="Average dated LEAN primary-credit rate selected for equity sample dates."),
    _MetricSpec("annualStandardDeviation", "lean_portfolio_annual_standard_deviation", "Annual Standard Deviation", "Annualized sample standard deviation of preprocessed daily strategy returns.", r"\sigma_{ann}=\sqrt{\operatorname{Var}_{sample}(r)\,N}", (("r", "preprocessed daily strategy returns"), ("N", "trading days per year")), "Preprocessed daily Strategy Equity/Return samples.", "fractional annual return", "ratio", "3 decimal places", "Returns zero for insufficient or constant return observations.", annualization="N = tradingDaysPerYear (252 unless the run config says otherwise)."),
    _MetricSpec("annualVariance", "lean_portfolio_annual_variance", "Annual Variance", "Annualized sample variance of preprocessed daily strategy returns.", r"\operatorname{Var}_{ann}=\operatorname{Var}_{sample}(r)\,N", (("r", "preprocessed daily strategy returns"), ("N", "trading days per year")), "Preprocessed daily Strategy Equity/Return samples.", "squared fractional annual return", "ratio", "3 decimal places", "Returns zero for insufficient or constant return observations.", annualization="N = tradingDaysPerYear (252 unless the run config says otherwise)."),
    _MetricSpec("averageLossRate", "lean_portfolio_average_loss_rate", "Average Loss Rate", "Mean capital-normalized P&L for non-positive closed trades.", r"\overline{\ell}=\operatorname{mean}_{p_i\le0}(p_i/C_i)", (("p_i", "closed-trade P&L"), ("C_i", "running capital before trade i")), "Closed-trade profit/loss series and running capital.", "fractional return", "ratio", "percentage with 2 decimal places", "Returns zero when no loss-rate observations are present."),
    _MetricSpec("averageWinRate", "lean_portfolio_average_win_rate", "Average Win Rate", "Mean capital-normalized P&L for profitable closed trades.", r"\overline{w}=\operatorname{mean}_{p_i>0}(p_i/C_i)", (("p_i", "closed-trade P&L"), ("C_i", "running capital before trade i")), "Closed-trade profit/loss series and running capital.", "fractional return", "ratio", "percentage with 2 decimal places", "Returns zero when no profitable closed-trade observations are present."),
    _MetricSpec("beta", "lean_portfolio_beta", "Beta", "Sample covariance of strategy and benchmark returns divided by benchmark sample variance.", r"\beta=\operatorname{Cov}(r,b)/\operatorname{Var}(b)", (("r", "preprocessed daily strategy returns"), ("b", "aligned daily benchmark returns")), "Aligned daily strategy and benchmark return vectors.", "unitless ratio", "ratio", "3 decimal places", "Returns zero when benchmark variance is zero.", benchmark="Aligned LEAN benchmark return series."),
    _MetricSpec("compoundingAnnualReturn", "lean_portfolio_compounding_annual_return", "Compounding Annual Return", "Calendar-year compound growth from start to end equity.", r"\operatorname{CAGR}=(E_T/E_0)^{1/y}-1", (("E_T", "end equity"), ("E_0", "start equity"), ("y", "calendar duration divided by 365")), "First and final Strategy Equity values and their timestamps.", "fractional annual return", "ratio", "percentage with 3 decimal places", "Uses calendar duration / 365, not trading duration / 252.", annualization="Calendar years = elapsed ms / 86,400,000 / 365."),
    _MetricSpec("drawdown", "lean_portfolio_drawdown", "Drawdown", "Largest Strategy Equity peak-to-trough decline.", r"\max_t(1-E_t/\max_{u\le t}E_u)", (("E_t", "Strategy Equity at time t"),), "Full Strategy Equity chart series.", "fractional decline", "ratio", "percentage with preserved scale", "LEAN retains scale-3 rounding for this native value.", alternatives=("maximum_drawdown.platform.v1",)),
    _MetricSpec("drawdownRecovery", "lean_portfolio_drawdown_recovery", "Drawdown Recovery", "Longest calendar interval from an equity peak to recovery at a new peak.", r"\max( t_{recovery}-t_{peak} )", (("t_{peak}", "prior equity peak timestamp"), ("t_{recovery}", "timestamp reaching a new peak")), "Full Strategy Equity chart timestamps and values.", "calendar days", "integer", "whole days", "Returns zero when no completed peak-to-recovery interval is observed."),
    _MetricSpec("endEquity", "lean_portfolio_end_equity", "End Equity", "Final retained Strategy Equity value.", r"E_T", (("E_T", "final Strategy Equity close"),), "Final full Strategy Equity chart value.", "account currency", "currency", "2 decimal places", "A sparse native chart is not interpolated to manufacture an end value."),
    _MetricSpec("expectancy", "lean_portfolio_expectancy", "LEAN Expectancy", "LEAN portfolio win/loss expectation from native rates.", r"\operatorname{Expectancy}=w\cdot PLR-l", (("w", "win rate"), ("PLR", "average win-rate / absolute average loss-rate"), ("l", "loss rate")), "Native portfolio win rate, loss rate, and profit/loss ratio.", "unitless expected-rate measure", "ratio", "3 decimal places", "This is not the platform arithmetic mean trade-return definition."),
    _MetricSpec("informationRatio", "lean_portfolio_information_ratio", "Information Ratio", "Annual active return divided by annualized tracking error.", r"IR=(R_a-R_b)/TE", (("R_a", "annual strategy performance"), ("R_b", "annual benchmark performance"), ("TE", "annualized tracking error")), "Aligned preprocessed daily strategy and benchmark returns.", "unitless ratio", "ratio", "3 decimal places", "Returns zero when tracking error is zero.", annualization="Daily observations, 252 trading days per year.", benchmark="Aligned LEAN benchmark return series."),
    _MetricSpec("lossRate", "lean_portfolio_loss_rate", "Loss Rate", "Share of all closed trades classified as losses by LEAN.", r"l=n_{loss}/n_{trades}", (("n_{loss}", "number of losing closed trades"), ("n_{trades}", "all closed trades")), "Closed-trade P&L classifications.", "fraction", "ratio", "percentage with 0 decimal places", "Returns zero when no closed trades are supplied."),
    _MetricSpec("portfolioTurnover", "lean_portfolio_turnover", "Portfolio Turnover", "Arithmetic mean of the native Portfolio Turnover series.", r"\overline{TO}=\operatorname{mean}(TO_t)", (("TO_t", "native turnover observation"),), "Native Portfolio Turnover chart series.", "fraction", "ratio", "percentage with 2 decimal places", "Returns zero when the retained turnover series is absent or all values are zero."),
    _MetricSpec("probabilisticSharpeRatio", "lean_portfolio_probabilistic_sharpe_ratio", "Probabilistic Sharpe Ratio", "Normal-CDF probability from observed Sharpe, skewness, excess kurtosis, and sample size.", r"\Phi\!\left((SR-SR^*)/\sqrt{\widehat{\operatorname{Var}}(SR)}\right)", (("SR", "observed daily-return Sharpe"), ("SR^*", "LEAN benchmark Sharpe, 1/\\sqrt{N}"), ("\\Phi", "standard normal CDF")), "Preprocessed daily strategy returns.", "probability", "0 to 1", "percentage with 3 decimal places", "Returns zero when the estimator variance is not positive.", annualization="Benchmark uses N = tradingDaysPerYear.", risk_free_rate="PSR benchmark is 1/sqrt(N), not the platform PSR convention."),
    _MetricSpec("profitLossRatio", "lean_portfolio_profit_loss_ratio", "Portfolio Profit/Loss Ratio", "Average winning rate divided by the absolute average losing rate.", r"PLR=\overline{w}/|\overline{\ell}|", (("\\overline{w}", "average win rate"), ("\\overline{\\ell}", "average loss rate")), "Capital-normalized closed-trade win/loss rates.", "unitless ratio", "ratio", "2 decimal places", "Returns zero when the average loss-rate denominator is zero."),
    _MetricSpec("sortinoRatio", "lean_portfolio_sortino_ratio", "Sortino Ratio", "Annual excess return over the dated LEAN primary-credit rate divided by annualized downside deviation.", r"\operatorname{Sortino}=(R_{ann}-r_f)/\sigma_{down,ann}", (("R_{ann}", "annual strategy performance"), ("r_f", "average dated LEAN primary-credit rate"), ("\\sigma_{down,ann}", "annualized sample deviation of negative returns")), "Preprocessed negative daily strategy returns and dated LEAN interest-rate observations.", "unitless ratio", "ratio", "3 decimal places", "Returns zero when annualized downside deviation is zero; this is a valid native zero, not platform unavailability.", annualization="Daily observations, 252 trading days per year.", risk_free_rate="Average dated LEAN primary-credit rate selected for equity sample dates.", alternatives=("sortino.platform.v1",)),
    _MetricSpec("startEquity", "lean_portfolio_start_equity", "Start Equity", "Starting-cash parameter used by the native statistics calculation.", r"E_0", (("E_0", "configured starting capital"),), "Algorithm configuration starting_cash (falling back to the first equity value only when unavailable).", "account currency", "currency", "2 decimal places", "Uses the retained run configuration; no current account balance is inferred."),
    _MetricSpec("totalNetProfit", "lean_portfolio_total_net_profit", "Total Net Profit", "Fractional growth from start equity to end equity.", r"E_T/E_0-1", (("E_T", "end equity"), ("E_0", "start equity")), "Start and final Strategy Equity values.", "fractional total return", "ratio", "percentage with 3 decimal places", "Returns zero when start equity is zero in the pinned implementation."),
    _MetricSpec("trackingError", "lean_portfolio_tracking_error", "Tracking Error", "Annualized sample deviation of active daily returns.", r"TE=\sqrt{\operatorname{Var}_{sample}(r-b)\,N}", (("r", "preprocessed strategy daily returns"), ("b", "aligned benchmark daily returns"), ("N", "trading days per year")), "Aligned preprocessed daily strategy and benchmark return vectors.", "fractional annual return", "ratio", "3 decimal places", "Returns zero for insufficient or identical active-return observations.", annualization="Daily observations, 252 trading days per year.", benchmark="Aligned LEAN benchmark return series."),
    _MetricSpec("treynorRatio", "lean_portfolio_treynor_ratio", "Treynor Ratio", "Annual excess return per unit of beta.", r"(R_{ann}-r_f)/\beta", (("R_{ann}", "annual strategy performance"), ("r_f", "average dated LEAN primary-credit rate"), ("\\beta", "aligned daily-return beta")), "Annual strategy performance, dated LEAN rate, and beta.", "unitless ratio", "ratio", "3 decimal places", "Returns zero when beta is zero.", annualization="Daily observations, 252 trading days per year.", benchmark="Aligned LEAN benchmark return series.", risk_free_rate="Average dated LEAN primary-credit rate selected for equity sample dates."),
    _MetricSpec("valueAtRisk95", "lean_portfolio_value_at_risk_95", "Value at Risk 95%", "Normal-distribution 5% quantile of the trailing daily-return window.", r"\operatorname{VaR}_{95}=Q_{\mathcal N(\mu,\sigma)}(0.05)", (("\\mu", "mean trailing daily return"), ("\\sigma", "sample daily-return standard deviation")), "Last N preprocessed daily strategy returns.", "fractional daily return", "ratio", "percentage with 3 decimal places", "Normal-model quantile rounded by LEAN to three decimals.", annualization="Window N = tradingDaysPerYear; result itself is a daily-return quantile."),
    _MetricSpec("valueAtRisk99", "lean_portfolio_value_at_risk_99", "Value at Risk 99%", "Normal-distribution 1% quantile of the trailing daily-return window.", r"\operatorname{VaR}_{99}=Q_{\mathcal N(\mu,\sigma)}(0.01)", (("\\mu", "mean trailing daily return"), ("\\sigma", "sample daily-return standard deviation")), "Last N preprocessed daily strategy returns.", "fractional daily return", "ratio", "percentage with 3 decimal places", "Normal-model quantile rounded by LEAN to three decimals.", annualization="Window N = tradingDaysPerYear; result itself is a daily-return quantile."),
    _MetricSpec("winRate", "lean_portfolio_win_rate", "Portfolio Win Rate", "Share of all closed trades classified as wins by LEAN.", r"w=n_{win}/n_{trades}", (("n_{win}", "number of winning closed trades"), ("n_{trades}", "all closed trades")), "Closed-trade P&L classifications.", "fraction", "ratio", "percentage with 0 decimal places", "Returns zero when no closed trades are supplied."),
)


_TRADE_SPECS: Final[tuple[_MetricSpec, ...]] = (
    _MetricSpec("averageEndTradeDrawdown", "lean_trade_average_end_trade_drawdown", "Average End-Trade Drawdown", "Native average drawdown measure recorded at trade ends.", r"\operatorname{mean}(DD_{end,i})", (("DD_{end,i}", "native end-trade drawdown for closed trade i"),), "Ordered closed trades, their P&L, and maximum favorable excursion primitives.", "account currency", "currency", "2 decimal places", "Preserves LEAN's end-trade drawdown convention; it is not the portfolio equity drawdown."),
    _MetricSpec("averageLosingTradeDuration", "lean_trade_average_losing_duration", "Average Losing Trade Duration", "LEAN online TimeSpan average of entry-to-exit durations for losing trades.", r"\operatorname{Avg}_{online}(t_{exit}-t_{entry})", (("t_{entry}", "trade entry timestamp"), ("t_{exit}", "trade exit timestamp")), "Closed trades with non-positive P&L and their duration primitives.", "duration", "TimeSpan", "native TimeSpan", "Returns a zero duration when no losing trades are supplied; online tick truncation is preserved."),
    _MetricSpec("averageLoss", "lean_trade_average_loss", "Average Loss", "Mean account-currency P&L of losing closed trades.", r"\operatorname{mean}_{p_i\le0}(p_i)", (("p_i", "closed-trade account-currency P&L"),), "Closed-trade P&L values classified as losses.", "account currency", "currency", "2 decimal places", "Returns zero when no loss observations are present."),
    _MetricSpec("averageMAE", "lean_trade_average_mae", "Average MAE", "Mean maximum adverse excursion across closed trades.", r"\operatorname{mean}(MAE_i)", (("MAE_i", "maximum adverse excursion of trade i"),), "Closed-trade MAE primitives.", "account currency", "currency", "2 decimal places", "Uses account-currency excursion values; it is not a percentage loss."),
    _MetricSpec("averageMFE", "lean_trade_average_mfe", "Average MFE", "Mean maximum favorable excursion across closed trades.", r"\operatorname{mean}(MFE_i)", (("MFE_i", "maximum favorable excursion of trade i"),), "Closed-trade MFE primitives.", "account currency", "currency", "2 decimal places", "Uses account-currency excursion values; it is not a realized-profit total."),
    _MetricSpec("averageProfit", "lean_trade_average_profit", "Average Profit", "Mean account-currency P&L of winning closed trades.", r"\operatorname{mean}_{p_i>0}(p_i)", (("p_i", "closed-trade account-currency P&L"),), "Closed-trade P&L values classified as wins.", "account currency", "currency", "2 decimal places", "Returns zero when no winning trades are present."),
    _MetricSpec("averageProfitLoss", "lean_trade_average_profit_loss", "Average P&L", "Arithmetic mean account-currency P&L across all closed trades.", r"\sum_i p_i/n", (("p_i", "closed-trade account-currency P&L"), ("n", "number of closed trades")), "All closed-trade P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when no closed trades are supplied."),
    _MetricSpec("averageTradeDuration", "lean_trade_average_duration", "Average Trade Duration", "LEAN online TimeSpan average of all entry-to-exit trade durations.", r"\operatorname{Avg}_{online}(t_{exit}-t_{entry})", (("t_{entry}", "trade entry timestamp"), ("t_{exit}", "trade exit timestamp")), "All closed-trade duration primitives.", "duration", "TimeSpan", "native TimeSpan", "Online double conversion and tick truncation are part of the pinned result; a simple arithmetic mean can differ by microseconds."),
    _MetricSpec("averageWinningTradeDuration", "lean_trade_average_winning_duration", "Average Winning Trade Duration", "LEAN online TimeSpan average of entry-to-exit durations for winning trades.", r"\operatorname{Avg}_{online}(t_{exit}-t_{entry})", (("t_{entry}", "trade entry timestamp"), ("t_{exit}", "trade exit timestamp")), "Closed trades with positive P&L and their duration primitives.", "duration", "TimeSpan", "native TimeSpan", "Returns a zero duration when no winning trades are supplied; online tick truncation is preserved."),
    _MetricSpec("endDateTime", "lean_trade_end_datetime", "Last Exit", "Latest exit timestamp among the supplied closed trades.", r"\max_i t_{exit,i}", (("t_{exit,i}", "closed-trade exit timestamp"),), "Closed-trade exit timestamps.", "int64 ms UTC", "timestamp", "viewer-local date and time", "Absent when no closed trade supplies an exit timestamp."),
    _MetricSpec("largestLoss", "lean_trade_largest_loss", "Largest Loss", "Most negative account-currency P&L among losing trades.", r"\min_{p_i\le0}p_i", (("p_i", "closed-trade account-currency P&L"),), "Closed-trade loss P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when no loss is present."),
    _MetricSpec("largestMAE", "lean_trade_largest_mae", "Largest MAE", "Most adverse maximum adverse excursion across closed trades.", r"\min_i MAE_i", (("MAE_i", "maximum adverse excursion of trade i"),), "Closed-trade MAE primitives.", "account currency", "currency", "2 decimal places", "Returns zero when no closed trades are supplied."),
    _MetricSpec("largestMFE", "lean_trade_largest_mfe", "Largest MFE", "Largest maximum favorable excursion across closed trades.", r"\max_i MFE_i", (("MFE_i", "maximum favorable excursion of trade i"),), "Closed-trade MFE primitives.", "account currency", "currency", "2 decimal places", "Returns zero when no closed trades are supplied."),
    _MetricSpec("largestProfit", "lean_trade_largest_profit", "Largest Profit", "Largest account-currency P&L among winning trades.", r"\max_{p_i>0}p_i", (("p_i", "closed-trade account-currency P&L"),), "Closed-trade winning P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when no win is present."),
    _MetricSpec("lossRate", "lean_trade_loss_rate", "Trade Loss Rate", "Share of closed trades classified as losing by LEAN.", r"n_{loss}/n", (("n_{loss}", "number of losing trades"), ("n", "all closed trades")), "Closed-trade P&L classifications.", "fraction", "ratio", "percentage with 2 decimal places", "Returns zero when no closed trades are supplied."),
    _MetricSpec("maxConsecutiveLosingTrades", "lean_trade_max_consecutive_losses", "Max Consecutive Losses", "Longest consecutive sequence of losing closed trades.", r"\max\operatorname{run}(p_i\le0)", (("p_i", "ordered closed-trade P&L"),), "Closed trades in LEAN's supplied order.", "trade count", "integer", "whole number", "Returns zero when no losing run occurs."),
    _MetricSpec("maxConsecutiveWinningTrades", "lean_trade_max_consecutive_wins", "Max Consecutive Wins", "Longest consecutive sequence of winning closed trades.", r"\max\operatorname{run}(p_i>0)", (("p_i", "ordered closed-trade P&L"),), "Closed trades in LEAN's supplied order.", "trade count", "integer", "whole number", "Returns zero when no winning run occurs."),
    _MetricSpec("maximumClosedTradeDrawdown", "lean_trade_maximum_closed_drawdown", "Maximum Closed-Trade Drawdown", "Largest drawdown in cumulative P&L measured only at closed-trade boundaries.", r"\min_i(CPL_i-\max_{j\le i}CPL_j)", (("CPL_i", "cumulative closed-trade P&L"),), "Ordered closed-trade P&L sequence.", "account currency", "currency", "2 decimal places", "This excludes intra-trade movement and is distinct from portfolio drawdown."),
    _MetricSpec("maximumDrawdownDuration", "lean_trade_maximum_drawdown_duration", "Maximum Drawdown Duration", "Longest closed-trade equity drawdown interval in LEAN's ordered trade sequence.", r"\max(t_{recovery}-t_{peak})", (("t_{peak}", "closed-trade cumulative-P&L peak"), ("t_{recovery}", "subsequent recovery timestamp")), "Ordered closed-trade P&L and entry/exit timestamps.", "duration", "TimeSpan", "native TimeSpan", "Returns zero duration when no completed closed-trade drawdown recovery occurs."),
    _MetricSpec("maximumEndTradeDrawdown", "lean_trade_maximum_end_drawdown", "Maximum End-Trade Drawdown", "Maximum native drawdown recorded at a trade end.", r"\max_i DD_{end,i}", (("DD_{end,i}", "native end-trade drawdown"),), "Closed-trade endTradeDrawdown primitives.", "account currency", "currency", "2 decimal places", "This is a native trade-end drawdown field, not a portfolio equity drawdown."),
    _MetricSpec("maximumIntraTradeDrawdown", "lean_trade_maximum_intra_drawdown", "Maximum Intra-Trade Drawdown", "Largest drawdown including trade-level adverse movement before close.", r"\min_i(CPL_{i-1}+MAE_i-\max(CPL+MFE))", (("MAE_i", "trade adverse excursion"), ("MFE_i", "trade favorable excursion"), ("CPL", "cumulative P&L")), "Ordered closed trades with MAE, MFE, and P&L primitives.", "account currency", "currency", "2 decimal places", "It includes intra-trade excursions and therefore differs from closed-trade drawdown."),
    _MetricSpec("medianLosingTradeDuration", "lean_trade_median_losing_duration", "Median Losing Trade Duration", "Median entry-to-exit duration among losing trades.", r"\operatorname{median}_{p_i\le0}(t_{exit}-t_{entry})", (("t_{entry}", "trade entry timestamp"), ("t_{exit}", "trade exit timestamp")), "Durations of closed trades classified as losses.", "duration", "TimeSpan", "native TimeSpan", "Returns zero duration when no losing-trade durations exist."),
    _MetricSpec("medianTradeDuration", "lean_trade_median_duration", "Median Trade Duration", "Median entry-to-exit duration among all closed trades.", r"\operatorname{median}_i(t_{exit,i}-t_{entry,i})", (("t_{entry,i}", "trade entry timestamp"), ("t_{exit,i}", "trade exit timestamp")), "Durations of all closed trades.", "duration", "TimeSpan", "native TimeSpan", "Returns zero duration when no trade durations exist."),
    _MetricSpec("medianWinningTradeDuration", "lean_trade_median_winning_duration", "Median Winning Trade Duration", "Median entry-to-exit duration among winning trades.", r"\operatorname{median}_{p_i>0}(t_{exit}-t_{entry})", (("t_{entry}", "trade entry timestamp"), ("t_{exit}", "trade exit timestamp")), "Durations of closed trades classified as wins.", "duration", "TimeSpan", "native TimeSpan", "Returns zero duration when no winning-trade durations exist."),
    _MetricSpec("numberOfLosingTrades", "lean_trade_number_losing", "Losing Trades", "Count of closed trades classified as losses.", r"n_{loss}", (("n_{loss}", "count of losing closed trades"),), "Closed-trade P&L classifications.", "trade count", "integer", "whole number", "Zero is a valid count, not unavailable."),
    _MetricSpec("numberOfWinningTrades", "lean_trade_number_winning", "Winning Trades", "Count of closed trades classified as wins.", r"n_{win}", (("n_{win}", "count of winning closed trades"),), "Closed-trade P&L classifications.", "trade count", "integer", "whole number", "Zero is a valid count, not unavailable."),
    _MetricSpec("profitFactor", "profit_factor", "LEAN Trade Profit Factor", "Positive account-currency trade P&L divided by the absolute negative account-currency trade P&L.", r"\sum_{p_i>0}p_i/|\sum_{p_i\le0}p_i|", (("p_i", "closed-trade account-currency P&L"),), "All closed-trade account-currency P&L values.", "unitless ratio", "ratio", "4 decimal places", "When positive profit exists without a loss denominator, pinned LEAN emits the finite sentinel 10 — not infinity, zero, or unavailable.", alternatives=("profit_factor.platform.v1",)),
    _MetricSpec("profitLossDownsideDeviation", "lean_trade_profit_loss_downside_deviation", "P&L Downside Deviation", "Sample standard deviation of losing closed-trade account-currency P&L.", r"\sqrt{\operatorname{Var}_{sample}(\{p_i:p_i\le0\})}", (("p_i", "closed-trade account-currency P&L"),), "Loss-classified closed-trade P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when fewer than two loss observations are supplied."),
    _MetricSpec("profitLossRatio", "lean_trade_profit_loss_ratio", "Trade Profit/Loss Ratio", "Average winning trade P&L divided by the absolute average losing trade P&L.", r"\overline{p}_{win}/|\overline{p}_{loss}|", (("\\overline{p}_{win}", "average winning trade P&L"), ("\\overline{p}_{loss}", "average losing trade P&L")), "Winning and losing closed-trade account-currency P&L values.", "unitless ratio", "ratio", "4 decimal places", "Returns zero when a required win/loss denominator is absent."),
    _MetricSpec("profitLossStandardDeviation", "lean_trade_profit_loss_standard_deviation", "P&L Standard Deviation", "Sample standard deviation of all closed-trade account-currency P&L.", r"\sqrt{\operatorname{Var}_{sample}(p)}", (("p", "all closed-trade account-currency P&L values"),), "All closed-trade P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when fewer than two P&L observations are supplied."),
    _MetricSpec("profitToMaxDrawdownRatio", "lean_trade_profit_to_max_drawdown_ratio", "Profit to Maximum Drawdown Ratio", "Total closed-trade P&L divided by absolute maximum closed-trade drawdown.", r"\sum_i p_i/|DD_{closed,max}|", (("p_i", "closed-trade account-currency P&L"), ("DD_{closed,max}", "maximum closed-trade drawdown")), "Ordered closed-trade P&L and closed-trade drawdown.", "unitless ratio", "ratio", "4 decimal places", "Returns LEAN's finite 10 sentinel for positive P&L without a closed-drawdown denominator."),
    _MetricSpec("sharpeRatio", "lean_trade_sharpe_ratio", "Trade Sharpe Ratio", "Mean closed-trade P&L divided by its sample standard deviation.", r"\overline p/\sigma_{sample}(p)", (("\\overline p", "mean closed-trade P&L"), ("\\sigma_{sample}", "sample P&L standard deviation")), "All closed-trade account-currency P&L values.", "unitless ratio", "ratio", "4 decimal places", "Not annualized; returns zero when P&L standard deviation is zero."),
    _MetricSpec("sortinoRatio", "lean_trade_sortino_ratio", "Trade Sortino Ratio", "Mean closed-trade P&L divided by losing-P&L downside deviation.", r"\overline p/\sigma_{downside}(p_{loss})", (("\\overline p", "mean closed-trade P&L"), ("\\sigma_{downside}", "sample deviation of losing P&L")), "All closed-trade P&L and losing-P&L subsets.", "unitless ratio", "ratio", "4 decimal places", "Not annualized; returns zero when downside deviation is zero."),
    _MetricSpec("startDateTime", "lean_trade_start_datetime", "First Entry", "Earliest entry timestamp among the supplied closed trades.", r"\min_i t_{entry,i}", (("t_{entry,i}", "closed-trade entry timestamp"),), "Closed-trade entry timestamps.", "int64 ms UTC", "timestamp", "viewer-local date and time", "Absent when no closed trade supplies an entry timestamp."),
    _MetricSpec("totalFees", "lean_trade_total_fees", "Trade Fees", "Sum of LEAN-attributed fees across closed trades.", r"\sum_i fee_i", (("fee_i", "fee attributed to closed trade i"),), "Closed-trade totalFees primitives.", "account currency", "currency", "2 decimal places", "A fee is not subtracted again from a native P&L value that already reflects LEAN's accounting."),
    _MetricSpec("totalLoss", "lean_trade_total_loss", "Total Loss", "Sum of non-positive closed-trade account-currency P&L.", r"\sum_{p_i\le0}p_i", (("p_i", "closed-trade account-currency P&L"),), "Loss-classified closed-trade P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when there are no losing P&L observations."),
    _MetricSpec("totalNumberOfTrades", "lean_trade_total_number", "Closed Trades", "Number of closed trades supplied to LEAN TradeStatistics.", r"n", (("n", "count of supplied closed trades"),), "Closed-trade population supplied by the retained native result.", "trade count", "integer", "whole number", "The full native population can differ from a separately truncated display ledger."),
    _MetricSpec("totalProfit", "lean_trade_total_profit", "Total Profit", "Sum of positive closed-trade account-currency P&L.", r"\sum_{p_i>0}p_i", (("p_i", "closed-trade account-currency P&L"),), "Winning closed-trade P&L values.", "account currency", "currency", "2 decimal places", "Returns zero when there are no winning P&L observations."),
    _MetricSpec("totalProfitLoss", "lean_trade_total_profit_loss", "Total P&L", "Sum of account-currency P&L across all closed trades.", r"\sum_i p_i", (("p_i", "closed-trade account-currency P&L"),), "All closed-trade P&L values.", "account currency", "currency", "2 decimal places", "This is account-currency P&L, not a percentage return."),
    _MetricSpec("winLossRatio", "lean_trade_win_loss_ratio", "Win/Loss Count Ratio", "Winning-trade count divided by losing-trade count.", r"n_{win}/n_{loss}", (("n_{win}", "winning closed-trade count"), ("n_{loss}", "losing closed-trade count")), "Closed-trade P&L classifications.", "unitless ratio", "ratio", "4 decimal places", "Returns LEAN's finite 10 sentinel when wins exist with no losing-count denominator."),
    _MetricSpec("winRate", "lean_trade_win_rate", "Trade Win Rate", "Share of closed trades classified as winning by LEAN.", r"n_{win}/n", (("n_{win}", "winning closed-trade count"), ("n", "all closed-trade count")), "Closed-trade P&L classifications.", "fraction", "ratio", "percentage with 2 decimal places", "Returns zero when no closed trades are supplied."),
)


_RUNTIME_SPECS: Final[tuple[_MetricSpec, ...]] = (
    _MetricSpec("Equity", "lean_runtime_equity", "Runtime Equity", "Value reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Equity] string parsed without recomputation.", "account currency", "currency", "2 decimal places", "Reported value; no local formula contract.", aliases=("runtime equity",)),
    _MetricSpec("Fees", "lean_runtime_fees", "Runtime Fees", "Fee value reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Fees] string parsed without recomputation.", "account currency", "currency", "2 decimal places", "Reported value; retain the emitted sign exactly; no local formula contract.", aliases=("runtime fees",)),
    _MetricSpec("Holdings", "lean_runtime_holdings", "Runtime Holdings", "Holdings value reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Holdings] string parsed without recomputation.", "account currency", "currency", "2 decimal places", "Reported value; no local formula contract.", aliases=("runtime holdings",)),
    _MetricSpec("Net Profit", "lean_runtime_net_profit", "Runtime Net Profit", "Net profit reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Net Profit] string parsed without recomputation.", "account currency", "currency", "2 decimal places", "Reported value; no local formula contract.", aliases=("runtime net profit",)),
    _MetricSpec("Probabilistic Sharpe Ratio", "lean_runtime_probabilistic_sharpe_ratio", "Runtime PSR", "Probabilistic Sharpe Ratio reported by LEAN Result.RuntimeStatistics.", None, (), "Reported Result.RuntimeStatistics[Probabilistic Sharpe Ratio] percentage string parsed without recomputation.", "probability", "0 to 1", "percentage with 2 decimal places", "Reported value; no local formula contract. It is separate from the 66-value parity inventory.", aliases=("runtime psr", "PSR")),
    _MetricSpec("Return", "lean_runtime_total_return", "Runtime Total Return", "Total return reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Return] percentage string parsed without recomputation.", "fractional total return", "ratio", "percentage with 2 decimal places", "Reported value; no local formula contract.", aliases=("runtime return", "total return")),
    _MetricSpec("Unrealized", "lean_runtime_unrealized", "Runtime Unrealized P&L", "Unrealized P&L reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Unrealized] string parsed without recomputation.", "account currency", "currency", "2 decimal places", "Reported value; no local formula contract.", aliases=("unrealized pnl", "runtime unrealized")),
    _MetricSpec("Volume", "lean_runtime_volume", "Runtime Volume", "Volume reported by LEAN Result.RuntimeStatistics at completion.", None, (), "Reported Result.RuntimeStatistics[Volume] string parsed without recomputation.", "account currency as emitted by LEAN runtime", "currency", "2 decimal places", "Reported value; no local formula contract and no invented unit conversion.", aliases=("runtime volume",)),
    _MetricSpec("Total Orders", "lean_runtime_total_orders", "Runtime Orders", "Order count carried with the native runtime projection.", None, (), "Retained normalized order count; the display projection does not recalculate it from trades.", "order count", "integer", "whole number", "Reported count; no local formula contract.", aliases=("orders", "runtime orders")),
)


def _runtime_entry(spec: _MetricSpec) -> dict[str, object]:
    is_normalized_order_count = spec.key == "Total Orders"
    return {
        "metric_id": _semantic_metric_id(spec),
        "variant_id": f"lean_runtime.{_snake_case(spec.key)}.v1",
        "contract_id": "lean-native-statistics-oracle-v1",
        "producer": "lean_runtime",
        "category": "runtime_snapshot",
        "label": spec.label,
        "definition": spec.definition,
        "interpretation": "This is a native runtime-reported value. The catalog documents its source and display contract without claiming a local formula.",
        "common_misreadings": (spec.edge_behavior,),
        "aliases": spec.aliases,
        "source_keys": (spec.key,),
        "search_terms": (
            spec.key,
            spec.label,
            "runtime",
            "LEAN",
            "normalized order count" if is_normalized_order_count else "Result.RuntimeStatistics",
        ),
        "formula_latex": None,
        "variables": (),
        "input_series": spec.input_series,
        "units": spec.units,
        "output_scale": spec.output_scale,
        "formatting": spec.formatting,
        "sampling_cadence": (
            "One retained normalized order count at run completion."
            if is_normalized_order_count
            else "One retained LEAN Result.RuntimeStatistics snapshot at run completion."
        ),
        "annualization": None,
        "benchmark": None,
        "risk_free_rate": None,
        "cost_treatment": "Preserve the emitted runtime value; do not derive, reconcile, or apply an extra fee adjustment in the catalog consumer.",
        "timezone_convention": "Runtime has no timestamp value. Any related run timestamp remains int64 ms UTC and displays in the viewer's local timezone.",
        "value_states": _value_states(),
        "canonical_symbol": "app.services.lean_sidecar_persistence._normalized_to_lean_statistics_response",
        "source_reference": (
            f"Learn-AI normalized LEAN result total_orders field, derived from the pinned LEAN source result "
            f"order collection @ {LEAN_SOURCE_COMMIT}; this displayed count is separate from the runtime-statistics map."
            if is_normalized_order_count
            else f"{LEAN_SOURCE_REFERENCE}; Result.RuntimeStatistics native output"
        ),
        "validating_tests": (
            "PythonDataService/tests/test_lean_statistics.py::test_formatted_lean_dashboard_strings_match_oracle",
        ),
        "fixture_or_receipt": LEAN_ORACLE_FIXTURE,
        "numerical_tolerance": None,
        "results_surfaces": ("strategy_lab.results.lean_native_runtime_snapshot",),
        "verdict_membership": None,
        "alternative_variant_ids": (),
    }


# The trust-spine tracer owns the shared ``sharpe.lean_native.v1`` identifier.
# Keep its source key in the exact oracle set while leaving its one authored
# variant out of this shard, so integration has one entry rather than a
# collision.  All other 24 portfolio keys are supplied here.
TRACER_OWNED_PORTFOLIO_KEYS: Final[frozenset[str]] = frozenset({"sharpeRatio"})

LEAN_PORTFOLIO_VARIANTS: Final[tuple[dict[str, object], ...]] = tuple(
    _native_entry(spec, section="portfolio") for spec in _PORTFOLIO_SPECS
)
LEAN_TRADE_VARIANTS: Final[tuple[dict[str, object], ...]] = tuple(
    _native_entry(spec, section="trade") for spec in _TRADE_SPECS
)
LEAN_RUNTIME_VARIANTS: Final[tuple[dict[str, object], ...]] = tuple(_runtime_entry(spec) for spec in _RUNTIME_SPECS)
LEAN_NATIVE_CATALOG_VARIANTS: Final[tuple[dict[str, object], ...]] = (
    *LEAN_PORTFOLIO_VARIANTS,
    *LEAN_TRADE_VARIANTS,
    *LEAN_RUNTIME_VARIANTS,
)
