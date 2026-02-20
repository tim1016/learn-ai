from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.ml.models.schemas import TrainingConfig
from app.ml.preprocessing.scaler import PriceScaler
from app.ml.preprocessing.stationarity import StationarityResult, run_stationarity_tests
from app.ml.preprocessing.windowing import create_sequences
from app.ml.protocols import MarketDataProvider

logger = logging.getLogger(__name__)


class DataPipeline:
    """Orchestrates: fetch -> feature engineering -> split -> scale -> window.

    Key fixes from the original pipeline:
      1. Scaler is fit on TRAINING data only (no look-ahead leakage).
      2. Features are shifted by 1 to prevent same-day look-ahead bias.
      3. Log returns can replace raw prices for stationarity.
      4. Optional winsorization clips extreme values.
      5. Stationarity tests run on the target column before training.
    """

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider
        self.scaler: PriceScaler | None = None

    def prepare(
        self, config: TrainingConfig
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, PriceScaler, StationarityResult | None]:
        """Full pipeline: returns (X_train, X_test, y_train, y_test, fitted_scaler, stationarity)."""

        # Step 1: Fetch raw data
        raw = self._provider.fetch_ohlcv(
            ticker=config.ticker,
            from_date=config.from_date,
            to_date=config.to_date,
            timespan=config.timespan,
            multiplier=config.multiplier,
        )
        logger.info(f"[ML] Pipeline: fetched {len(raw)} bars for {config.ticker}")

        # Step 2: Convert to DataFrame, sort by timestamp
        df = pd.DataFrame(raw)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Step 3: Compute derived features
        if "returns" in config.features:
            df["returns"] = df["close"].pct_change().fillna(0)

        if "log_return" in config.features or config.log_returns:
            df["log_return"] = np.log(df["close"] / df["close"].shift(1))

        # Step 4: Feature shifting — use t-1 values to predict t
        # This prevents look-ahead bias (e.g., using today's VWAP to predict today's close)
        feature_cols = list(config.features)
        df[feature_cols] = df[feature_cols].shift(1)
        df = df.dropna(subset=feature_cols).reset_index(drop=True)

        logger.info(f"[ML] Pipeline: {len(df)} bars after feature shifting and dropna")

        if len(df) < config.sequence_length + 20:
            raise ValueError(
                f"Insufficient data: got {len(df)} bars after preprocessing, need at least "
                f"{config.sequence_length + 20} for sequence_length={config.sequence_length}"
            )

        # Step 5: Extract feature array
        data = df[feature_cols].values.astype(np.float64)

        # Step 6: Temporal split of raw data (before scaling to prevent leakage)
        split_idx = int(len(data) * config.train_split)
        train_data = data[:split_idx]
        # Include overlap for test lookback window so test sequences can look back
        test_overlap_start = max(0, split_idx - config.sequence_length)
        test_data_with_overlap = data[test_overlap_start:]

        # Step 7: Stationarity tests on the unscaled training target column
        stationarity: StationarityResult | None = None
        try:
            stationarity = run_stationarity_tests(train_data[:, 0])
        except Exception as e:
            logger.warning(f"[ML] Pipeline: stationarity tests failed: {e}")

        # Step 8: Winsorize training data if enabled (compute bounds from training only)
        if config.winsorize:
            lower_q, upper_q = config.winsorize_limits
            lower_bounds = np.quantile(train_data, lower_q, axis=0)
            upper_bounds = np.quantile(train_data, upper_q, axis=0)
            train_data = np.clip(train_data, lower_bounds, upper_bounds)
            test_data_with_overlap = np.clip(test_data_with_overlap, lower_bounds, upper_bounds)
            logger.info("[ML] Pipeline: winsorization applied")

        # Step 9: Scale — fit on training data only (no leakage)
        self.scaler = PriceScaler(scaler_type=config.scaler_type)
        scaled_train = self.scaler.fit_transform(train_data)
        scaled_test_overlap = self.scaler.transform(test_data_with_overlap)

        logger.info(
            f"[ML] Pipeline: scaled with {config.scaler_type} scaler (fit on training only)"
        )

        # Step 10: Create sequences from scaled data
        X_train, y_train = create_sequences(
            scaled_train, config.sequence_length, target_col_index=0
        )

        X_test, y_test = create_sequences(
            scaled_test_overlap, config.sequence_length, target_col_index=0
        )
        # Remove any test sequences whose targets overlap with training data
        # The overlap region produces sequences with targets still in the training window
        overlap_len = split_idx - test_overlap_start
        # Number of sequences from the overlap that have targets in training period
        n_overlap_sequences = max(0, overlap_len - config.sequence_length)
        if n_overlap_sequences > 0:
            X_test = X_test[n_overlap_sequences:]
            y_test = y_test[n_overlap_sequences:]

        logger.info(
            f"[ML] Pipeline: train={X_train.shape[0]} samples, "
            f"test={X_test.shape[0]} samples"
        )

        return X_train, X_test, y_train, y_test, self.scaler, stationarity
