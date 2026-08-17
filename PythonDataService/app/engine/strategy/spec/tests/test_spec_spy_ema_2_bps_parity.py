"""EMA Crossover 2 bps StrategySpec parity acceptance gate.

The independent LEAN trade oracle is fixture ``ENG-007``, exercised directly
against both implementations by
``tests/integration/reconciliation/test_ema_crossover_2_bps_lean_golden.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.strategy.algorithms.ema_crossover_2_bps import EmaCrossover2BpsAlgorithm
from app.engine.strategy.spec.tests._parity_helpers import (
    SYMBOL,
    assert_trade_logs_match,
    build_minute_bars,
    closes_for_spy_ema,
    load_spec_algo,
    run_strategy,
)


def test_two_bps_spec_matches_hand_coded_strategy_trade_by_trade() -> None:
    bars = build_minute_bars(closes_for_spy_ema(2000))

    reference = run_strategy(EmaCrossover2BpsAlgorithm(symbol=SYMBOL), bars)
    spec_strategy = load_spec_algo("ema_crossover_2_bps")
    spec_strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    spec_trades = run_strategy(spec_strategy, bars)

    assert len(reference) >= 3, "parity test must exercise multiple trades"
    assert_trade_logs_match(spec_trades, reference, label="EMA Crossover 2 bps spec parity")


def test_two_bps_spec_changes_only_identity_copy_and_gap_operand() -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    original = json.loads((fixtures / "spy_ema_crossover.spec.json").read_text(encoding="utf-8"))
    two_bps = json.loads((fixtures / "ema_crossover_2_bps.spec.json").read_text(encoding="utf-8"))

    two_bps.pop("strategy_key")
    two_bps["name"] = original["name"]
    two_bps["description"] = original["description"]
    two_bps["entry"]["conditions"][1] = original["entry"]["conditions"][1]

    assert two_bps == original
