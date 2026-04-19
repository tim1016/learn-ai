"""Data-divergence research module.

Quantifies per-bar and per-trade differences between TradingView-sourced
and Polygon-sourced indicator values, per the research plan at
``Downloads/Research_Plan_TV_vs_Polygon_Divergence.md``.

Public entry points:
  * ``ingest.tv_ingest`` — parse TradingView Pine-script CSV dumps.
  * ``ingest.polygon_ingest`` — fetch (or resample) Polygon aggregates.
  * ``ingest.align`` — time-align the two sources for downstream analysis.
"""
