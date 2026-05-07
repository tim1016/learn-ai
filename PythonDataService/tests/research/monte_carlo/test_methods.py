"""Monte Carlo path-generation primitive tests.

Covers the determinism + multiset + size invariants the runner depends
on. Equity-curve and drawdown helpers tested against the canonical
Phase A formulas (same compounding rule as ``app/engine/results/statistics.py``)
so the simulated outputs are directly comparable to the parent run's
reported metrics.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.research.monte_carlo.methods import (
    equity_curve,
    max_drawdown,
    max_losing_streak,
    resample_trades,
    reshuffle_trades,
)


# ---------------------------------------------------------------------------
# Reshuffle.
# ---------------------------------------------------------------------------
class TestReshuffle:
    def test_preserves_multiset(self):
        returns = np.array([0.01, -0.02, 0.005, -0.01, 0.03])
        rng = np.random.default_rng(42)
        out = reshuffle_trades(returns, rng=rng)
        # Same length, same multiset (sort and compare).
        assert out.size == returns.size
        np.testing.assert_array_equal(np.sort(out), np.sort(returns))

    def test_does_not_mutate_input(self):
        returns = np.array([0.01, -0.02, 0.005])
        before = returns.copy()
        rng = np.random.default_rng(0)
        reshuffle_trades(returns, rng=rng)
        np.testing.assert_array_equal(returns, before)

    def test_same_seed_produces_identical_output(self):
        returns = np.array([0.01, -0.02, 0.005, -0.01, 0.03])
        out_a = reshuffle_trades(returns, rng=np.random.default_rng(7))
        out_b = reshuffle_trades(returns, rng=np.random.default_rng(7))
        np.testing.assert_array_equal(out_a, out_b)

    def test_different_seeds_produce_different_orders(self):
        # Long enough that the probability of two random permutations
        # accidentally matching is negligible (~ 1 / 20!).
        returns = np.arange(20, dtype=float)
        out_a = reshuffle_trades(returns, rng=np.random.default_rng(1))
        out_b = reshuffle_trades(returns, rng=np.random.default_rng(2))
        assert not np.array_equal(out_a, out_b)

    def test_empty_input_returns_empty(self):
        out = reshuffle_trades(np.array([]), rng=np.random.default_rng(0))
        assert out.size == 0


# ---------------------------------------------------------------------------
# Resample.
# ---------------------------------------------------------------------------
class TestResample:
    def test_size_argument_controls_output_length(self):
        returns = np.array([0.01, -0.02, 0.03])
        out = resample_trades(returns, size=10, rng=np.random.default_rng(0))
        assert out.size == 10

    def test_allows_duplicates(self):
        # With size > universe size and a small universe, duplicates are
        # almost guaranteed (P[no dup] is vanishingly small).
        returns = np.array([0.01, -0.01, 0.0])
        out = resample_trades(returns, size=50, rng=np.random.default_rng(0))
        # Pigeonhole: 50 samples from 3 possible values must have dups.
        assert len(np.unique(out)) <= 3

    def test_output_values_are_subset_of_input(self):
        returns = np.array([0.01, -0.02, 0.03, -0.005])
        out = resample_trades(returns, size=20, rng=np.random.default_rng(0))
        assert set(np.unique(out).tolist()).issubset(set(returns.tolist()))

    def test_same_seed_produces_identical_output(self):
        returns = np.array([0.01, -0.02, 0.03])
        out_a = resample_trades(returns, size=15, rng=np.random.default_rng(7))
        out_b = resample_trades(returns, size=15, rng=np.random.default_rng(7))
        np.testing.assert_array_equal(out_a, out_b)

    def test_size_zero_returns_empty(self):
        returns = np.array([0.01, -0.02])
        out = resample_trades(returns, size=0, rng=np.random.default_rng(0))
        assert out.size == 0

    def test_negative_size_raises(self):
        returns = np.array([0.01])
        with pytest.raises(ValueError, match="size must be non-negative"):
            resample_trades(returns, size=-1, rng=np.random.default_rng(0))

    def test_empty_universe_with_nonzero_size_raises(self):
        with pytest.raises(ValueError, match="empty returns"):
            resample_trades(np.array([]), size=5, rng=np.random.default_rng(0))


# ---------------------------------------------------------------------------
# equity_curve.
# ---------------------------------------------------------------------------
class TestEquityCurve:
    def test_starts_at_initial_equity(self):
        curve = equity_curve(100_000, np.array([0.01, -0.02]))
        assert curve[0] == 100_000

    def test_compounds_correctly(self):
        # 100,000 * 1.01 = 101,000; 101,000 * 0.98 = 98,980.
        curve = equity_curve(100_000, np.array([0.01, -0.02]))
        assert curve[1] == pytest.approx(101_000)
        assert curve[2] == pytest.approx(98_980)

    def test_empty_returns_yields_single_point(self):
        curve = equity_curve(100_000, np.array([]))
        np.testing.assert_array_equal(curve, np.array([100_000.0]))

    def test_output_length(self):
        # ``len(curve) == len(returns) + 1``.
        for n in [0, 1, 5, 100]:
            curve = equity_curve(1000, np.random.default_rng(0).standard_normal(n) * 0.01)
            assert curve.size == n + 1


# ---------------------------------------------------------------------------
# max_drawdown.
# ---------------------------------------------------------------------------
class TestMaxDrawdown:
    def test_monotonic_curve_has_zero_drawdown(self):
        curve = np.array([100, 105, 110, 120], dtype=float)
        assert max_drawdown(curve) == 0.0

    def test_simple_drop_from_peak(self):
        # Peak 120, trough 90 → drawdown = (120 - 90) / 120 = 0.25.
        curve = np.array([100, 120, 110, 90, 95], dtype=float)
        assert max_drawdown(curve) == pytest.approx(0.25)

    def test_short_curve_returns_zero(self):
        assert max_drawdown(np.array([100], dtype=float)) == 0.0
        assert max_drawdown(np.array([], dtype=float)) == 0.0

    def test_drawdown_is_in_zero_one_for_reasonable_curves(self):
        rng = np.random.default_rng(0)
        returns = rng.standard_normal(200) * 0.01
        curve = equity_curve(100_000, returns)
        dd = max_drawdown(curve)
        assert 0.0 <= dd <= 1.0


# ---------------------------------------------------------------------------
# max_losing_streak.
# ---------------------------------------------------------------------------
class TestMaxLosingStreak:
    def test_no_losses(self):
        assert max_losing_streak(np.array([0.01, 0.02, 0.0])) == 0

    def test_all_losses(self):
        assert max_losing_streak(np.array([-0.01, -0.02, -0.005])) == 3

    def test_mixed_sequence(self):
        # Streaks: [-, -, +, -, -, -, +] → max losing streak = 3.
        seq = np.array([-0.01, -0.02, 0.01, -0.005, -0.01, -0.02, 0.005])
        assert max_losing_streak(seq) == 3

    def test_zero_returns_terminate_streak(self):
        # 0 is not a loss; streak resets.
        seq = np.array([-0.01, -0.02, 0.0, -0.01, -0.02])
        assert max_losing_streak(seq) == 2

    def test_empty_returns_zero(self):
        assert max_losing_streak(np.array([])) == 0
