"""Pre-flight checks for backtest runs.

Given a proposed strategy configuration + date range, evaluates a series of
checks that cover the most common reasons a backtest result will diverge
from what a TradingView user would see (every item maps to a known gotcha
in ``docs/tv-polygon-validation-gotchas.md``).

Each check returns one of:

  * ``ok``       — configuration matches TradingView convention.
  * ``warning``  — non-blocking; user can override but should know.
  * ``blocking`` — running this backtest would produce results that
                   should not be trusted.

The checks layer is pure and side-effect-free: callers can render the
result however they like (UI gauge, CLI output, automated CI gate, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

CheckStatus = Literal["ok", "warning", "blocking"]
SessionFilter = Literal["rth_only", "full_session", "unspecified"]


@dataclass(frozen=True)
class IndicatorRequest:
    """Indicator the strategy will compute. ``length`` is the period."""

    name: str  # "ema", "sma", "rsi", "macd", "bb", "adx", "atr", "supertrend"
    length: int  # 5, 10, 14, 20, 26, 50, 200, etc.
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightRequest:
    strategy_name: str
    symbol: str
    start_date: date
    end_date: date
    timeframe: str  # "5m" | "15m" | "1h"
    indicators: list[IndicatorRequest]
    session_filter: SessionFilter = "unspecified"
    warmup_days: int = 0
    dividend_adjustment: bool = False  # whether prices are dividend-adjusted


@dataclass(frozen=True)
class CheckResult:
    id: str
    label: str
    status: CheckStatus
    message: str
    fix_hint: str | None = None
    docs_link: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    overall: CheckStatus
    checks: list[CheckResult]
    summary: str


# Canonical defaults — match TradingView Pine v6 ``ta.*`` and the gotchas doc.
_CANONICAL_DEFAULTS: dict[str, list[int]] = {
    "ema": [5, 10, 20, 30, 40, 50, 100, 200],
    "sma": [20, 50, 200],
    "rsi": [14],
    "macd": [12, 26, 9],
    "bb": [20],
    "adx": [14],
    "atr": [14],
    "supertrend": [10],
}

# Approximate bars-needed-for-99%-convergence per indicator length, expressed
# as a multiplier. For an EMA(N), the seed weight decays as ((N-1)/(N+1))^k;
# k ≈ 4*N to drop below 1%. Wilder smoothing is similar.
_CONVERGENCE_BAR_MULTIPLIER: dict[str, float] = {
    "ema": 4.0,
    "sma": 1.0,
    "rsi": 4.0,
    "macd": 4.0,
    "bb": 1.0,
    "adx": 4.0,
    "atr": 4.0,
    "supertrend": 4.0,
}

# Bars per RTH trading day at each timeframe.
_BARS_PER_DAY: dict[str, int] = {"5m": 78, "15m": 26, "1h": 7}

_GOTCHAS_DOC = "docs/tv-polygon-validation-gotchas.md"


def _check_session_filter(req: PreflightRequest) -> CheckResult:
    if req.session_filter == "rth_only":
        return CheckResult(
            id="session_filter",
            label="Session filter",
            status="ok",
            message="RTH-only — matches TradingView default chart configuration.",
        )
    if req.session_filter == "full_session":
        return CheckResult(
            id="session_filter",
            label="Session filter",
            status="blocking",
            message=(
                "Strategy will consume pre-market and after-hours bars when "
                "computing indicators. TradingView's default chart shows "
                "regular trading hours only — your indicator values will "
                "diverge from any TV chart and your trade list will not match."
            ),
            fix_hint=(
                "Set session_filter to 'rth_only', or add an _is_rth() guard "
                "at the top of your bar handler that returns early on bars "
                "outside 09:30–16:00 ET."
            ),
            docs_link=f"{_GOTCHAS_DOC}#2-extended-hours-bars-flood-the-indicators",
        )
    # unspecified
    return CheckResult(
        id="session_filter",
        label="Session filter",
        status="warning",
        message=(
            "Session filter not declared. Default behavior is 'full_session' "
            "(includes pre-market + after-hours), which diverges from TradingView."
        ),
        fix_hint="Explicitly declare session_filter='rth_only' in your strategy config.",
        docs_link=f"{_GOTCHAS_DOC}#2-extended-hours-bars-flood-the-indicators",
    )


def _check_warmup(req: PreflightRequest) -> CheckResult:
    bars_per_day = _BARS_PER_DAY.get(req.timeframe, 26)

    needed_bars = 0
    longest_indicator: tuple[str, int] | None = None
    for ind in req.indicators:
        mult = _CONVERGENCE_BAR_MULTIPLIER.get(ind.name, 4.0)
        bars = int(ind.length * mult)
        if bars > needed_bars:
            needed_bars = bars
            longest_indicator = (ind.name, ind.length)

    if longest_indicator is None:
        return CheckResult(
            id="warmup",
            label="Indicator warmup buffer",
            status="ok",
            message="No indicators declared, so no warmup buffer required.",
        )

    needed_days = max(1, needed_bars // bars_per_day)
    req.warmup_days * bars_per_day
    name, length = longest_indicator

    if req.warmup_days >= needed_days:
        return CheckResult(
            id="warmup",
            label="Indicator warmup buffer",
            status="ok",
            message=(
                f"{req.warmup_days}-day buffer covers the {length}-period "
                f"{name.upper()}'s {needed_days}-day convergence window."
            ),
        )
    if req.warmup_days >= needed_days // 2:
        return CheckResult(
            id="warmup",
            label="Indicator warmup buffer",
            status="warning",
            message=(
                f"{req.warmup_days}-day buffer is short for {length}-period "
                f"{name.upper()} (needs ~{needed_days} RTH days for full "
                f"convergence). Indicator values in the first weeks of the "
                f"trade window will partially reflect the SMA seed."
            ),
            fix_hint=f"Increase warmup_days to >= {needed_days}.",
            docs_link=f"{_GOTCHAS_DOC}#3-ema-warmup-seeding-and-convergence",
        )
    return CheckResult(
        id="warmup",
        label="Indicator warmup buffer",
        status="blocking",
        message=(
            f"Warmup buffer of {req.warmup_days} days is far too short for "
            f"{length}-period {name.upper()}. The indicator needs ~{needed_days} "
            f"RTH days to converge, but the strategy will start trading "
            f"immediately on the first bar — using deeply unconverged values."
        ),
        fix_hint=f"Increase warmup_days to at least {needed_days}.",
        docs_link=f"{_GOTCHAS_DOC}#3-ema-warmup-seeding-and-convergence",
    )


def _check_indicator_canonicality(req: PreflightRequest) -> CheckResult:
    nonstandard: list[str] = []
    for ind in req.indicators:
        defaults = _CANONICAL_DEFAULTS.get(ind.name)
        if defaults is None:
            nonstandard.append(f"{ind.name}({ind.length}) — unknown indicator")
            continue
        if ind.length not in defaults and ind.name not in ("macd",):
            nonstandard.append(f"{ind.name}({ind.length})")
    if not nonstandard:
        return CheckResult(
            id="indicator_params",
            label="Indicator parameters",
            status="ok",
            message="All indicators use canonical TradingView default parameters.",
        )
    return CheckResult(
        id="indicator_params",
        label="Indicator parameters",
        status="warning",
        message=(
            f"{len(nonstandard)} non-canonical indicator parameter(s): "
            + ", ".join(nonstandard)
            + ". Custom periods are valid, but no "
            "TradingView reference values exist for them — alignment cannot "
            "be checked against the published Pine `ta.*` defaults."
        ),
        fix_hint=(
            "If you need TV-equivalent validation, change to a canonical "
            "period (EMA: 5/10/20/30/40/50/100/200; SMA: 20/50/200; RSI: 14; "
            "BB: 20; ADX/ATR: 14; SuperTrend: 10)."
        ),
    )


def _check_polygon_data_available(
    req: PreflightRequest,
    cache_root: Path = Path("cache/divergence"),
) -> CheckResult:
    """Confirm we have Polygon-source bars in the cache."""
    # The Day-2 ingest writes to ``{tf}/polygon/…``; the Day-3 CLI writes to
    # ``{tf}/merged.parquet``. Either satisfies the "we have data" bar.
    tf_dir = cache_root / req.timeframe
    candidates = [
        tf_dir / "merged.parquet",
        tf_dir / "polygon" / f"{req.symbol.lower()}_{req.timeframe}.parquet",
    ]
    for path in candidates:
        if path.exists():
            return CheckResult(
                id="polygon_data",
                label="Polygon source data availability",
                status="ok",
                message=f"Polygon {req.timeframe} cache populated ({path.name}).",
            )
    return CheckResult(
        id="polygon_data",
        label="Polygon source data availability",
        status="warning",
        message=(
            f"No cached Polygon {req.timeframe} parquet for {req.symbol} under "
            f"{tf_dir}. Backtest will fall back to live Polygon fetches; "
            f"populate the cache for faster, reproducible runs."
        ),
        fix_hint=(
            f"Run `python -m app.research.divergence.cli all "
            f"--pg <polygon_1min_csv> --tf {req.timeframe}` to populate the cache."
        ),
    )


def _check_tv_reference_available(
    req: PreflightRequest,
    cache_root: Path = Path("cache/divergence"),
) -> CheckResult:
    """Check whether a TV CSV has been ingested for this timeframe."""
    tf_dir = cache_root / req.timeframe
    candidates = [
        tf_dir / "tv" / f"{req.symbol.lower()}_{req.timeframe}.parquet",
        tf_dir / "tv" / f"spy_{req.timeframe}.parquet",  # current naming convention
        tf_dir / "merged.parquet",  # merged implies TV was present at ingest time
    ]
    for path in candidates:
        if path.exists():
            return CheckResult(
                id="tv_reference",
                label="TradingView reference data",
                status="ok",
                message=(
                    f"TradingView reference present ({path.name}); alignment "
                    "can be validated against the current cache."
                ),
            )
    return CheckResult(
        id="tv_reference",
        label="TradingView reference data",
        status="warning",
        message=(
            f"No TradingView reference data for {req.symbol} {req.timeframe}. "
            "Backtest will run, but trade-level alignment with TradingView "
            "cannot be validated automatically."
        ),
        fix_hint=(
            "Export a TV CSV using docs/learn-ai_tv_indicator_dump_v6.pine and "
            "run the ingest CLI to populate the reference cache."
        ),
    )


def _check_dividend_handling(req: PreflightRequest) -> CheckResult:
    """For backtests, unadjusted prices are correct; only chart comparisons need adjustment."""
    if req.dividend_adjustment:
        return CheckResult(
            id="dividend_adjustment",
            label="Dividend adjustment",
            status="warning",
            message=(
                "Strategy is using dividend-adjusted prices. For backtesting, "
                "this is unusual: a real trader pays the unadjusted price at "
                "execution time. Adjusted prices may produce P&L numbers that "
                "don't match what an executing strategy would actually achieve."
            ),
            fix_hint=(
                "Use unadjusted prices for backtests. Apply dividend adjustment "
                "only when comparing against a TradingView chart."
            ),
            docs_link=f"{_GOTCHAS_DOC}#1-tradingview-adjusts-for-dividends-polygon-doesnt",
        )
    return CheckResult(
        id="dividend_adjustment",
        label="Dividend adjustment",
        status="ok",
        message="Using unadjusted prices — what a real trader would actually pay.",
    )


def run_preflight(
    req: PreflightRequest,
    cache_root: Path | str = Path("cache/divergence"),
) -> PreflightResult:
    """Run all checks against ``req`` and return a single ``PreflightResult``."""
    cache_root = Path(cache_root)
    checks: list[CheckResult] = [
        _check_session_filter(req),
        _check_warmup(req),
        _check_indicator_canonicality(req),
        _check_polygon_data_available(req, cache_root),
        _check_tv_reference_available(req, cache_root),
        _check_dividend_handling(req),
    ]

    # Compute overall status: blocking > warning > ok.
    has_blocking = any(c.status == "blocking" for c in checks)
    has_warning = any(c.status == "warning" for c in checks)
    overall: CheckStatus = "blocking" if has_blocking else ("warning" if has_warning else "ok")

    n_block = sum(1 for c in checks if c.status == "blocking")
    n_warn = sum(1 for c in checks if c.status == "warning")
    n_ok = sum(1 for c in checks if c.status == "ok")
    summary = (
        f"{n_block} blocking issue{'s' if n_block != 1 else ''}, "
        f"{n_warn} warning{'s' if n_warn != 1 else ''}, "
        f"{n_ok} check{'s' if n_ok != 1 else ''} passed."
    )

    return PreflightResult(overall=overall, checks=checks, summary=summary)
