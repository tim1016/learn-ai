"""Shared helpers for spec-vs-hand-coded parity tests.

Also exposes ``configure_script_logger`` and a module-level ``logger`` so
the script-mode entry points (``run_all``, ``run_parity``) emit pass/fail
messages through the structured logger rather than ``print`` — keeping
the package compliant with the repo's no-print rule while preserving
human-readable stdout output when invoked directly.

The parity contract is "given the same input bars, ``SpecAlgorithm``
produces the same trades (entry time, entry price, exit time, exit
price, PnL, win/loss, indicator snapshot values) as the hand-coded
reference algorithm." The spec layer doesn't reimplement indicator
math — it just declares which indicator and how to wire it — so a
true bit-for-bit trade-log match against the hand-coded twin is the
appropriate gate.

These helpers build minute-level synthetic bars with predictable 15-
minute consolidator boundaries (the same trick the existing
``test_sma_crossover_parity`` and ``test_rsi_mean_reversion_parity``
scripts use). Each consolidated bar's close equals the corresponding
synthetic close value.
"""

from __future__ import annotations

import logging
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.spec import SpecAlgorithm, load_spec_from_path

EASTERN = ZoneInfo("America/New_York")
SYMBOL = "TEST"
RESOLUTION_MINUTES = 15
START_TIME = datetime(2024, 1, 2, 10, 0, tzinfo=EASTERN)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


# ---------------------------------------------------------------------------
# Script logging — used by run_all() / run_parity() entry points so test
# scripts emit structured logs instead of bare print() calls.
# ---------------------------------------------------------------------------
logger = logging.getLogger("app.engine.strategy.spec.tests")


def configure_script_logger() -> None:
    """Attach a stdout handler at INFO level for ``python -m`` script runs.

    The structured logger replaces the bare ``print()`` calls that
    script-style test runners would otherwise use. Configured once per
    process — re-entry is a no-op so calling this from multiple
    ``run_*`` entry points is safe.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def fixture_path(name: str) -> Path:
    """Resolve the canonical spec fixture path for the given strategy name."""
    return FIXTURES_DIR / f"{name}.spec.json"


# ---------------------------------------------------------------------------
# Synthetic close generators — copied verbatim from the existing parity
# tests so the spec parity gate exercises the same input shape and is
# directly comparable to the hand-coded reference's already-trusted output.
# ---------------------------------------------------------------------------
def closes_for_sma(num_bars: int) -> list[float]:
    """SMA crossover synthetic series — mix of winning and losing trades."""
    closes: list[float] = []
    base = 100.0
    for i in range(num_bars):
        drift = -0.01 * i
        wave_slow = 4.0 * math.sin(i / 11.0)
        wave_fast = 1.5 * math.sin(i / 3.1)
        closes.append(base + drift + wave_slow + wave_fast)
    return closes


def closes_for_rsi(num_bars: int) -> list[float]:
    """RSI mean-reversion series — pushes RSI(14) below 30 and above 70."""
    closes: list[float] = []
    base = 100.0
    for i in range(num_bars):
        drift = -0.025 * i
        wave_slow = 6.0 * math.sin(i / 13.0)
        wave_fast = 2.5 * math.sin(i / 2.7)
        closes.append(base + drift + wave_slow + wave_fast)
    return closes


def closes_for_spy_ema(num_bars: int) -> list[float]:
    """EMA(5)/EMA(10)+RSI(14) synthetic series.

    The SPY entry rule (fresh EMA5 up-cross + gap >= 0.20 + 50 <= RSI <= 70)
    only fires when a cross happens with momentum — at the moment of
    crossover the gap must already exceed 0.20, which requires a
    decisive single-bar move. This generator superimposes three sine
    components at different frequencies on a $400 base to produce
    SPY-like intraday volatility (~1% slow swings + ~0.3% fast wiggles
    + ~0.1% shake), tuned to fire the rule a handful of times across a
    1500-2000 bar window.
    """
    closes: list[float] = []
    base = 400.0  # SPY-ish price level
    for i in range(num_bars):
        drift = 5.0 * i / max(num_bars, 1)  # gentle macro uptrend (~$5)
        slow = 4.0 * math.sin(i / 12.0)
        fast = 1.2 * math.sin(i / 1.6)
        shake = 0.4 * math.sin(i / 0.7)
        closes.append(base + drift + slow + fast + shake)
    return closes


# ---------------------------------------------------------------------------
# Bar builder + fake data reader — same trick as the SMA/RSI parity tests.
# ---------------------------------------------------------------------------
def build_minute_bars(closes: list[float], start: datetime = START_TIME) -> list[TradeBar]:
    bars: list[TradeBar] = []
    for i, c in enumerate(closes):
        t = start + timedelta(minutes=15 * i)
        price = Decimal(str(round(c, 4)))
        bars.append(
            TradeBar(
                symbol=SYMBOL,
                time=t,
                end_time=t + timedelta(minutes=1),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100,
            )
        )
    last_t = start + timedelta(minutes=15 * len(closes))
    sentinel_price = Decimal(str(round(closes[-1], 4)))
    bars.append(
        TradeBar(
            symbol=SYMBOL,
            time=last_t,
            end_time=last_t + timedelta(minutes=1),
            open=sentinel_price,
            high=sentinel_price,
            low=sentinel_price,
            close=sentinel_price,
            volume=100,
        )
    )
    return bars


@dataclass
class FakeDataReader:
    bars: list[TradeBar]

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        # Filter by symbol in addition to date — without this, a strategy
        # subscribed to the wrong ticker would still receive bars and
        # silently mask a symbol-wiring regression.
        target = symbol.upper()
        for b in self.bars:
            if b.symbol.upper() != target:
                continue
            if start <= b.time.date() <= end:
                yield b


# ---------------------------------------------------------------------------
# Engine driver — runs a strategy through ``BacktestEngine`` against a
# pre-built bar list. Same engine config as the existing parity tests
# (signal-bar-close fills, zero commission) so spec results are directly
# comparable to hand-coded results.
# ---------------------------------------------------------------------------
def run_strategy(
    strategy: Strategy,
    bars: list[TradeBar],
    *,
    symbol: str = SYMBOL,
    start_date: tuple[int, int, int] = (2024, 1, 2),
    end_date: tuple[int, int, int] = (2024, 12, 31),
) -> list[LoggedTrade]:
    """Run a Strategy through BacktestEngine and return its trade_log.

    Wraps ``initialize`` to pin the strategy's date window to the synthetic
    data range — both hand-coded references and ``SpecAlgorithm`` use a
    SPY default window in their own initialize() that doesn't match
    synthetic data dates.
    """
    # Patch initialize() to also override dates after the strategy's own
    # initialize() sets its defaults. Same pattern as the existing
    # test_sma_crossover_parity script.
    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(*start_date)
        strategy.set_end_date(*end_date)

    strategy.initialize = _patched_init  # type: ignore[assignment]

    reader = FakeDataReader(bars=bars)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("0"),
        ),
    )
    engine.run(strategy)
    # ``trade_log`` is populated by the strategy's ``on_order_event``;
    # accessed directly off the strategy instance.
    return getattr(strategy, "trade_log", [])


# ---------------------------------------------------------------------------
# Trade-log diffing.
# ---------------------------------------------------------------------------
def assert_trade_logs_match(
    spec_trades: list[LoggedTrade],
    ref_trades: list[LoggedTrade],
    *,
    label: str,
) -> None:
    """Trade-by-trade equivalence check.

    Compares entry/exit times, prices, PnL, result, and indicator-snapshot
    *values* (not keys — the spec uses spec-defined ids while the hand-
    coded references use their own f-string keys; values are what matter
    for parity).
    """
    if len(spec_trades) != len(ref_trades):
        raise AssertionError(
            f"{label}: trade count mismatch — spec={len(spec_trades)} ref={len(ref_trades)}\n"
            f"spec results: {[t.result for t in spec_trades]}\n"
            f"ref results:  {[t.result for t in ref_trades]}"
        )

    for i, (sp, rf) in enumerate(zip(spec_trades, ref_trades, strict=True)):
        problems: list[str] = []
        if sp.entry_time != rf.entry_time:
            problems.append(f"entry_time {sp.entry_time} != {rf.entry_time}")
        if sp.exit_time != rf.exit_time:
            problems.append(f"exit_time {sp.exit_time} != {rf.exit_time}")
        if sp.entry_price != rf.entry_price:
            problems.append(f"entry_price {sp.entry_price} != {rf.entry_price}")
        if sp.exit_price != rf.exit_price:
            problems.append(f"exit_price {sp.exit_price} != {rf.exit_price}")
        if sp.pnl_pts != rf.pnl_pts:
            problems.append(f"pnl_pts {sp.pnl_pts} != {rf.pnl_pts}")
        if sp.pnl_pct != rf.pnl_pct:
            problems.append(f"pnl_pct {sp.pnl_pct} != {rf.pnl_pct}")
        if sp.result != rf.result:
            problems.append(f"result {sp.result!r} != {rf.result!r}")

        # Indicator-snapshot values (sorted, since the spec and reference
        # use different keys for the same indicators).
        sp_vals = sorted(Decimal(v) for v in sp.indicators.values())
        rf_vals = sorted(Decimal(v) for v in rf.indicators.values())
        if sp_vals != rf_vals:
            problems.append(f"indicator snapshot values {sp_vals} != {rf_vals}")

        if problems:
            raise AssertionError(f"{label}: trade #{i + 1} mismatch\n  - " + "\n  - ".join(problems))


def load_spec_algo(name: str) -> SpecAlgorithm:
    spec = load_spec_from_path(fixture_path(name))
    return SpecAlgorithm(spec)
