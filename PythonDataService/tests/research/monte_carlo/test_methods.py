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
    def test_reshuffle_properties(self):
        returns = np.array([0.01, -0.02, 0.005, -0.01, 0.03])
        rng = np.random.default_rng(42)
        out = reshuffle_trades(returns, rng=rng)
        # Same length, same multiset (sort and compare).
        assert out.size == returns.size, "preserves length"
        np.testing.assert_array_equal(np.sort(out), np.sort(returns), err_msg="preserves multiset")

        returns = np.array([0.01, -0.02, 0.005])
        before = returns.copy()
        rng = np.random.default_rng(0)
        reshuffle_trades(returns, rng=rng)
        np.testing.assert_array_equal(returns, before, err_msg="does not mutate input")

        out = reshuffle_trades(np.array([]), rng=np.random.default_rng(0))
        assert out.size == 0, "empty input returns empty"


# ---------------------------------------------------------------------------
# Resample.
# ---------------------------------------------------------------------------
class TestResample:
    def test_resample_outputs(self):
        returns = np.array([0.01, -0.02, 0.03])
        out = resample_trades(returns, size=10, rng=np.random.default_rng(0))
        assert out.size == 10, "size argument controls output length"

        returns = np.array([0.01, -0.02, 0.03, -0.005])
        out = resample_trades(returns, size=20, rng=np.random.default_rng(0))
        assert set(np.unique(out).tolist()).issubset(set(returns.tolist())), "output values are subset of input"

        returns = np.array([0.01, -0.02])
        out = resample_trades(returns, size=0, rng=np.random.default_rng(0))
        assert out.size == 0, "size zero returns empty"

    @pytest.mark.parametrize(
        ("returns", "size", "match"),
        [
            pytest.param((0.01,), -1, "size must be non-negative", id="negative_size_raises"),
            pytest.param((), 5, "empty returns", id="empty_universe_with_nonzero_size_raises"),
        ],
    )
    def test_invalid_arguments_raise(self, returns, size, match):
        with pytest.raises(ValueError, match=match):
            resample_trades(np.array(returns), size=size, rng=np.random.default_rng(0))


# ---------------------------------------------------------------------------
# equity_curve.
# ---------------------------------------------------------------------------
class TestEquityCurve:
    def test_start_compounding_and_empty_returns(self):
        curve = equity_curve(100_000, np.array([0.01, -0.02]))
        assert curve[0] == 100_000, "starts at initial equity"
        # 100,000 * 1.01 = 101,000; 101,000 * 0.98 = 98,980.
        assert curve[1] == pytest.approx(101_000), "compounds correctly at first step"
        assert curve[2] == pytest.approx(98_980), "compounds correctly at second step"

        empty_curve = equity_curve(100_000, np.array([]))
        np.testing.assert_array_equal(
            empty_curve, np.array([100_000.0]), err_msg="empty returns yields single point"
        )

    def test_output_length(self):
        # ``len(curve) == len(returns) + 1``.
        for n in [0, 1, 5, 100]:
            curve = equity_curve(1000, np.random.default_rng(0).standard_normal(n) * 0.01)
            assert curve.size == n + 1


# ---------------------------------------------------------------------------
# max_drawdown.
# ---------------------------------------------------------------------------
class TestMaxDrawdown:
    def test_max_drawdown_outcomes(self):
        curve = np.array([100, 105, 110, 120], dtype=float)
        assert max_drawdown(curve) == 0.0, "monotonic curve has zero drawdown"

        # Peak 120, trough 90 → drawdown = (120 - 90) / 120 = 0.25.
        curve = np.array([100, 120, 110, 90, 95], dtype=float)
        assert max_drawdown(curve) == pytest.approx(0.25), "simple drop from peak"

        assert max_drawdown(np.array([100], dtype=float)) == 0.0, "single-point curve returns zero"
        assert max_drawdown(np.array([], dtype=float)) == 0.0, "empty curve returns zero"

        rng = np.random.default_rng(0)
        returns = rng.standard_normal(200) * 0.01
        curve = equity_curve(100_000, returns)
        dd = max_drawdown(curve)
        assert 0.0 <= dd <= 1.0, "drawdown is in zero-one for reasonable curves"


# ---------------------------------------------------------------------------
# max_losing_streak.
# ---------------------------------------------------------------------------
class TestMaxLosingStreak:
    @pytest.mark.parametrize(
        ("returns", "expected"),
        [
            pytest.param((0.01, 0.02, 0.0), 0, id="no_losses"),
            pytest.param((-0.01, -0.02, -0.005), 3, id="all_losses"),
            # Streaks: [-, -, +, -, -, -, +] → max losing streak = 3.
            pytest.param((-0.01, -0.02, 0.01, -0.005, -0.01, -0.02, 0.005), 3, id="mixed_sequence"),
            # 0 is not a loss; streak resets.
            pytest.param((-0.01, -0.02, 0.0, -0.01, -0.02), 2, id="zero_returns_terminate_streak"),
            pytest.param((), 0, id="empty_returns_zero"),
        ],
    )
    def test_max_losing_streak(self, returns, expected):
        assert max_losing_streak(np.array(returns)) == expected
