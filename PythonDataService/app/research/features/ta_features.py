"""Phase 1 feature computation using pandas-ta (no TA-Lib C dependency)."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.research.features.registry import FeatureName

logger = logging.getLogger(__name__)


class TechnicalFeatures:
    """Compute research features from OHLCV bars."""

    @staticmethod
    def compute_momentum_5m(df: pd.DataFrame) -> pd.Series:
        """5-bar price momentum: (close_t - close_{t-5}) / close_{t-5}."""
        return df["close"].pct_change(periods=5)

    @staticmethod
    def compute_rsi_14(df: pd.DataFrame) -> pd.Series:
        """14-period RSI via pandas-ta."""
        result = ta.rsi(df["close"], length=14)
        return result if result is not None else pd.Series(np.nan, index=df.index)

    @staticmethod
    def compute_realized_vol_30(df: pd.DataFrame) -> pd.Series:
        """30-bar realized volatility: rolling std of log returns."""
        log_returns = np.log(df["close"] / df["close"].shift(1))
        return log_returns.rolling(window=30).std()

    @staticmethod
    def compute_volume_zscore(df: pd.DataFrame) -> pd.Series:
        """Volume z-score: (volume - rolling_mean) / rolling_std over 30 bars."""
        vol_mean = df["volume"].rolling(window=30).mean()
        vol_std = df["volume"].rolling(window=30).std()
        return (df["volume"] - vol_mean) / vol_std.replace(0, np.nan)

    @staticmethod
    def compute_macd_signal(df: pd.DataFrame) -> pd.Series:
        """MACD signal line via pandas-ta (EMA-9 of MACD)."""
        macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd_df is None or macd_df.empty:
            return pd.Series(np.nan, index=df.index)
        # Signal line is the third column (MACDs_12_26_9)
        return macd_df.iloc[:, 2]

    @staticmethod
    def compute_feature(feature_name: str, bars: list[dict]) -> pd.Series:
        """Dispatch to the appropriate feature computation.

        Parameters
        ----------
        feature_name : str
            One of the FeatureName enum values.
        bars : list[dict]
            OHLCV bars with timestamp, open, high, low, close, volume.

        Returns
        -------
        pd.Series
            Computed feature values aligned with bar indices.

        Raises
        ------
        ValueError
            If the feature name is not registered.
        """
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

        dispatch = {
            FeatureName.MOMENTUM_5M.value: TechnicalFeatures.compute_momentum_5m,
            FeatureName.RSI_14.value: TechnicalFeatures.compute_rsi_14,
            FeatureName.REALIZED_VOL_30.value: TechnicalFeatures.compute_realized_vol_30,
            FeatureName.VOLUME_ZSCORE.value: TechnicalFeatures.compute_volume_zscore,
            FeatureName.MACD_SIGNAL.value: TechnicalFeatures.compute_macd_signal,
        }

        compute_fn = dispatch.get(feature_name)
        if compute_fn is None:
            raise ValueError(f"Unknown feature: {feature_name}")

        result = compute_fn(df)
        logger.info(
            "[Research] Computed %s: %d valid / %d total",
            feature_name,
            result.notna().sum(),
            len(result),
        )
        return result
