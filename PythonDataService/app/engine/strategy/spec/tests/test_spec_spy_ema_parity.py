"""Spec SPY EMA crossover ≡ ``SpyEmaCrossoverAlgorithm``.

Phase 1 acceptance gate — the most demanding of the three. The hand-coded
``SpyEmaCrossoverAlgorithm`` is bit-exact against LEAN's reference C#
output (validated by ``test_spy_validation``); proving that
``SpecAlgorithm`` reproduces ``SpyEmaCrossoverAlgorithm`` trade-by-trade
on a controlled synthetic stream therefore inherits LEAN bit-exactness
for the spec layer on this rule set.

The parity test uses synthetic bars rather than the LEAN data dump
(which lives outside the container) — what's exercised is the
**algorithmic logic** of the spec, not the LEAN data path.

See ``test_spec_sma_parity.py`` for the parity contract description.
"""

from __future__ import annotations

import sys

from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from app.engine.strategy.spec.tests._parity_helpers import (
    assert_trade_logs_match,
    build_minute_bars,
    closes_for_spy_ema,
    load_spec_algo,
    run_strategy,
)

NUM_BARS = 2000
MIN_TRADES = 3


def _run_parity() -> tuple[list, list]:
    closes = closes_for_spy_ema(NUM_BARS)
    bars = build_minute_bars(closes)

    # Hand-coded reference. SpyEmaCrossoverAlgorithm parameterizes the symbol
    # so we can drive it against the synthetic TEST stream while keeping
    # the LEAN-bit-exact rule set (EMA(5)/EMA(10) + RSI(14), 5-bar exit).
    from app.engine.strategy.spec.tests._parity_helpers import SYMBOL

    ref_strategy = SpyEmaCrossoverAlgorithm(symbol=SYMBOL)
    ref_trades = run_strategy(ref_strategy, bars)

    spec_strategy = load_spec_algo("spy_ema_crossover")
    spec_strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    spec_trades = run_strategy(spec_strategy, bars)

    return spec_trades, ref_trades


def test_spy_ema_spec_matches_hand_coded() -> None:
    spec_trades, ref_trades = _run_parity()
    assert len(ref_trades) >= MIN_TRADES, (
        f"vacuous test — reference produced only {len(ref_trades)} trades; "
        f"adjust the synthetic data generator"
    )
    assert_trade_logs_match(
        spec_trades, ref_trades, label="SPY EMA crossover spec parity"
    )


def run_parity() -> None:
    try:
        spec_trades, ref_trades = _run_parity()
    except Exception as e:
        print(f"FAIL: setup error — {e}")
        sys.exit(1)

    print(f"Reference trades : {len(ref_trades)}  → {[t.result for t in ref_trades]}")
    print(f"Spec trades      : {len(spec_trades)} → {[t.result for t in spec_trades]}")
    print()

    if len(ref_trades) < MIN_TRADES:
        print(f"FAIL: too few trades ({len(ref_trades)}) — test is vacuous")
        sys.exit(1)

    try:
        assert_trade_logs_match(
            spec_trades, ref_trades, label="SPY EMA crossover spec parity"
        )
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)

    print(
        f"PASS: spec SPY EMA crossover reproduces SpyEmaCrossoverAlgorithm "
        f"({len(spec_trades)} trades, identical trade-by-trade)"
    )


if __name__ == "__main__":
    run_parity()
