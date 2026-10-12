"""Record the vendor's real answers for the golden fixture ``options-backtest-spy-2025-06-10`` (#3030).

Fixture paperwork: run by hand, never by a test or by CI. It is the script that made ``manifest.json`` and the five bodies
beside it on 2026-10-12. Running it records a NEW fixture (the vendor's ``request_id`` differs on every call and its data
may have been revised), so the hand-worked figures in ``tests/options_backtest/test_golden_spy_2025_06_10.py`` must be
worked again from the new bytes before it is registered; see ``attribution.md``.

Read-only: every request is a ``GET``. The key is read from the environment, sent in an ``Authorization`` header, and never
printed, logged or written; it is in no URL, so it is in no recorded request. Each body is saved exactly as received, and
``manifest.json`` lists the request each file answers (method, path, query) with the sha256 and size of its bytes.

    POLYGON_API_KEY=<key> python regenerate.py <an empty folder to write into>

Stdout is the facts the run found (the at-the-money strikes, the contracts it recorded), as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://api.polygon.io"
TIMEOUT_S = 30.0
TRADING_DATE = date(2025, 6, 10)
EXPIRY = date(2025, 6, 13)
NEW_YORK = ZoneInfo("America/New_York")
# The decision is taken on the bar that closes at 09:35 New York, which the vendor stamps with its start, 09:34.
DECISION_BAR_START = datetime(2025, 6, 10, 9, 34, tzinfo=NEW_YORK)
MINUTE_BARS_QUERY: dict[str, object] = {"adjusted": "false", "sort": "asc", "limit": 50_000}


@dataclass
class Recorder:
    """Saves each answer's body byte for byte and keeps the manifest entry for it."""

    client: httpx.Client
    key: str
    out: Path

    def __post_init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def save(self, name: str, path: str, params: dict[str, object]) -> Any:
        response = self.client.get(f"{BASE_URL}{path}", params=params, headers={"Authorization": f"Bearer {self.key}"})
        body = response.content
        if self.key.encode() in body:
            raise RuntimeError(f"the answer to {path} contains the key; nothing was saved")
        if response.status_code != httpx.codes.OK:
            raise RuntimeError(f"{path} answered HTTP {response.status_code}; a fixture records only good answers")
        (self.out / name).write_bytes(body)
        self.entries.append(
            {
                "file": name,
                "method": "GET",
                "path": path,
                "query": {param: str(value) for param, value in params.items()},
                "http_status": response.status_code,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        return json.loads(body, parse_float=Decimal)


def nearest(strikes: list[Decimal], price: Decimal) -> Decimal:
    """The listed strike nearest the price, the lower of two as near: the rule the Options Backtest picks the money by."""
    return min(strikes, key=lambda strike: (abs(strike - price), strike))


def occ_ticker(right: str, strike: Decimal) -> str:
    return f"O:SPY{EXPIRY:%y%m%d}{right}{int(strike * 1000):08d}"


def main(out: Path) -> dict[str, Any]:
    key = os.environ["POLYGON_API_KEY"]
    day = TRADING_DATE.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=TIMEOUT_S) as client:
        recorder = Recorder(client, key, out)
        underlying = recorder.save(
            "spy_minute_2025-06-10.json", f"/v2/aggs/ticker/SPY/range/1/minute/{day}/{day}", MINUTE_BARS_QUERY
        )
        # The lake asks for the whole listing as of the date. It is recorded here with one expiry added, so that it is
        # a single page (316 contracts) and the fixture does not depend on the vendor's paging.
        listing = recorder.save(
            "spy_contracts_as_of_2025-06-10_expiring_2025-06-13.json",
            "/v3/reference/options/contracts",
            {"underlying_ticker": "SPY", "as_of": day, "expiration_date": EXPIRY.isoformat(), "limit": 1000},
        )
        decision_start_ms = int(DECISION_BAR_START.timestamp()) * 1000
        decision_bar = next(row for row in underlying["results"] if row["t"] == decision_start_ms)
        price = Decimal(str(decision_bar["c"]))
        calls = sorted({Decimal(str(row["strike_price"])) for row in listing["results"] if row["contract_type"] == "call"})
        puts = sorted({Decimal(str(row["strike_price"])) for row in listing["results"] if row["contract_type"] == "put"})
        atm_call, atm_put = nearest(calls, price), nearest(puts, price)
        wanted = {
            "atm_call": occ_ticker("C", atm_call),
            "atm_put": occ_ticker("P", atm_put),
            "highest_call": occ_ticker("C", calls[-1]),
        }
        for ticker in wanted.values():
            recorder.save(
                f"{ticker.replace(':', '_')}_minute_2025-06-10.json",
                f"/v2/aggs/ticker/{ticker}/range/1/minute/{day}/{day}",
                MINUTE_BARS_QUERY,
            )
    facts = {
        "trading_date": day,
        "expiry": EXPIRY.isoformat(),
        "underlying_close_at_0935_new_york": str(price),
        "underlying_bar_start_ms": decision_start_ms,
        "atm_call_strike": str(atm_call),
        "atm_put_strike": str(atm_put),
        "contracts": wanted,
        "listing_has_next_url": "next_url" in listing,
        "listing_rows": len(listing["results"]),
        "call_strikes": len(calls),
        "put_strikes": len(puts),
    }
    (out / "manifest.json").write_text(json.dumps({"requests": recorder.entries, "facts": facts}, indent=1) + "\n")
    return facts


if __name__ == "__main__":
    json.dump(main(Path(sys.argv[1])), sys.stdout, indent=1)
