from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import numpy as np
from sklearn.preprocessing import MinMaxScaler, RobustScaler, StandardScaler

logger = logging.getLogger(__name__)

ScalerType = Literal["minmax", "standard", "robust"]


class PriceScaler:
    """Wraps sklearn scalers with JSON save/load for reproducibility.

    Supports MinMaxScaler, StandardScaler (z-score), and RobustScaler (median/IQR).
    """

    def __init__(
        self,
        scaler_type: ScalerType = "standard",
        feature_range: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        self._scaler_type = scaler_type
        self._feature_range = feature_range
        self._scaler = self._create_scaler()
        self._is_fitted = False

    def _create_scaler(self) -> MinMaxScaler | StandardScaler | RobustScaler:
        if self._scaler_type == "minmax":
            return MinMaxScaler(feature_range=self._feature_range)
        elif self._scaler_type == "standard":
            return StandardScaler()
        elif self._scaler_type == "robust":
            return RobustScaler()
        else:
            raise ValueError(f"Unknown scaler type: {self._scaler_type}")

    @property
    def scaler_type(self) -> ScalerType:
        return self._scaler_type

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
        params: dict = {"scaler_type": self._scaler_type}

        if self._scaler_type == "minmax":
            params["data_min"] = self._scaler.data_min_.tolist()
            params["data_max"] = self._scaler.data_max_.tolist()
            params["feature_range"] = list(self._scaler.feature_range)
        elif self._scaler_type == "standard":
            params["mean"] = self._scaler.mean_.tolist()
            params["scale"] = self._scaler.scale_.tolist()
            params["var"] = self._scaler.var_.tolist()
        elif self._scaler_type == "robust":
            params["center"] = self._scaler.center_.tolist()
            params["scale"] = self._scaler.scale_.tolist()

        params["n_features"] = int(self._scaler.n_features_in_)
        path.write_text(json.dumps(params, indent=2))
        logger.info(f"[ML] Scaler ({self._scaler_type}) saved to {path}")

    def load(self, path: Path) -> None:
        """Load scaler parameters from JSON."""
        params = json.loads(path.read_text())
        self._scaler_type = params["scaler_type"]
        self._scaler = self._create_scaler()
        n_features = params["n_features"]

        if self._scaler_type == "minmax":
            self._scaler.data_min_ = np.array(params["data_min"])
            self._scaler.data_max_ = np.array(params["data_max"])
            self._scaler.data_range_ = self._scaler.data_max_ - self._scaler.data_min_
            self._scaler.scale_ = (
                self._scaler.feature_range[1] - self._scaler.feature_range[0]
            ) / self._scaler.data_range_
            self._scaler.min_ = (
                self._scaler.feature_range[0]
                - self._scaler.data_min_ * self._scaler.scale_
            )
        elif self._scaler_type == "standard":
            self._scaler.mean_ = np.array(params["mean"])
            self._scaler.scale_ = np.array(params["scale"])
            self._scaler.var_ = np.array(params["var"])
        elif self._scaler_type == "robust":
            self._scaler.center_ = np.array(params["center"])
            self._scaler.scale_ = np.array(params["scale"])

        self._scaler.n_features_in_ = n_features
        self._is_fitted = True
        logger.info(f"[ML] Scaler ({self._scaler_type}) loaded from {path}")
