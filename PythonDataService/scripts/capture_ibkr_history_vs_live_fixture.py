"""Capture the #2314 receipt: one day's live-assembled minutes beside IBKR 1-minute history.

Reads a clerk's ``source_bars.sqlite3`` (read-only) for the day's
``provenance=realtime`` minutes, fetches the same window from IBKR
``reqHistoricalData`` (1 min TRADES, ``useRTH=False``) and the RTH minute
completeness of a few thin symbols, and writes the three fixture files that
``tests/services/test_ibkr_history_equivalence_fixture.py`` verifies.

Run inside a container that reaches IB Gateway, e.g.::

    python -m scripts.capture_ibkr_history_vs_live_fixture \\
        --ledger /app/artifacts/alpaca_clerk/accounts/alpaca/paper:<sid>/source_bars.sqlite3 \\
        --date 2026-09-24 --out /tmp/fixture
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

from ib_async import IB, Stock

from app.lean_sidecar.trading_calendar import session_window_for_date

logger = logging.getLogger(__name__)

_THIN_SYMBOLS = ("SPY", "IWM", "EWN", "FLCH", "KBWP")


def _live_minutes(ledger: Path, day: date) -> list[dict[str, object]]:
    window = session_window_for_date(day)
    day_start, day_end = window.open_ms_utc - 6 * 3_600_000, window.close_ms_utc + 5 * 3_600_000
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT start_ms, end_ms, open, high, low, close, volume, session_phase FROM source_bars "
            "WHERE provenance = 'realtime' AND start_ms >= ? AND start_ms < ? ORDER BY start_ms",
            (day_start, day_end),
        ).fetchall()
    finally:
        conn.close()
    keys = ("start_ms", "end_ms", "open", "high", "low", "close", "volume", "session_phase")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def _capture(host: str, port: int, client_id: int, day: date, live: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, dict[str, int]]]:
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=15)
    try:
        end = f"{(day.isoformat()).replace('-', '')} 23:59:00 UTC"
        spy = Stock("SPY", "SMART", "USD")
        await ib.qualifyContractsAsync(spy)
        bars = await ib.reqHistoricalDataAsync(
            spy, endDateTime=end, durationStr="1 D", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=False, formatDate=2,
        )
        low, high = live[0]["start_ms"], live[-1]["start_ms"]
        history = [
            {"start_ms": ms, "open": str(b.open), "high": str(b.high), "low": str(b.low),
             "close": str(b.close), "volume": int(b.volume)}
            for b in bars
            if low <= (ms := int(b.date.timestamp() * 1000)) <= high  # type: ignore[operator]
        ]
        thin: dict[str, dict[str, int]] = {}
        for symbol in _THIN_SYMBOLS:
            contract = Stock(symbol, "SMART", "USD")
            await ib.qualifyContractsAsync(contract)
            rth = await ib.reqHistoricalDataAsync(
                contract, endDateTime=end, durationStr="1 D", barSizeSetting="1 min",
                whatToShow="TRADES", useRTH=True, formatDate=2,
            )
            thin[symbol] = {"rth_minutes": len(rth), "zero_volume": sum(1 for b in rth if b.volume == 0)}
            await asyncio.sleep(3)  # stay clear of IBKR's historical-data pacing
        return history, thin
    finally:
        ib.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--host", default="host.containers.internal")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=91)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    live = _live_minutes(args.ledger, args.date)
    if not live:
        raise SystemExit(f"{args.ledger} holds no realtime minutes on {args.date}")
    history, thin = asyncio.run(_capture(args.host, args.port, args.client_id, args.date, live))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "live_minutes.json").write_text(json.dumps({"symbol": "SPY", "bars": live}, indent=0))
    (args.out / "history_minutes.json").write_text(json.dumps({"symbol": "SPY", "bars": history}, indent=0))
    (args.out / "thin_symbol_completeness.json").write_text(json.dumps({"symbols": thin}, indent=1))
    logger.info("fixture written", extra={"out": str(args.out), "live": len(live), "history": len(history)})


if __name__ == "__main__":
    main()
