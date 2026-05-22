"""Fetch 24-month minute bars for SPY, QQQ, AAPL, TSLA via ensure_data.

Invokes ensure_data() directly in-process (bypasses the HTTP routing gate
DATA_LAKE_ENABLED) so the data lands in /lean-data-writer/lake/.

Window: 2024-06-03 → 2026-04-30 (covers W6mo, W12mo, W24mo for all 12 cells).

Usage (inside polygon-data-service container):
    python /app/scripts/capture_24mo_minute_bars.py

Output:
    /lean-data-writer/lake/equity/usa/minute/<ticker>/YYYYMMDD_trade.zip
    per-ticker counts + status printed to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("capture_24mo")

# Ensure /app is on sys.path when running directly inside the container.
import sys as _sys  # noqa: E402

if "/app" not in _sys.path:
    _sys.path.insert(0, "/app")

TICKERS = ["SPY", "QQQ", "AAPL", "TSLA"]
START_DATE = date(2024, 6, 3)
END_DATE = date(2026, 4, 30)
# Pinned LEAN image digest (from app/lean_sidecar/config.py).
LEAN_IMAGE_DIGEST = "sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c"
# Per-ticker timeout: 24 months * ~20 days * 390 bars each = ~187 000 bars.
# Polygon Aggregates paginates at 50 000 results; expect ~4 round trips + overhead.
# 1800 s (30 min) per ticker is generous.
FETCH_TIMEOUT_SECONDS = 1800


async def run_ticker(ticker: str) -> dict:
    from app.data_lake.ensure_data import ensure_data
    from app.data_lake.types import DataRunSpec

    request_id = uuid.uuid4()
    spec = DataRunSpec(
        request_id=request_id,
        run_type="python_lab",
        requester="capture_24mo_minute_bars.py",
        market="usa",
        symbols=[ticker],
        start_trading_date=START_DATE,
        end_trading_date=END_DATE,
        resolution="minute",
        data_types=["trade"],
        price_adjustment_mode="raw",
        provider="polygon",
        include_factor_files=True,
        include_map_files=True,
        lean_image_digest=LEAN_IMAGE_DIGEST,
        force_refresh=False,
        fetch_timeout_seconds=FETCH_TIMEOUT_SECONDS,
    )

    logger.info("=== [%s] starting ensure_data request_id=%s ===", ticker, request_id)
    result = await ensure_data(spec)
    logger.info(
        "=== [%s] DONE: status=%s fetched=%d reused=%d failures=%d duration_ms=%d ===",
        ticker,
        result.overall_status,
        result.fetched_artifact_count,
        result.reused_artifact_count,
        len(result.failures),
        result.duration_ms,
    )
    if result.failures:
        for f in result.failures:
            logger.warning(
                "[%s] FAILURE: kind=%s sym=%s date=%s reason=%s detail=%s",
                ticker,
                f.artifact_kind,
                f.symbol,
                f.trading_date,
                f.reason,
                f.detail,
            )

    return {
        "ticker": ticker,
        "request_id": str(request_id),
        "overall_status": result.overall_status,
        "fetched_artifact_count": result.fetched_artifact_count,
        "reused_artifact_count": result.reused_artifact_count,
        "failure_count": len(result.failures),
        "duration_ms": result.duration_ms,
        "lean_data_root_path": result.lean_data_root_path,
        "data_availability_hash": result.data_availability_hash,
        "artifacts": [
            {
                "kind": a.artifact_kind,
                "symbol": a.symbol,
                "trading_date": a.trading_date.isoformat() if a.trading_date else None,
                "file_path": a.file_path,
            }
            for a in result.artifacts
            if a.artifact_kind == "time_series_bars"
        ][:5],  # first 5 bar artifacts for verification
        "failures": [
            {
                "kind": f.artifact_kind,
                "symbol": f.symbol,
                "trading_date": f.trading_date.isoformat() if f.trading_date else None,
                "reason": f.reason,
                "detail": f.detail,
            }
            for f in result.failures
        ],
    }


async def main() -> None:
    # Init the catalog DB pool once before the first ticker.
    from app.data_lake import catalog_client

    await catalog_client.init_pool()
    logger.info("Catalog DB pool initialized.")

    summaries = []
    for ticker in TICKERS:
        try:
            summary = await run_ticker(ticker)
            summaries.append(summary)
        except Exception:
            logger.exception("FATAL: ensure_data raised for ticker=%s", ticker)
            summaries.append({"ticker": ticker, "overall_status": "EXCEPTION"})
        # Brief pause between tickers to respect rate limits.
        await asyncio.sleep(2)

    logger.info("=== CAPTURE SUMMARY ===\n%s", json.dumps(summaries, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
