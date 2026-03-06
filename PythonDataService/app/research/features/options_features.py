from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MIN_VOLUME_SKEW = 50
MIN_OI_SKEW = 100


class OptionsFeatures:
    """Compute options-derived features from IV time series."""

    @staticmethod
    def compute_iv_30d(iv_data: pd.DataFrame) -> pd.Series:
        """Raw 30-day constant-maturity ATM IV. Direct output from IV builder."""
        return iv_data["iv_30d_atm"].astype(float)

    @staticmethod
    def compute_iv_rank(iv_data: pd.DataFrame, window: int = 60, min_periods: int = 30) -> pd.Series:
        """IV Rank: (IV - min) / (max - min) over rolling window.

        Args:
            iv_data: DataFrame with 'iv_30d_atm' column
            window: Lookback window in trading days
            min_periods: Minimum periods before computing rank
        """
        iv = iv_data["iv_30d_atm"].astype(float)
        rolling_min = iv.rolling(window=window, min_periods=min_periods).min()
        rolling_max = iv.rolling(window=window, min_periods=min_periods).max()

        denom = rolling_max - rolling_min
        # Avoid division by zero when IV is constant
        rank = np.where(denom > 1e-10, (iv - rolling_min) / denom, 0.5)

        return pd.Series(rank, index=iv.index, name=f"iv_rank_{window}")

    @staticmethod
    def compute_log_skew(iv_data: pd.DataFrame) -> pd.Series:
        """Log put-call skew: ln(IV_put / IV_call).

        Scale-invariant measure of demand asymmetry.
        Positive = elevated put demand (bearish hedging).
        """
        iv_put = iv_data["iv_30d_put"].astype(float)
        iv_call = iv_data["iv_30d_call"].astype(float)

        # Only compute where both are valid and positive
        valid = (iv_put > 0) & (iv_call > 0) & iv_put.notna() & iv_call.notna()

        skew = pd.Series(np.nan, index=iv_data.index, name="log_skew")
        skew[valid] = np.log(iv_put[valid] / iv_call[valid])

        return skew

    @staticmethod
    def compute_vrp(
        iv_data: pd.DataFrame,
        stock_data: pd.DataFrame,
        mode: str = "signal",
    ) -> pd.Series:
        """Volatility Risk Premium: IV - RV.

        Args:
            iv_data: DataFrame with 'iv_30d_atm'
            stock_data: DataFrame with 'close' column (daily)
            mode: "signal" (trailing RV, no lookahead) or "research" (forward RV)
        """
        iv = iv_data["iv_30d_atm"].astype(float)

        close = stock_data["close"].astype(float)
        log_returns = np.log(close / close.shift(1))

        if mode == "research":
            # Forward-looking RV (only for research, NOT signal generation)
            rv = log_returns.shift(-5).rolling(window=5).std() * math.sqrt(252)
            vrp = iv - rv
            vrp.name = "vrp_5_forward"
        else:
            # Trailing RV (safe for signal mode — no lookahead)
            rv = log_returns.rolling(window=5).std() * math.sqrt(252)
            vrp = iv - rv
            vrp.name = "vrp_5"

        return vrp

    @staticmethod
    def compute_feature(
        feature_name: str,
        iv_data: pd.DataFrame,
        stock_data: pd.DataFrame | None = None,
        mode: str = "signal",
    ) -> pd.Series:
        """Dispatch to the appropriate options feature computation.

        Args:
            feature_name: Feature name from registry
            iv_data: DataFrame with IV columns
            stock_data: DataFrame with daily stock OHLCV (needed for VRP)
            mode: "signal" or "research" (affects VRP direction)
        """
        if feature_name == "iv_30d":
            return OptionsFeatures.compute_iv_30d(iv_data)
        elif feature_name == "iv_rank_60":
            return OptionsFeatures.compute_iv_rank(iv_data, window=60, min_periods=30)
        elif feature_name == "iv_rank_252":
            return OptionsFeatures.compute_iv_rank(iv_data, window=252, min_periods=60)
        elif feature_name == "log_skew":
            return OptionsFeatures.compute_log_skew(iv_data)
        elif feature_name == "vrp_5":
            if stock_data is None:
                raise ValueError("VRP requires stock_data (daily OHLCV)")
            if mode == "research":
                raise ValueError(
                    "vrp_5 uses trailing RV only. Use vrp_5_forward for research mode "
                    "with forward-looking RV."
                )
            return OptionsFeatures.compute_vrp(iv_data, stock_data, mode="signal")
        elif feature_name == "vrp_5_forward":
            if stock_data is None:
                raise ValueError("VRP requires stock_data (daily OHLCV)")
            if mode == "signal":
                raise ValueError(
                    "vrp_5_forward uses forward-looking RV and must NOT be used in signal mode. "
                    "Use vrp_5 instead."
                )
            return OptionsFeatures.compute_vrp(iv_data, stock_data, mode="research")
        else:
            raise ValueError(f"Unknown options feature: {feature_name}")
