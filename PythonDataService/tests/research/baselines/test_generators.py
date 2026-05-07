"""Spec-generator tests.

Verifies the v1 baseline methods produce valid ``StrategySpec``
instances that the engine can actually run, and that the generators
reject pathological inputs early.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.research.baselines.generators import (
    buy_and_hold_spec,
    random_ema_window_specs,
)
from app.research.runs.ledger import RunLedger


def _make_parent(**overrides) -> RunLedger:
    base: dict = {
        "schema_version": "1.0",
        "run_id": "a" * 32,
        "strategy_spec_id": "spy_ema_crossover",
        "strategy_spec_hash": "d" * 64,
        "strategy_spec_json": {},
        "engine_git_commit": "test",
        "symbol": "TEST",
        "resolution_minutes": 15,
        "start_ms": 1704171600000,
        "end_ms": 1735621200000,
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "data_snapshot_id": "TEST|15|...|test",
    }
    base.update(overrides)
    return RunLedger(**base)


# ---------------------------------------------------------------------------
# Buy-and-hold.
# ---------------------------------------------------------------------------
class TestBuyAndHold:
    def test_uses_parent_symbol_and_resolution(self):
        parent = _make_parent(symbol="QQQ", resolution_minutes=30)
        spec = buy_and_hold_spec(parent)
        assert spec.symbols == ["QQQ"]
        assert spec.resolution.period_minutes == 30

    def test_entry_uses_tautological_bar_property(self):
        spec = buy_and_hold_spec(_make_parent())
        # The condition list has exactly one BarProperty entry that's
        # tautologically true (range >= 0; OHLC invariant guarantees
        # high >= low so the bar's range is always non-negative).
        assert len(spec.entry.conditions) == 1
        cond = spec.entry.conditions[0]
        assert cond.kind == "BarProperty"  # type: ignore[union-attr]
        assert cond.property == "range"  # type: ignore[union-attr]
        assert cond.op == ">="  # type: ignore[union-attr]
        assert cond.value == 0.0  # type: ignore[union-attr]

    def test_exit_never_fires_during_run(self):
        spec = buy_and_hold_spec(_make_parent())
        # ``BarsSinceEntry >= 999_999`` is unreachable for any
        # realistic backtest; engine flushes via on_end_of_algorithm.
        assert len(spec.exit.conditions) == 1
        cond = spec.exit.conditions[0]
        assert cond.kind == "BarsSinceEntry"  # type: ignore[union-attr]
        assert cond.value == 999_999  # type: ignore[union-attr]

    def test_no_indicators_declared(self):
        # B&H needs no indicator state — keeps the spec minimal.
        spec = buy_and_hold_spec(_make_parent())
        assert spec.indicators == []


# ---------------------------------------------------------------------------
# Random EMA windows.
# ---------------------------------------------------------------------------
class TestRandomEmaWindows:
    def test_produces_requested_count(self):
        parent = _make_parent()
        rng = np.random.default_rng(0)
        out = random_ema_window_specs(parent, count=10, rng=rng)
        assert len(out) == 10

    def test_every_pair_has_slow_greater_than_fast(self):
        parent = _make_parent()
        rng = np.random.default_rng(0)
        for _, params in random_ema_window_specs(parent, count=50, rng=rng):
            assert params["slow"] > params["fast"]

    def test_pairs_in_requested_ranges(self):
        parent = _make_parent()
        rng = np.random.default_rng(0)
        out = random_ema_window_specs(
            parent, count=30, fast_range=(3, 5), slow_range=(8, 12), rng=rng
        )
        for _, params in out:
            assert 3 <= params["fast"] <= 5
            assert 8 <= params["slow"] <= 12

    def test_same_seed_produces_identical_output(self):
        parent = _make_parent()
        a = random_ema_window_specs(parent, count=10, rng=np.random.default_rng(42))
        b = random_ema_window_specs(parent, count=10, rng=np.random.default_rng(42))
        assert [p for _, p in a] == [p for _, p in b]

    def test_uses_parent_symbol_and_resolution(self):
        parent = _make_parent(symbol="IWM", resolution_minutes=60)
        spec, _ = random_ema_window_specs(
            parent, count=1, rng=np.random.default_rng(0)
        )[0]
        assert spec.symbols == ["IWM"]
        assert spec.resolution.period_minutes == 60

    def test_zero_count_raises(self):
        with pytest.raises(ValueError, match="count must be positive"):
            random_ema_window_specs(
                _make_parent(), count=0, rng=np.random.default_rng(0)
            )

    def test_invalid_period_lower_bound_raises(self):
        with pytest.raises(ValueError, match="EMA periods must be"):
            random_ema_window_specs(
                _make_parent(),
                count=5,
                fast_range=(1, 10),  # 1 < schema's minimum of 2
                rng=np.random.default_rng(0),
            )

    def test_inverted_range_raises(self):
        with pytest.raises(ValueError, match="range upper bounds"):
            random_ema_window_specs(
                _make_parent(),
                count=5,
                fast_range=(10, 5),  # hi < lo
                rng=np.random.default_rng(0),
            )

    def test_unsatisfiable_constraints_raise(self):
        # ``slow_hi <= fast_lo`` means no valid (fast, slow) pair
        # exists — fail fast rather than loop forever.
        with pytest.raises(ValueError, match="admits no valid"):
            random_ema_window_specs(
                _make_parent(),
                count=5,
                fast_range=(20, 30),
                slow_range=(10, 15),
                rng=np.random.default_rng(0),
            )

    def test_uses_canonical_spy_ema_shape(self):
        """Generated specs use FreshCross + 0.20 gap + RSI(50,70) +
        5-bar hold — same shape as the canonical SPY EMA fixture but
        with random EMA windows. The architecture spec calls this the
        'EMA window family' null baseline.
        """
        parent = _make_parent()
        spec, _ = random_ema_window_specs(
            parent, count=1, rng=np.random.default_rng(0)
        )[0]
        # Three indicators: fast EMA, slow EMA, RSI(14).
        kinds = sorted(ind.kind for ind in spec.indicators)
        assert kinds == ["EMA", "EMA", "RSI"]
        # Five-bar hold via BarsSinceEntry on the exit.
        assert len(spec.exit.conditions) == 1
        exit_cond = spec.exit.conditions[0]
        assert exit_cond.kind == "BarsSinceEntry"  # type: ignore[union-attr]
        assert exit_cond.value == 5  # type: ignore[union-attr]
