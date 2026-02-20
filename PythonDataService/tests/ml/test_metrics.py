from __future__ import annotations

import numpy as np
import pytest

from app.ml.evaluation.metrics import (
    calculate_directional_accuracy,
    calculate_mae,
    calculate_mape,
    calculate_max_drawdown,
    calculate_profit_factor,
    calculate_rmse,
    calculate_sharpe_ratio,
    calculate_trading_returns,
)


class TestCalculateRmse:
    def test_perfect_prediction(self) -> None:
        actual = np.array([1.0, 2.0, 3.0])
        assert calculate_rmse(actual, actual) == 0.0

    def test_known_error(self) -> None:
        actual = np.array([0.0, 0.0])
        predicted = np.array([1.0, 1.0])
        assert calculate_rmse(actual, predicted) == 1.0


class TestCalculateMae:
    def test_known_values(self) -> None:
        actual = np.array([1.0, 2.0, 3.0])
        predicted = np.array([2.0, 3.0, 4.0])
        assert calculate_mae(actual, predicted) == 1.0

    def test_perfect_prediction(self) -> None:
        actual = np.array([1.0, 2.0, 3.0])
        assert calculate_mae(actual, actual) == 0.0


class TestCalculateMape:
    def test_known_values(self) -> None:
        actual = np.array([100.0, 200.0])
        predicted = np.array([110.0, 220.0])
        assert calculate_mape(actual, predicted) == 10.0

    def test_excludes_zeros(self) -> None:
        actual = np.array([0.0, 100.0])
        predicted = np.array([10.0, 110.0])
        # Only the non-zero actual is considered: |110-100|/100 = 10%
        assert calculate_mape(actual, predicted) == 10.0

    def test_all_zeros_returns_zero(self) -> None:
        actual = np.array([0.0, 0.0])
        predicted = np.array([1.0, 2.0])
        assert calculate_mape(actual, predicted) == 0.0


class TestDirectionalAccuracy:
    def test_perfect_directions(self) -> None:
        actual = np.array([1.0, 2.0, 3.0, 4.0])
        predicted = np.array([1.0, 2.0, 3.0, 4.0])
        assert calculate_directional_accuracy(actual, predicted) == 100.0

    def test_opposite_directions(self) -> None:
        actual = np.array([1.0, 2.0, 3.0, 4.0])
        predicted = np.array([4.0, 3.0, 2.0, 1.0])
        assert calculate_directional_accuracy(actual, predicted) == 0.0

    def test_single_point_returns_zero(self) -> None:
        actual = np.array([1.0])
        predicted = np.array([2.0])
        assert calculate_directional_accuracy(actual, predicted) == 0.0


class TestSharpeRatio:
    def test_positive_returns(self) -> None:
        # Constant positive returns → high Sharpe
        returns = np.array([0.01] * 100)
        sharpe = calculate_sharpe_ratio(returns)
        assert sharpe > 0

    def test_zero_std_returns_zero(self) -> None:
        # All identical returns → std=0 → Sharpe=0
        returns = np.array([0.0, 0.0, 0.0])
        assert calculate_sharpe_ratio(returns) == 0.0

    def test_single_point_returns_zero(self) -> None:
        returns = np.array([0.05])
        assert calculate_sharpe_ratio(returns) == 0.0

    def test_negative_returns(self) -> None:
        returns = np.array([-0.01] * 100)
        assert calculate_sharpe_ratio(returns) < 0


class TestMaxDrawdown:
    def test_monotonically_increasing(self) -> None:
        # No drawdown for a strictly increasing curve
        equity = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert calculate_max_drawdown(equity) == 0.0

    def test_known_drawdown(self) -> None:
        # Peak at 10, trough at 5 → 50% drawdown
        equity = np.array([1.0, 5.0, 10.0, 5.0, 8.0])
        dd = calculate_max_drawdown(equity)
        assert dd == pytest.approx(0.5, abs=1e-6)

    def test_single_point_returns_zero(self) -> None:
        equity = np.array([1.0])
        assert calculate_max_drawdown(equity) == 0.0

    def test_full_drawdown(self) -> None:
        # Peak at 10, drop to near zero
        equity = np.array([10.0, 5.0, 1.0])
        dd = calculate_max_drawdown(equity)
        assert dd == pytest.approx(0.9, abs=1e-6)


class TestProfitFactor:
    def test_all_gains(self) -> None:
        returns = np.array([0.01, 0.02, 0.03])
        assert calculate_profit_factor(returns) == float("inf")

    def test_all_losses(self) -> None:
        returns = np.array([-0.01, -0.02, -0.03])
        assert calculate_profit_factor(returns) == 0.0

    def test_equal_gains_and_losses(self) -> None:
        returns = np.array([0.05, -0.05])
        assert calculate_profit_factor(returns) == pytest.approx(1.0)

    def test_known_ratio(self) -> None:
        # Gains = 0.06, Losses = 0.03 → PF = 2.0
        returns = np.array([0.02, -0.01, 0.04, -0.02])
        assert calculate_profit_factor(returns) == pytest.approx(2.0)

    def test_no_trades(self) -> None:
        returns = np.array([0.0, 0.0])
        assert calculate_profit_factor(returns) == 0.0


class TestTradingReturns:
    def test_perfect_prediction_positive(self) -> None:
        # Actual goes up, predicted goes up → positive return
        actual = np.array([100.0, 110.0])
        predicted = np.array([100.0, 105.0])
        tr = calculate_trading_returns(actual, predicted)
        assert len(tr) == 1
        assert tr[0] > 0

    def test_wrong_direction_negative(self) -> None:
        # Actual goes up, predicted goes down → negative return
        actual = np.array([100.0, 110.0])
        predicted = np.array([100.0, 95.0])
        tr = calculate_trading_returns(actual, predicted)
        assert tr[0] < 0

    def test_empty_for_single_point(self) -> None:
        actual = np.array([100.0])
        predicted = np.array([100.0])
        tr = calculate_trading_returns(actual, predicted)
        assert len(tr) == 0
