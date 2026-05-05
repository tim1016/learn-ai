"""Spec SMA crossover ≡ ``SmaCrossoverAlgorithm`` (hand-coded reference).

Phase 1 acceptance gate. Runs both the spec evaluator and the hand-coded
``SmaCrossoverAlgorithm`` against the same synthetic minute-bar stream
through the same ``BacktestEngine`` configuration, and asserts the trade
logs match trade-by-trade on:

  * entry/exit timestamps
  * entry/exit prices
  * PnL (points and percent)
  * WIN/LOSS verdict
  * indicator-snapshot values

The hand-coded twin is the canonical implementation per
``docs/math-sources-of-truth.md``; ``SpecAlgorithm`` is the parity-pinned
secondary. If this test ever fails, the spec layer has drifted and the
hand-coded version is the authority.
"""

from __future__ import annotations

import sys

from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm
from app.engine.strategy.spec.tests._parity_helpers import (
    RESOLUTION_MINUTES,
    SYMBOL,
    assert_trade_logs_match,
    build_minute_bars,
    closes_for_sma,
    configure_script_logger,
    load_spec_algo,
    logger,
    run_strategy,
)

# Spec uses 10/30 (canonical defaults). Generate enough bars to produce a
# meaningful number of crossovers.
SHORT_WINDOW = 10
LONG_WINDOW = 30
NUM_BARS = 800
MIN_TRADES = 3  # if we can't generate at least this many, the test is vacuous


def _run_parity() -> tuple[list, list]:
    closes = closes_for_sma(NUM_BARS)
    bars = build_minute_bars(closes)

    # Hand-coded reference, parameterized to match the canonical spec
    # (symbol=SPY in spec but we override with TEST symbol so the
    # synthetic-bar reader matches; window/resolution match).
    ref_strategy = SmaCrossoverAlgorithm(
        symbol=SYMBOL,
        short_window=SHORT_WINDOW,
        long_window=LONG_WINDOW,
        resolution_minutes=RESOLUTION_MINUTES,
    )
    ref_trades = run_strategy(ref_strategy, bars)

    # Spec-driven strategy, loaded from the canonical fixture.
    spec_strategy = load_spec_algo("sma_crossover")
    # Override symbol to match the synthetic bar stream. The fixture
    # symbol is "SPY"; the synthetic data uses "TEST". This is a
    # test-time substitution, not a schema change.
    spec_strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    spec_trades = run_strategy(spec_strategy, bars)

    return spec_trades, ref_trades


def test_sma_spec_matches_hand_coded() -> None:
    spec_trades, ref_trades = _run_parity()
    assert len(ref_trades) >= MIN_TRADES, (
        f"vacuous test — reference produced only {len(ref_trades)} trades; "
        f"adjust the synthetic data generator"
    )
    assert_trade_logs_match(spec_trades, ref_trades, label="SMA crossover spec parity")


def run_parity() -> None:
    configure_script_logger()
    try:
        spec_trades, ref_trades = _run_parity()
    except Exception as e:
        logger.error("FAIL: setup error — %s", e)
        sys.exit(1)

    logger.info("Reference trades : %d  → %s", len(ref_trades), [t.result for t in ref_trades])
    logger.info("Spec trades      : %d → %s", len(spec_trades), [t.result for t in spec_trades])

    if len(ref_trades) < MIN_TRADES:
        logger.error("FAIL: too few trades (%d) — test is vacuous", len(ref_trades))
        sys.exit(1)

    try:
        assert_trade_logs_match(spec_trades, ref_trades, label="SMA crossover spec parity")
    except AssertionError as e:
        logger.error("FAIL: %s", e)
        sys.exit(1)

    logger.info(
        "PASS: spec SMA crossover reproduces SmaCrossoverAlgorithm "
        "(%d trades, identical trade-by-trade)",
        len(spec_trades),
    )


if __name__ == "__main__":
    run_parity()
