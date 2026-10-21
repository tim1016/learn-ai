"""Read-only account-cutoff probe; no lake or catalog writes.

Usage: python scripts/probe_option_archive_boundary.py SPY241025C00580000
  --today-session-ms <canonical session open>
Choose a ticker that existed at the requested anniversary sessions. The probe
logs served, outside_data_plan, and other provider reasons separately. A
weekday anniversary is needed to settle whether its date is inclusive.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta

from app.config import settings
from app.data_lake.option_lake import option_history_floor, vendor_reason
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_bars import fetch_option_minute_bars
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.option_archive.planner import archive_history_boundary
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract_symbol")
    parser.add_argument("--today-session-ms", type=int, required=True)
    args = parser.parse_args()
    today = et_date_at_ms(args.today_session_ms)
    boundary = archive_history_boundary(today)
    for day in expected_sessions(boundary - timedelta(days=5), option_history_floor(today) + timedelta(days=2)):
        try:
            bars = await fetch_option_minute_bars(args.contract_symbol, day, settings.POLYGON_API_KEY)
            logger.info("archive cutoff probe", extra={"session_ms": session_open_ms_utc(day), "outcome": "served", "bars": len(bars)})
        except PolygonFetchError as exc:
            logger.info("archive cutoff probe", extra={"session_ms": session_open_ms_utc(day), "outcome": vendor_reason(exc), "reason": str(exc)})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s %(session_ms)s %(outcome)s")
    asyncio.run(main())
