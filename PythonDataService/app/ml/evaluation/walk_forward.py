from __future__ import annotations

import logging
from typing import List

import numpy as np

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
from app.ml.models.schemas import TrainingConfig, WalkForwardResult
from app.ml.preprocessing.scaler import PriceScaler
from app.ml.preprocessing.windowing import create_sequences
from app.ml.training.lstm_model import build_lstm_model

logger = logging.getLogger(__name__)


def walk_forward_validate(
    data: np.ndarray,
    config: TrainingConfig,
    n_folds: int = 5,
    expanding: bool = True,
) -> WalkForwardResult:
    """Walk-forward validation for time series.

    Splits data into n_folds sequential test windows.
    For each fold, trains on all prior data and tests on the fold's window.

    Args:
        data: Raw unscaled feature array (n_samples, n_features).
        config: Training config (sequence_length, epochs, etc.).
        n_folds: Number of test folds.
        expanding: If True, training window grows. If False, fixed-size sliding window.
    """
    fold_size = len(data) // (n_folds + 1)
    if fold_size < config.sequence_length + 10:
        raise ValueError(
            f"Fold size ({fold_size}) too small for "
            f"sequence_length ({config.sequence_length})"
        )

    fold_results: List[dict] = []

    for fold_idx in range(n_folds):
        train_end = fold_size * (fold_idx + 1)
        test_end = min(train_end + fold_size, len(data))

        if expanding:
            train_data = data[:train_end]
        else:
            train_start = max(0, train_end - fold_size * 2)
            train_data = data[train_start:train_end]

        test_data = data[train_end:test_end]

        if len(test_data) < config.sequence_length + 1:
            logger.warning(f"[ML] Fold {fold_idx}: insufficient test data, skipping")
            continue

        # Scale separately per fold to avoid look-ahead bias
        scaler = PriceScaler(scaler_type=config.scaler_type)
        scaled_train = scaler.fit_transform(train_data)
        scaled_test = scaler.transform(test_data)

        X_train, y_train = create_sequences(scaled_train, config.sequence_length)
        X_test, y_test = create_sequences(scaled_test, config.sequence_length)

        input_shape = (X_train.shape[1], X_train.shape[2])
        model = build_lstm_model(config, input_shape)

        model.fit(
            X_train,
            y_train,
            epochs=config.epochs,
            batch_size=config.batch_size,
            verbose=0,
        )

        predictions = model.predict(X_test, verbose=0).flatten()

        # Compute trading-relevant metrics
        trading_returns = calculate_trading_returns(y_test, predictions)
        equity_curve = (
            np.cumsum(trading_returns) + 1.0
            if len(trading_returns) > 0
            else np.array([1.0])
        )

        fold_result = {
            "fold": fold_idx,
            "train_size": len(X_train),
            "test_size": len(X_test),
            "rmse": calculate_rmse(y_test, predictions),
            "mae": calculate_mae(y_test, predictions),
            "mape": calculate_mape(y_test, predictions),
            "directional_accuracy": calculate_directional_accuracy(
                y_test, predictions
            ),
            "sharpe_ratio": calculate_sharpe_ratio(trading_returns),
            "max_drawdown": calculate_max_drawdown(equity_curve),
            "profit_factor": calculate_profit_factor(trading_returns),
        }
        fold_results.append(fold_result)

        logger.info(
            f"[ML] Walk-forward fold {fold_idx}: "
            f"RMSE={fold_result['rmse']:.6f}, "
            f"Sharpe={fold_result['sharpe_ratio']:.2f}, "
            f"MaxDD={fold_result['max_drawdown']:.4f}"
        )

    def avg(key: str) -> float:
        values = [f[key] for f in fold_results if np.isfinite(f[key])]
        return float(np.mean(values)) if values else 0.0

    return WalkForwardResult(
        ticker=config.ticker,
        num_folds=len(fold_results),
        avg_rmse=round(avg("rmse"), 6),
        avg_mae=round(avg("mae"), 6),
        avg_mape=round(avg("mape"), 2),
        avg_directional_accuracy=round(avg("directional_accuracy"), 2),
        avg_sharpe_ratio=round(avg("sharpe_ratio"), 4),
        avg_max_drawdown=round(avg("max_drawdown"), 4),
        avg_profit_factor=round(avg("profit_factor"), 4),
        fold_results=fold_results,
    )
