from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def create_sequences(
    data: np.ndarray,
    sequence_length: int,
    target_col_index: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Create sliding window sequences for LSTM.

    Args:
        data: Scaled data array of shape (n_samples, n_features).
        sequence_length: Number of time steps to look back.
        target_col_index: Index of the target column in the feature array.

    Returns:
        X: shape (n_sequences, sequence_length, n_features)
        y: shape (n_sequences,) -- next-step target value
    """
    if len(data) <= sequence_length:
        raise ValueError(
            f"Data length ({len(data)}) must exceed sequence_length ({sequence_length})"
        )

    X, y = [], []
    for i in range(sequence_length, len(data)):
        X.append(data[i - sequence_length : i])
        y.append(data[i, target_col_index])

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.float32)

    logger.info(
        f"[ML] Created {len(X_arr)} sequences: X={X_arr.shape}, y={y_arr.shape}"
    )
    return X_arr, y_arr


def train_test_split_temporal(
    X: np.ndarray,
    y: np.ndarray,
    train_ratio: float = 0.8,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split time series data preserving temporal order (no shuffle)."""
    split_idx = int(len(X) * train_ratio)
    return X[:split_idx], X[split_idx:], y[:split_idx], y[split_idx:]
