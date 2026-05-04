"""Spec RSI mean reversion ≡ ``RsiMeanReversionAlgorithm``.

Phase 1 acceptance gate. Runs the spec evaluator and the hand-coded
``RsiMeanReversionAlgorithm`` through the same engine configuration on
the same synthetic minute bars; asserts trade logs match trade-by-trade.

See ``test_spec_sma_parity.py`` for the full parity contract description.
"""

from __future__ import annotations

import sys

from app.engine.strategy.algorithms.rsi_mean_reversion import RsiMeanReversionAlgorithm
from app.engine.strategy.spec.tests._parity_helpers import (
    RESOLUTION_MINUTES,
    SYMBOL,
    assert_trade_logs_match,
    build_minute_bars,
    closes_for_rsi,
    load_spec_algo,
    run_strategy,
)

WINDOW = 14
OVERSOLD = 30.0
OVERBOUGHT = 70.0
NUM_BARS = 500
MIN_TRADES = 2


def _run_parity() -> tuple[list, list]:
    closes = closes_for_rsi(NUM_BARS)
    bars = build_minute_bars(closes)

    ref_strategy = RsiMeanReversionAlgorithm(
        symbol=SYMBOL,
        window=WINDOW,
        oversold=OVERSOLD,
        overbought=OVERBOUGHT,
        resolution_minutes=RESOLUTION_MINUTES,
    )
    ref_trades = run_strategy(ref_strategy, bars)

    spec_strategy = load_spec_algo("rsi_mean_reversion")
    spec_strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    spec_trades = run_strategy(spec_strategy, bars)

    return spec_trades, ref_trades


def test_rsi_mean_reversion_spec_matches_hand_coded() -> None:
    spec_trades, ref_trades = _run_parity()
    assert len(ref_trades) >= MIN_TRADES, (
        f"vacuous test — reference produced only {len(ref_trades)} trades; "
        f"adjust the synthetic data generator"
    )
    assert_trade_logs_match(
        spec_trades, ref_trades, label="RSI mean-reversion spec parity"
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
            spec_trades, ref_trades, label="RSI mean-reversion spec parity"
        )
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)

    print(
        f"PASS: spec RSI mean reversion reproduces RsiMeanReversionAlgorithm "
        f"({len(spec_trades)} trades, identical trade-by-trade)"
    )


if __name__ == "__main__":
    run_parity()
