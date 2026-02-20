from __future__ import annotations

import numpy as np


def calculate_rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Root Mean Squared Error."""
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def calculate_mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Mean Absolute Error."""
    return float(np.mean(np.abs(actual - predicted)))


def calculate_mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Mean Absolute Percentage Error. Excludes zero-actual values."""
    mask = actual != 0
    if not np.any(mask):
        return 0.0
    return float(
        np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100
    )


def calculate_directional_accuracy(
    actual: np.ndarray, predicted: np.ndarray
) -> float:
    """Percentage of times the model correctly predicts the direction of change."""
    if len(actual) < 2:
        return 0.0
    actual_direction = np.sign(np.diff(actual))
    predicted_direction = np.sign(np.diff(predicted))
    correct = np.sum(actual_direction == predicted_direction)
    return float(correct / len(actual_direction) * 100)
