"""Ingest submodule: CSV/API → parquet."""

from app.research.divergence.ingest.align import align_tv_polygon
from app.research.divergence.ingest.dividend_adjuster import (
    SPY_DIVIDENDS,
    DividendEvent,
    apply_dividend_adjustment,
    detect_dividends_from_gap,
    dividends_from_polygon_payload,
    reverse_dividend_adjustment,
)
from app.research.divergence.ingest.polygon_ingest import (
    ingest_polygon_1min_csv_resampled,
    resample_ohlcv,
)
from app.research.divergence.ingest.tv_ingest import ingest_tv_csv

__all__ = [
    "SPY_DIVIDENDS",
    "DividendEvent",
    "align_tv_polygon",
    "apply_dividend_adjustment",
    "detect_dividends_from_gap",
    "dividends_from_polygon_payload",
    "ingest_polygon_1min_csv_resampled",
    "ingest_tv_csv",
    "resample_ohlcv",
    "reverse_dividend_adjustment",
]
