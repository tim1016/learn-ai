"""Research feature names."""

from __future__ import annotations

from enum import StrEnum


class FeatureName(StrEnum):
    """Supported features."""

    MOMENTUM_5M = "momentum_5m"
    RSI_14 = "rsi_14"
    REALIZED_VOL_30 = "realized_vol_30"
    VOLUME_ZSCORE = "volume_zscore"
    MACD_SIGNAL = "macd_signal"

    # Options-derived features (daily frequency)
    IV_30D = "iv_30d"
    IV_RANK_60 = "iv_rank_60"
    LOG_SKEW = "log_skew"
    IV_RANK_252 = "iv_rank_252"
    VRP_5 = "vrp_5"
