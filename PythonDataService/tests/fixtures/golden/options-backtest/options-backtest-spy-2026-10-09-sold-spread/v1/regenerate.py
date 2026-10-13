"""Record the vendor's real answers for the golden fixture ``options-backtest-spy-2026-10-09-sold-spread`` (#3110).

Fixture paperwork: run by hand, never by a test or by CI. It made ``manifest.json`` and the four bodies beside it, on
2026-10-13, and ``manifest.json`` lists the request each body answers (method, path, query) with the sha256 and the size
of its bytes. Running it again records a NEW fixture (the vendor's ``request_id`` differs on every call and its data may
have been revised), so the hand-worked figures in ``expected.json`` and in
``tests/options_backtest/test_golden_spy_2026_10_09_sold_spread.py`` must be worked again from the new bytes before it is
registered; see ``attribution.md``.

What it records: one SPY session, the underlying's minute bars, the listing of one expiry (the session's own), and two
calls of that expiry: the one at the money at the entry decision and the one two listed strikes above it. A call credit
spread sells the first and is covered by the second. It refuses to write anything unless both calls have a bar in the
minute the entry fills in and in the minute the exit fills in, which is the one thing a sold spread needs of a session
that the vendor's answers cannot be assumed to give.

Read-only: every request is a ``GET``. The key is read from the environment and stripped of surrounding whitespace, sent
in an ``Authorization`` header built once from the stripped key, and never printed, logged or written; it is in no URL, so
it is in no recorded request. No error this script raises holds the key: a failed request is reported by its path and its
status (or, with no answer, the kind of error), and the library's own error text, which can quote a header, is dropped.
Each body is saved exactly as received.

    POLYGON_API_KEY=<key> python regenerate.py <an empty folder to write into>

Stdout is the facts the run found (the at-the-money strike, the two contracts it recorded), as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://api.polygon.io"
TIMEOUT_S = 30.0
TRADING_DATE = date(2026, 10, 9)
# The expiry of the session itself: SPY lists an expiry on every weekday, and the setup's same-day rule picks this one.
EXPIRY = TRADING_DATE
NEW_YORK = ZoneInfo("America/New_York")
# The decision is taken on the bar that closes at 09:35 New York, which the vendor stamps with its start, 09:34.
DECISION_BAR_START = datetime(2026, 10, 9, 9, 34, tzinfo=NEW_YORK)
# The order decided there fills at the open of the next bar, stamped 09:35. The exit time is 15:30, 30 minutes before the
# close, and the orders it decides fill at the open of the bar stamped 15:30.
ENTRY_FILL_BAR_START = datetime(2026, 10, 9, 9, 35, tzinfo=NEW_YORK)
EXIT_FILL_BAR_START = datetime(2026, 10, 9, 15, 30, tzinfo=NEW_YORK)
# The covering call is this many listed strikes above the money.
COVER_STEPS_ABOVE_THE_MONEY = 2
MINUTE_BARS_QUERY: dict[str, object] = {"adjusted": "false", "sort": "asc", "limit": 50_000}
# What a key may hold to ride in a header: printable ASCII and no space. Anything else is refused before a request, because
# the HTTP library's error text for a bad header value quotes the value.
SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")


def key_from_environment() -> str:
    """``POLYGON_API_KEY`` without surrounding whitespace, or an error that says nothing of its value."""
    key = os.environ["POLYGON_API_KEY"].strip()
    if SENDABLE_KEY.fullmatch(key) is None:
        raise ValueError("POLYGON_API_KEY is empty, or holds a space or a character that is not printable ASCII")
    return key


@dataclass
class Recorder:
    """Asks the vendor and keeps each answer's body byte for byte, to be written together once every check has passed."""

    client: httpx.Client
    key: str = field(repr=False)

    def __post_init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.bodies: dict[str, bytes] = {}
        self.headers = {"Authorization": f"Bearer {self.key}"}

    def fetch(self, name: str, path: str, params: dict[str, object]) -> Any:
        try:
            response = self.client.get(f"{BASE_URL}{path}", params=params, headers=self.headers)
        except httpx.HTTPError as error:
            # The library's text can quote the header it refused; name the path and the kind of failure only.
            raise RuntimeError(f"the request for {path} failed ({type(error).__name__}); nothing was saved") from None
        body = response.content
        if self.key.encode() in body:
            raise RuntimeError(f"the answer to {path} contains the key; nothing was saved")
        if response.status_code != httpx.codes.OK:
            raise RuntimeError(f"{path} answered HTTP {response.status_code}; a fixture records only good answers")
        self.bodies[name] = body
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

    def write(self, out: Path) -> None:
        """Write every body as received, and the manifest of the requests that were made, beside them."""
        for name, body in self.bodies.items():
            (out / name).write_bytes(body)


def nearest(strikes: list[Decimal], price: Decimal) -> Decimal:
    """The listed strike nearest the price, the lower of two as near: the rule the Options Backtest picks the money by."""
    return min(strikes, key=lambda strike: (abs(strike - price), strike))


def occ_ticker(right: str, strike: Decimal) -> str:
    return f"O:SPY{EXPIRY:%y%m%d}{right}{int(strike * 1000):08d}"


def start_ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000


def require_a_bar_at(ticker: str, bars: list[dict[str, Any]], moment: datetime) -> None:
    """Refuse a contract with no bar stamped ``moment``: a sold spread cannot be filled in a minute nothing traded."""
    if not any(row["t"] == start_ms(moment) for row in bars):
        raise RuntimeError(f"{ticker} has no bar stamped {moment:%H:%M} New York; this session cannot be used")


def require_an_empty_folder(out: Path) -> None:
    """Refuse a folder that already holds anything, before a request is made.

    A recording is the files this script saves and nothing else, and it overwrites a file of the same name, so a folder
    with other files in it would mix a new recording into an old one or into someone's work. The folder may not exist
    yet; it is made.
    """
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} is not an empty folder. Name a folder that is empty or does not exist yet.")


def main(out: Path) -> dict[str, Any]:
    require_an_empty_folder(out)
    key = key_from_environment()
    day = TRADING_DATE.isoformat()
    with httpx.Client(timeout=TIMEOUT_S) as client:
        recorder = Recorder(client, key)
        underlying = recorder.fetch(
            f"spy_minute_{day}.json", f"/v2/aggs/ticker/SPY/range/1/minute/{day}/{day}", MINUTE_BARS_QUERY
        )
        # The lake asks for the whole listing as of the date. It is recorded here with one expiry added, so that it is
        # a single page and the fixture does not depend on the vendor's paging.
        listing = recorder.fetch(
            f"spy_contracts_as_of_{day}_expiring_{EXPIRY.isoformat()}.json",
            "/v3/reference/options/contracts",
            {"underlying_ticker": "SPY", "as_of": day, "expiration_date": EXPIRY.isoformat(), "limit": 1000},
        )
        if "next_url" in listing:
            raise RuntimeError("the listing has a next page; this fixture is built to hold one page")
        decision_bar = next(row for row in underlying["results"] if row["t"] == start_ms(DECISION_BAR_START))
        price = Decimal(str(decision_bar["c"]))
        calls = sorted(
            {Decimal(str(row["strike_price"])) for row in listing["results"] if row["contract_type"] == "call"}
        )
        puts = sorted(
            {Decimal(str(row["strike_price"])) for row in listing["results"] if row["contract_type"] == "put"}
        )
        money = nearest(calls, price)
        cover = calls[calls.index(money) + COVER_STEPS_ABOVE_THE_MONEY]
        wanted = {"sold_call": occ_ticker("C", money), "bought_call": occ_ticker("C", cover)}
        for ticker in wanted.values():
            bars = recorder.fetch(
                f"{ticker.replace(':', '_')}_minute_{day}.json",
                f"/v2/aggs/ticker/{ticker}/range/1/minute/{day}/{day}",
                MINUTE_BARS_QUERY,
            )
            for moment in (ENTRY_FILL_BAR_START, EXIT_FILL_BAR_START):
                require_a_bar_at(ticker, bars["results"], moment)
    standard = all(
        row["exercise_style"] == "american" and row["shares_per_contract"] == 100 for row in listing["results"]
    )
    facts = {
        "trading_date": day,
        "expiry": EXPIRY.isoformat(),
        "underlying_close_at_0935_new_york": str(price),
        "underlying_bar_start_ms": start_ms(DECISION_BAR_START),
        "entry_fill_bar_start_ms": start_ms(ENTRY_FILL_BAR_START),
        "exit_fill_bar_start_ms": start_ms(EXIT_FILL_BAR_START),
        "money_call_strike": str(money),
        "cover_call_strike": str(cover),
        "contracts": wanted,
        "listing_has_next_url": False,
        "listing_rows": len(listing["results"]),
        "call_strikes": len(calls),
        "put_strikes": len(puts),
        "every_listed_contract_is_standard": standard,
    }
    out.mkdir(parents=True, exist_ok=True)
    recorder.write(out)
    (out / "manifest.json").write_text(json.dumps({"requests": recorder.entries, "facts": facts}, indent=1) + "\n")
    return facts


if __name__ == "__main__":
    json.dump(main(Path(sys.argv[1])), sys.stdout, indent=1)
