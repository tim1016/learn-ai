"""Measure how soon after a minute closes IBKR 1-minute history serves its final row (#2410).

For each of ``--minutes`` consecutive minutes, polls ``reqHistoricalData``
(1 min TRADES, ``useRTH=False``) about once a second from the minute's close
for 50 s and records, per symbol, when the closed minute's row first appeared,
when it first held the value it still held 50 s later, and the slowest
request. One JSON line per (symbol, minute), written to stdout.

Run inside the data-plane container, where the IB Gateway is reachable:

    podman cp PythonDataService/scripts/probe_ibkr_history_settle.py \\
        polygon-data-service:/tmp/probe.py
    podman exec polygon-data-service python /tmp/probe.py --symbols SPY,TSLA --minutes 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from ib_async import IB, Stock


async def _probe(host: str, port: int, client_id: int, symbols: list[str], minutes: int) -> None:
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, readonly=True, timeout=15)
    try:
        for _ in range(minutes):
            now = time.time()
            boundary = (int(now // 60) + 1) * 60
            await asyncio.sleep(boundary - now)
            target = boundary - 60
            polls: dict[str, list[tuple[float, float, tuple[object, ...] | None]]] = {s: [] for s in symbols}
            while time.time() < boundary + 50:
                for symbol in symbols:
                    started = time.time()
                    bars = await ib.reqHistoricalDataAsync(
                        Stock(symbol, "SMART", "USD"),
                        endDateTime="",
                        durationStr="180 S",
                        barSizeSetting="1 min",
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=2,
                    )
                    finished = time.time()
                    row = next((bar for bar in bars if int(bar.date.timestamp()) == target), None)
                    values = None if row is None else (row.open, row.high, row.low, row.close, float(row.volume))
                    polls[symbol].append((round(started - boundary, 2), round(finished - started, 2), values))
                await asyncio.sleep(1)
            for symbol in symbols:
                final = polls[symbol][-1][2]
                record = {
                    "sym": symbol,
                    "minute_start": target,
                    "first_row_s": next((p[0] for p in polls[symbol] if p[2] is not None), None),
                    "first_final_s": next((p[0] for p in polls[symbol] if p[2] == final), None),
                    "max_req_s": max(p[1] for p in polls[symbol]),
                    "final": final,
                    "changed_after_close": len({p[2] for p in polls[symbol] if p[2] is not None}) > 1,
                }
                sys.stdout.write(json.dumps(record) + "\n")
                sys.stdout.flush()
    finally:
        ib.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", default="SPY,TSLA")
    parser.add_argument("--minutes", type=int, default=8)
    parser.add_argument("--host", default="host.containers.internal")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=93)
    args = parser.parse_args()
    asyncio.run(_probe(args.host, args.port, args.client_id, args.symbols.split(","), args.minutes))


if __name__ == "__main__":
    main()
