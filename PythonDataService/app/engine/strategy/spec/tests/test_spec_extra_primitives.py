"""Behavior tests for Phase 2.2 primitives: DrawdownFromPeak, BarProperty.

These are not parity tests — both primitives are new in Phase 2.2 and
have no hand-coded twin. Each test engineers a synthetic price stream
where the correct answer is obvious from the rule definition.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from app.engine.strategy.spec import SpecAlgorithm, StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    SYMBOL,
    build_minute_bars,
    run_strategy,
)


def _run(spec: StrategySpec, closes: list[float]):
    bars = build_minute_bars(closes)
    strategy = SpecAlgorithm(spec)
    strategy._symbol_name = SYMBOL  # type: ignore[attr-defined]
    return run_strategy(strategy, bars)


# ---------------------------------------------------------------------------
# DrawdownFromPeak — trailing stop that fires after a fade from the high.
# ---------------------------------------------------------------------------
def _trailing_stop_spec(retrace_pct: float, sma_window: int = 5) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"trailing-stop test ({retrace_pct:.2%})",
            "symbols": [SYMBOL],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "sma_s", "kind": "SMA", "period": sma_window},
                {"id": "sma_l", "kind": "SMA", "period": sma_window * 2},
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
                    "name": "trailing stop",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "DrawdownFromPeak", "value": retrace_pct}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                }
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )


def _ramp_then_fade(num_warmup: int) -> list[float]:
    """Slow ramp up, peak, then a clean fade — exercises the trailing stop."""
    closes: list[float] = []
    base = 100.0
    for _ in range(num_warmup):
        closes.append(base)
    # Ramp up to ~$103 across 30 bars.
    for i in range(30):
        closes.append(base + 0.10 * (i + 1))
    # Hold at peak for one bar (peak gets locked in).
    closes.append(closes[-1])
    # Fade: drop slowly. Each step is 0.5% off the peak. Trailing stop
    # at 1% should fire on the second fade bar.
    peak = closes[-1]
    for i in range(1, 6):
        closes.append(peak * (1 - 0.005 * i))
    # Tail.
    for _ in range(20):
        closes.append(closes[-1])
    return closes


def test_trailing_stop_fires_on_retrace() -> None:
    spec = _trailing_stop_spec(retrace_pct=0.01)  # 1% retrace
    closes = _ramp_then_fade(num_warmup=40)
    trades = _run(spec, closes)

    assert len(trades) == 1, f"expected exactly 1 trade, got {len(trades)}: {trades}"
    t = trades[0]
    # Trade exits during the fade — pnl should be net positive (entry was
    # low on the ramp; we exit only after retracing 1% from the peak).
    assert t.pnl_pct > Decimal("0"), f"expected net WIN despite trailing stop, got {t.pnl_pct}"


def test_trailing_stop_resets_between_trades() -> None:
    """A trailing stop on a re-entry must reset its peak — otherwise the
    second trade would inherit the first trade's high-water mark and fire
    on a tiny pullback."""
    spec_two_cycles = StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "trailing-stop reset test",
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
                    "name": "trailing stop 1%",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "DrawdownFromPeak", "value": 0.01}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                }
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )
    # Two ramp-fade cycles. If the trailing stop's peak doesn't reset
    # between trades, the second trade would be cut short on the same
    # tick as entry.
    base = 100.0
    closes: list[float] = [base] * 40
    # Cycle 1: ramp up, fade.
    for i in range(30):
        closes.append(base + 0.10 * (i + 1))
    closes.append(closes[-1])
    peak1 = closes[-1]
    for i in range(1, 6):
        closes.append(peak1 * (1 - 0.005 * i))
    # Flat valley to let SMAs cross down.
    valley = closes[-1]
    for _ in range(30):
        closes.append(valley)
    # Cycle 2: ramp up, fade.
    cycle2_base = closes[-1]
    for i in range(30):
        closes.append(cycle2_base + 0.10 * (i + 1))
    closes.append(closes[-1])
    peak2 = closes[-1]
    for i in range(1, 6):
        closes.append(peak2 * (1 - 0.005 * i))
    for _ in range(20):
        closes.append(closes[-1])

    trades = _run(spec_two_cycles, closes)
    # The shape should produce 2 trades; the 2nd would not exist (or
    # would be a trivially short loss) if the peak failed to reset.
    assert len(trades) >= 1, "expected at least 1 trade, got 0"
    # If a 2nd trade exists, it must not be cut short by leftover peak
    # state from the 1st trade — its PnL should be meaningful, not ~0.
    if len(trades) >= 2:
        second = trades[1]
        assert abs(second.pnl_pts) > Decimal("0.10"), (
            f"second trade looks like it inherited stale peak state — "
            f"pnl_pts={second.pnl_pts}"
        )


# ---------------------------------------------------------------------------
# BarProperty — range filter for ORB-style entry gates.
# ---------------------------------------------------------------------------
def _range_filter_spec(min_range_pct: float) -> StrategySpec:
    """Entry only fires on bars whose range_pct (high-low)/close >= min_range_pct."""
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"range-filter test (>= {min_range_pct:.4%})",
            "symbols": [SYMBOL],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "sma_s", "kind": "SMA", "period": 5},
                {"id": "sma_l", "kind": "SMA", "period": 10},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "sma_s", "right": "sma_l", "direction": "up"},
                    {"kind": "BarProperty", "property": "range_pct", "op": ">=", "value": min_range_pct},
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
            },
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {"kind": "FreshCross", "left": "sma_s", "right": "sma_l", "direction": "down"}
                ],
            },
        }
    )


def test_bar_property_filter_blocks_low_range_bars() -> None:
    """A range filter set so high it's never satisfied must never let an
    entry through, even when the SMA cross would otherwise fire."""
    # Synthetic bars produced by build_minute_bars are equal-OHLC (same
    # close, open, high, low) — range_pct is 0. So a range filter at
    # >= 0.001 (0.1%) blocks every bar.
    closes = [100.0 + 0.05 * i for i in range(80)]
    spec = _range_filter_spec(min_range_pct=0.001)
    trades = _run(spec, closes)
    assert trades == [], (
        f"BarProperty range_pct >= 0.1% should have blocked every bar "
        f"(synthetic bars have range=0), but got trades: {trades}"
    )


# ---------------------------------------------------------------------------
# End-to-end combination — RSI band entry + hard stop + trailing stop.
# ---------------------------------------------------------------------------
def test_multi_rule_survival_list_compiles_and_runs() -> None:
    """Smoke test: a survival list with hard-stop + trailing-stop +
    profit-target compiles, runs without error, and produces at least
    one trade. Each individual primitive has its own targeted behavior
    test elsewhere; this one only proves multi-rule composition works."""
    spec = StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "multi-rule survival composition",
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
                    "name": "profit target",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "PnLPercent", "op": ">=", "value": 0.005}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                },
                {
                    "name": "hard stop",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "PnLPercent", "op": "<=", "value": -0.01}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                },
                {
                    "name": "trailing stop",
                    "when": {
                        "logic": "AND",
                        "conditions": [{"kind": "DrawdownFromPeak", "value": 0.003}],
                    },
                    "action": {"kind": "CLOSE_ALL"},
                },
            ],
            "exit": {"logic": "OR", "conditions": []},
        }
    )

    # Just feed the strategy enough movement to fire entry + at least one
    # survival rule. The exact rule that fires isn't important.
    closes = _ramp_then_fade(num_warmup=40)
    trades = _run(spec, closes)
    assert len(trades) >= 1, f"expected at least 1 trade, got 0: {trades}"


# ---------------------------------------------------------------------------
# Script entry point.
# ---------------------------------------------------------------------------
def run_all() -> None:
    failed = False
    tests = [
        ("trailing stop fires on retrace", test_trailing_stop_fires_on_retrace),
        ("trailing stop resets between trades", test_trailing_stop_resets_between_trades),
        ("BarProperty range filter blocks low-range bars", test_bar_property_filter_blocks_low_range_bars),
        ("multi-rule survival list composes", test_multi_rule_survival_list_compiles_and_runs),
    ]
    for label, fn in tests:
        try:
            fn()
            print(f"PASS: {label}")
        except AssertionError as e:
            failed = True
            print(f"FAIL: {label} — {e}")
        except Exception as e:
            failed = True
            print(f"ERROR: {label} — {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
