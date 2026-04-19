"""Indicator implementations for the divergence study.

Three surfaces to compare against TradingView's ground truth:

    native   — pandas-only reference implementations. Canonical formulas.
    engine   — adapter around learn-ai's streaming Indicator base class.
    service  — adapter around TechnicalAnalysisService (pandas_ta-backed).
"""

from app.research.divergence.indicators.engine_adapter import (
    compute_engine_ema_batch,
    compute_engine_rsi_batch,
    compute_engine_sma_batch,
)
from app.research.divergence.indicators.native import (
    adx_system,
    atr_wilder,
    bollinger,
    compute_all_native,
    ema,
    macd,
    rsi_wilder,
    sma,
    supertrend,
)

__all__ = [
    "adx_system",
    "atr_wilder",
    "bollinger",
    "compute_all_native",
    "compute_engine_ema_batch",
    "compute_engine_rsi_batch",
    "compute_engine_sma_batch",
    "ema",
    "macd",
    "rsi_wilder",
    "sma",
    "supertrend",
]
