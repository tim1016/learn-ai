from __future__ import annotations

import numpy as np

from app.ml.evaluation.metrics import (
    calculate_directional_accuracy,
    calculate_mae,
    calculate_mape,
    calculate_rmse,
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
