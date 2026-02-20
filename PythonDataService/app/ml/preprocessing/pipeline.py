from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.ml.models.schemas import TrainingConfig
from app.ml.preprocessing.scaler import PriceScaler
from app.ml.preprocessing.windowing import create_sequences, train_test_split_temporal
from app.ml.protocols import MarketDataProvider

logger = logging.getLogger(__name__)


class DataPipeline:
    """Orchestrates: fetch -> DataFrame -> feature selection -> scale -> window -> split."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider
        self.scaler = PriceScaler()

    def prepare(
        self, config: TrainingConfig
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, PriceScaler]:
        """Full pipeline: returns (X_train, X_test, y_train, y_test, fitted_scaler)."""

        # Step 1: Fetch raw data
        raw = self._provider.fetch_ohlcv(
            ticker=config.ticker,
            from_date=config.from_date,
            to_date=config.to_date,
        )
        logger.info(f"[ML] Pipeline: fetched {len(raw)} bars for {config.ticker}")

        if len(raw) < config.sequence_length + 20:
            raise ValueError(
                f"Insufficient data: got {len(raw)} bars, need at least "
                f"{config.sequence_length + 20} for sequence_length={config.sequence_length}"
            )

        # Step 2: Convert to DataFrame, sort, select features
        df = pd.DataFrame(raw)
        df = df.sort_values("timestamp").reset_index(drop=True)

        if "returns" in config.features:
            df["returns"] = df["close"].pct_change().fillna(0)

        feature_cols = config.features
        data = df[feature_cols].values.astype(np.float64)

        # Step 3: Scale
        scaled = self.scaler.fit_transform(data)
        logger.info(f"[ML] Pipeline: scaled {data.shape} to range [0,1]")

        # Step 4: Create sequences (target is always the first feature column)
        X, y = create_sequences(scaled, config.sequence_length, target_col_index=0)

        # Step 5: Temporal train/test split
        X_train, X_test, y_train, y_test = train_test_split_temporal(
            X, y, config.train_split
        )

        logger.info(
            f"[ML] Pipeline: train={X_train.shape[0]} samples, "
            f"test={X_test.shape[0]} samples"
        )

        return X_train, X_test, y_train, y_test, self.scaler
