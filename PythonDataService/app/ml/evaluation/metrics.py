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


# --- Trading-relevant metrics ---


def calculate_sharpe_ratio(
    returns: np.ndarray,
    risk_free_rate: float = 0.0,
    annualization_factor: float = 252.0,
) -> float:
    """Annualized Sharpe ratio.

    Args:
        returns: Array of period returns (e.g., daily returns).
        risk_free_rate: Risk-free rate per period (default 0 for simplicity).
        annualization_factor: Trading days per year (default 252).

    Returns:
        Annualized Sharpe ratio. Returns 0.0 if std is zero.
    """
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate
    std = float(np.std(excess, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * np.sqrt(annualization_factor))


def calculate_max_drawdown(equity_curve: np.ndarray) -> float:
    """Maximum peak-to-trough drawdown as a fraction (e.g., 0.15 = 15% drawdown).

    Args:
        equity_curve: Cumulative equity/returns series (not period returns).

    Returns:
        Maximum drawdown as a positive fraction. Returns 0.0 if no drawdown.
    """
    if len(equity_curve) < 2:
        return 0.0
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = (running_max - equity_curve) / np.where(running_max != 0, running_max, 1.0)
    return float(np.max(drawdowns))


def calculate_profit_factor(returns: np.ndarray) -> float:
    """Ratio of gross profits to gross losses.

    Args:
        returns: Array of period returns.

    Returns:
        Profit factor (>1 is profitable). Returns 0.0 if no losses, inf-safe.
    """
    gains = returns[returns > 0]
    losses = returns[returns < 0]
    total_gains = float(np.sum(gains)) if len(gains) > 0 else 0.0
    total_losses = float(np.abs(np.sum(losses))) if len(losses) > 0 else 0.0
    if total_losses == 0:
        return float("inf") if total_gains > 0 else 0.0
    return total_gains / total_losses


def calculate_trading_returns(
    actual: np.ndarray, predicted: np.ndarray
) -> np.ndarray:
    """Compute strategy returns: go long when model predicts up, short when down.

    Uses the predicted direction (sign of predicted change) applied to actual returns.

    Args:
        actual: Actual values (scaled or unscaled).
        predicted: Predicted values.

    Returns:
        Array of strategy returns (length = len(actual) - 1).
    """
    if len(actual) < 2:
        return np.array([], dtype=np.float64)
    actual_returns = np.diff(actual) / np.where(actual[:-1] != 0, actual[:-1], 1.0)
    predicted_direction = np.sign(np.diff(predicted))
    return actual_returns * predicted_direction
