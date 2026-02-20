from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from sklearn.preprocessing import MinMaxScaler

logger = logging.getLogger(__name__)


class PriceScaler:
    """Wraps sklearn MinMaxScaler with JSON save/load for reproducibility."""

    def __init__(self, feature_range: tuple[float, float] = (0.0, 1.0)) -> None:
        self._scaler = MinMaxScaler(feature_range=feature_range)
        self._is_fitted = False

    def fit_transform(self, data: np.ndarray) -> np.ndarray:
        """Fit and transform. data shape: (n_samples, n_features)."""
        result = self._scaler.fit_transform(data)
        self._is_fitted = True
        return result

    def transform(self, data: np.ndarray) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("Scaler not fitted. Call fit_transform first.")
        return self._scaler.transform(data)

    def inverse_transform(self, data: np.ndarray) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("Scaler not fitted. Call fit_transform first.")
        return self._scaler.inverse_transform(data)

    def save(self, path: Path) -> None:
        """Save scaler parameters to JSON for portability."""
        params = {
            "data_min": self._scaler.data_min_.tolist(),
            "data_max": self._scaler.data_max_.tolist(),
            "feature_range": list(self._scaler.feature_range),
        }
        path.write_text(json.dumps(params, indent=2))
        logger.info(f"[ML] Scaler saved to {path}")

    def load(self, path: Path) -> None:
        """Load scaler parameters from JSON."""
        params = json.loads(path.read_text())
        self._scaler.data_min_ = np.array(params["data_min"])
        self._scaler.data_max_ = np.array(params["data_max"])
        self._scaler.data_range_ = self._scaler.data_max_ - self._scaler.data_min_
        self._scaler.scale_ = (
            self._scaler.feature_range[1] - self._scaler.feature_range[0]
        ) / self._scaler.data_range_
        self._scaler.min_ = (
            self._scaler.feature_range[0] - self._scaler.data_min_ * self._scaler.scale_
        )
        self._scaler.n_features_in_ = len(params["data_min"])
        self._is_fitted = True
        logger.info(f"[ML] Scaler loaded from {path}")
