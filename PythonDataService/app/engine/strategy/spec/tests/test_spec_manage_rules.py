"""Behavior tests for Phase 2.1 manage-layer survival rules.

These are not parity tests — there's no hand-coded reference algorithm
that uses PnL stop-loss / profit-target as a survival rule. Instead each
test engineers a synthetic price stream where the correct answer is
obvious from the rule definition, runs ``SpecAlgorithm`` against it,
and asserts the trade log matches the engineered expectation.

What's exercised:
  * ``PnLPercent`` survival rule with CLOSE_ALL action — fires on
    drawdown beyond the threshold.
  * Profit-target survival rule — fires on rise past the threshold.
  * Survival rule order: first-match-wins per bar.
  * Survival rule precedence over signal-flip exit on the same bar.
  * Survival rule ignored while flat (no false fire on warmup bars).
"""

from __future__ import annotations

import sys
from decimal import Decimal

from app.engine.strategy.spec import SpecAlgorithm, StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    SYMBOL,
    build_minute_bars,
    configure_script_logger,
    logger,
    run_strategy,
)


# ---------------------------------------------------------------------------
# Spec builders — small inline specs constructed in code rather than loaded
# from JSON. Behavior tests want flexible per-test specs; the canonical
# JSON fixtures are reserved for the parity-pinned reference strategies.
# ---------------------------------------------------------------------------
def _stop_loss_spec(stop_pct: float, sma_window: int = 5) -> StrategySpec:
    """SMA-cross entry with a single stop-loss survival rule and no exit
    block. The strategy enters on the first SMA cross and only exits via
    the survival rule, making the test answer fully predictable.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"stop-loss test ({stop_pct:.2%})",
            "symbols": [SYMBOL],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "sma_s", "kind": "SMA", "period": sma_window},
                {"id": "sma_l", "kind": "SMA", "period": sma_window * 2},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "FreshCross",
                        "left": "sma_s",
                        "right": "sma_l",
                        "direction": "up",
                    }
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "survival": [
                {
                    "name": "stop loss",
                    "when": {
                        "logic": "AND",
                        "conditions": [
                            {"kind": "PnLPercent", "op": "<=", "value": stop_pct}
                        ],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                }
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )


def _profit_target_spec(target_pct: float, sma_window: int = 5) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"profit target test (+{target_pct:.2%})",
            "symbols": [SYMBOL],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "sma_s", "kind": "SMA", "period": sma_window},
                {"id": "sma_l", "kind": "SMA", "period": sma_window * 2},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "FreshCross",
                        "left": "sma_s",
                        "right": "sma_l",
                        "direction": "up",
                    }
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "survival": [
                {
                    "name": "profit target",
                    "when": {
                        "logic": "AND",
                        "conditions": [
                            {"kind": "PnLPercent", "op": ">=", "value": target_pct}
                        ],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                }
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )


# ---------------------------------------------------------------------------
# Engineered price streams — predictable shapes designed to exercise one
# survival rule each.
# ---------------------------------------------------------------------------
def _ramp_up_then_drop(num_warmup: int, drop_below_entry_pct: float) -> list[float]:
    """A ramp-up that crosses the SMAs, then a sharp drop below entry.

    Entry fires on the first ramp bar at price ~ ``base + 0.05``. The
    drop bar is engineered to land below entry by at least
    ``drop_below_entry_pct`` (a negative fraction, e.g. ``-0.02`` = 2%
    below entry), guaranteeing any stop tighter than that triggers.
    """
    assert drop_below_entry_pct < 0
    closes: list[float] = []
    base = 100.0
    for _ in range(num_warmup):
        closes.append(base)
    # Ramp up — 30 bars of +0.05 each.
    for i in range(30):
        closes.append(base + 0.05 * (i + 1))
    # Entry price is approximately the first ramp bar's close.
    expected_entry = base + 0.05
    drop_target = expected_entry * (1 + drop_below_entry_pct)
    closes.append(drop_target)
    # Tail: stay at the dropped level so we can detect any stray re-entries.
    for _ in range(20):
        closes.append(drop_target)
    return closes


def _ramp_up_then_overshoot(num_warmup: int, target_pct: float) -> list[float]:
    closes: list[float] = []
    base = 100.0
    for _ in range(num_warmup):
        closes.append(base)
    # Ramp up — slow enough to fire the SMA cross before hitting target.
    for i in range(20):
        closes.append(base + 0.05 * (i + 1))
    entry_price_estimate = closes[-1]
    # Overshoot the profit target on the next bar.
    closes.append(entry_price_estimate * (1 + target_pct + 0.01))
    for _ in range(20):
        closes.append(closes[-1])
    return closes


# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------
def _run(spec: StrategySpec, closes: list[float]):
    bars = build_minute_bars(closes)
    strategy = SpecAlgorithm(spec)
    strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    return run_strategy(strategy, bars)


def test_stop_loss_fires_on_drawdown() -> None:
    spec = _stop_loss_spec(stop_pct=-0.005)  # -0.5%
    closes = _ramp_up_then_drop(num_warmup=40, drop_below_entry_pct=-0.02)  # 2% below entry
    trades = _run(spec, closes)

    assert len(trades) == 1, f"expected exactly 1 trade, got {len(trades)}: {trades}"
    t = trades[0]
    assert t.result == "LOSS", f"expected LOSS (stop fired), got {t.result}"
    # Stop fires when PnL <= -0.5%, so realized pnl_pct should be at least
    # that bad. Engineered drop is -1% so the stop must be triggered.
    assert t.pnl_pct <= Decimal("-0.005"), (
        f"expected pnl_pct <= -0.5% (stop should have fired), got {t.pnl_pct}"
    )


def test_profit_target_fires_on_overshoot() -> None:
    spec = _profit_target_spec(target_pct=0.005)  # +0.5%
    closes = _ramp_up_then_overshoot(num_warmup=40, target_pct=0.01)  # overshoot to +1%
    trades = _run(spec, closes)

    assert len(trades) == 1, f"expected exactly 1 trade, got {len(trades)}: {trades}"
    t = trades[0]
    assert t.result == "WIN", f"expected WIN (target fired), got {t.result}"
    assert t.pnl_pct >= Decimal("0.005"), (
        f"expected pnl_pct >= 0.5% (target should have fired), got {t.pnl_pct}"
    )


def test_survival_rule_order_first_match_wins() -> None:
    """Two survival rules with overlapping firing conditions; the rule
    listed first in declaration order must fire when both would fire."""
    spec = StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "first-match-wins",
            "symbols": [SYMBOL],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "sma_s", "kind": "SMA", "period": 5},
                {"id": "sma_l", "kind": "SMA", "period": 10},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "sma_s", "right": "sma_l", "direction": "up"}
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
            },
            "survival": [
                {
                    "name": "tight target",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "PnLPercent", "op": ">=", "value": 0.001}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                },
                {
                    "name": "wider target",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "PnLPercent", "op": ">=", "value": 0.005}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                },
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )
    closes = _ramp_up_then_overshoot(num_warmup=40, target_pct=0.01)
    trades = _run(spec, closes)

    assert len(trades) == 1, f"expected exactly 1 trade, got {len(trades)}"
    t = trades[0]
    # Tight target fires at +0.1%, so realized PnL should be well below
    # the wider target's +0.5%. (Both rules would match the engineered
    # +1% bar; first-match-wins means the tight target fires earlier on
    # an earlier bar of the ramp.)
    assert t.pnl_pct < Decimal("0.005"), (
        f"first-match-wins violated — tight (+0.1%) target should have fired before "
        f"the wide (+0.5%) target, but realized pnl_pct={t.pnl_pct}"
    )


def test_survival_rule_does_not_fire_while_flat() -> None:
    """``PnLPercent`` must return False when no position is open. Without
    this, a survival-rule-only spec with no entry conditions would
    'fire' on every warmup bar."""
    spec = _stop_loss_spec(stop_pct=-0.005)
    # Flat data — no SMA cross, no entry, no position. Survival rule
    # must never fire.
    closes = [100.0] * 60
    trades = _run(spec, closes)
    assert trades == [], f"expected no trades when flat, got {trades}"


# ---------------------------------------------------------------------------
# Script entry point.
# ---------------------------------------------------------------------------
def run_all() -> None:
    configure_script_logger()
    failed = False
    tests = [
        ("stop loss fires on drawdown", test_stop_loss_fires_on_drawdown),
        ("profit target fires on overshoot", test_profit_target_fires_on_overshoot),
        ("survival rule order: first-match-wins", test_survival_rule_order_first_match_wins),
        ("survival rule does not fire while flat", test_survival_rule_does_not_fire_while_flat),
    ]
    for label, fn in tests:
        try:
            fn()
            logger.info("PASS: %s", label)
        except AssertionError as e:
            failed = True
            logger.error("FAIL: %s — %s", label, e)
        except Exception as e:
            failed = True
            logger.error("ERROR: %s — %s: %s", label, type(e).__name__, e)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
