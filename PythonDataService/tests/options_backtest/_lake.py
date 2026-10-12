"""A small lake in a temporary folder, written by the lake's own writers behind the tests' fake catalog.

A listing and an option day are stored through ``ensure_option_listing`` and ``ensure_option_day_bars`` with the vendor
faked by ``respx`` (the answers are the shapes the vendor really sent, ``tests/_helpers/option_vendor.py``), so what
a test reads back is what the lake writes. The underlying's stock minute file is written by the stock writer and
given a receipt in the fake catalog the way a captured file has one.

A test using these is decorated with ``@respx.mock`` and asks for the ``fake_catalog`` and ``tmp_lake`` fixtures; the
clock fixture is autouse, so the session and plan rules run on 2026-10-12 and not on the day the test runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake import option_bars as option_bars_module
from app.data_lake import option_lake as option_lake_module
from app.data_lake import option_listing as option_listing_module
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_bars import ensure_option_day_bars
from app.data_lake.option_lake import OptionCaptureResult
from app.data_lake.option_listing import ensure_option_listing
from app.data_lake.path_policy import LeanMinuteBarPath, lake_subpath
from app.data_lake.run_materialization import EngineRunMaterialization
from app.data_lake.types import ArtifactIdentity
from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp
from tests._helpers.option_vendor import bar_row, no_trades, traded

D = Decimal

# 2026-10-12 14:00 New York, the day the plan's floor was probed.
NOW_2026_10_12_MS = 1791828000000
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"

# 2025-06-13 is a Friday and a full session: it opens at 13:30 UTC and closes at 20:00 UTC.
FRIDAY = date(2025, 6, 13)
FRIDAY_OPEN_MS = 1749821400000
FRIDAY_CLOSE_MS = 1749844800000
MINUTE_MS = 60_000


@pytest.fixture(autouse=True)
def the_clock_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (option_lake_module, option_bars_module, option_listing_module):
        monkeypatch.setattr(module, "current_ms", lambda: NOW_2026_10_12_MS)


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def raw_root(tmp_lake: Path) -> Path:
    return tmp_lake / lake_subpath("raw")


def vendor_listing_row(contract: OptionContract, *, underlying: str = "SPY", **overrides: Any) -> dict[str, Any]:
    """A listed contract exactly as the vendor sends it: nine keys, and no ``additional_underlyings``."""
    strike = contract.strike
    row: dict[str, Any] = {
        "cfi": "OCASPS" if contract.right == "call" else "OPASPS",
        "contract_type": contract.right,
        "exercise_style": "american",
        "expiration_date": contract.expiry_date.isoformat(),
        "primary_exchange": "BATO",
        "shares_per_contract": 100,
        "strike_price": int(strike) if strike == int(strike) else float(strike),
        "ticker": f"O:{contract.occ_symbol}",
        "underlying_ticker": underlying,
    }
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not None}


async def store_listing(
    rows: Sequence[dict[str, Any]], *, underlying: str = "SPY", day: date = FRIDAY
) -> OptionCaptureResult:
    """Capture the listing of ``underlying`` for ``day`` with these vendor rows, through the lake's own function."""
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(200, json={"request_id": "r", "status": "OK", "results": list(rows)})
    )
    result = await ensure_option_listing(underlying, day)
    assert result.failure is None, result.failure
    return result


def minute_bar(index: int, o: float, h: float, low: float, c: float, *, volume: int = 10) -> dict[str, Any]:
    """A vendor result row for the minute that starts ``index`` minutes after the Friday's open."""
    return bar_row(index, o=o, h=h, l=low, c=c, v=volume, vw=c, n=1)


async def store_option_day(
    contract: OptionContract,
    results: Sequence[dict[str, Any]],
    *,
    day: date = FRIDAY,
    listed_under: str = "SPY",
) -> OptionCaptureResult:
    """Capture one contract's session through the lake's own function; no results is a listed day with no trade."""
    symbol = contract.occ_symbol
    respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/O:{symbol}/range/1/minute/.*").mock(
        return_value=traded(symbol, list(results)) if results else no_trades(symbol)
    )
    result = await ensure_option_day_bars(contract, day, listed_under=listed_under)
    assert result.failure is None, result.failure
    return result


def stock_bar(start_ms: int, o: str, h: str, low: str, c: str, *, symbol: str = "SPY") -> TradeBar:
    return TradeBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + MINUTE_MS,
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=100,
    )


def stock_day_zip(bars: Sequence[TradeBar], *, symbol: str = "SPY", day: date = FRIDAY) -> bytes:
    """The stock writer's bytes for a day holding exactly these bars, whatever their starts (no receipt, nothing stored)."""
    with tempfile.TemporaryDirectory() as folder:
        return write_lean_day_zip(Path(folder), symbol, day, list(bars)).read_bytes()


async def store_underlying_day(
    fake_catalog: FakeCatalog, tmp_lake: Path, bars: Sequence[TradeBar], *, symbol: str = "SPY", day: date = FRIDAY
) -> Path:
    """Write the stock minute file for ``day`` and give it the catalog receipt a captured file has."""
    path = write_lean_day_zip(raw_root(tmp_lake), symbol, day, list(bars))
    relative = LeanMinuteBarPath(market="usa", symbol=symbol, trading_date=day, data_type="trade").relative_path()
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol=symbol,
        trading_date=day,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )
    artifact_id = await fake_catalog.claim_minute_bar(identity, "test-worker", 60_000, "test-recipe", str(relative))
    assert artifact_id is not None
    payload = path.read_bytes()
    completed = await fake_catalog.complete_artifact(
        artifact_id,
        len(bars),
        bars[0].start_ms,
        bars[-1].start_ms,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    assert completed
    return path


def option_bars_path(contract: OptionContract, day: date = FRIDAY) -> str:
    """Where the lake keeps one contract's bars for a day, relative to the raw root."""
    return f"option/usa/minute/{contract.root.lower()}/{contract.occ_symbol.lower()}/{day:%Y%m%d}_trade.zip"


def option_day_zip(contract: OptionContract, starts_ms: Sequence[int], *, day: date = FRIDAY) -> bytes:
    """The lake's own bytes for a contract's day, one bar starting at each of these instants, on a minute or not.

    The capture refuses a vendor bar that does not start on a whole minute, so a stored day holding one is made here
    and put in place of a captured file with ``replace_the_stored_file``.
    """
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    bars = [
        MinuteTradeBar(
            bar_start_et=(epoch + timedelta(milliseconds=start_ms)).astimezone(ZoneInfo("America/New_York")),
            open=D("1.27"),
            high=D("1.31"),
            low=D("1.25"),
            close=D("1.27"),
            volume=D(10),
        )
        for start_ms in starts_ms
    ]
    return build_minute_trade_zip_bytes(
        symbol=contract.occ_symbol, trading_date_yyyymmdd=f"{day:%Y%m%d}", bars=bars
    )


def replace_the_stored_file(
    fake_catalog: FakeCatalog, raw: Path, relative_path: str, payload: bytes, *, admit: bool
) -> None:
    """Put other bytes in a stored file; ``admit`` gives them the receipt too, so the catalog admits them."""
    (raw / relative_path).write_bytes(payload)
    if not admit:
        return
    [row] = [row for row in fake_catalog.rows.values() if row["file_path"] == relative_path]
    row["file_sha256"] = hashlib.sha256(payload).hexdigest()
    row["file_size_bytes"] = len(payload)


def the_file_of(raw: Path, relative_path: str) -> bytes:
    return (raw / relative_path).read_bytes()


BARS_URL = r"https://api\.polygon\.io/v2/aggs/ticker/O:[A-Z]+\d{6}[CP]\d{8}/range/1/minute/.*"


def symbol_asked(request: httpx.Request) -> str:
    """The OCC symbol a bars request is for."""
    return request.url.path.split("/ticker/O:")[1].split("/")[0]


def vendor_lists(*contracts: OptionContract, underlying: str = "SPY") -> respx.Route:
    """Make the vendor answer a listing request with these contracts (it is not requested until a test captures)."""
    return respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "request_id": "r",
                "status": "OK",
                "results": [vendor_listing_row(c, underlying=underlying) for c in contracts],
            },
        )
    )


def vendor_bars(answer: Callable[[str], httpx.Response]) -> respx.Route:
    """Make the vendor answer every bars request by the symbol asked for."""
    return respx.get(url__regex=BARS_URL).mock(side_effect=lambda request: answer(symbol_asked(request)))


class StockFetchDouble:
    """The stock engine's fetch of the underlying's day, as a double: it writes the file and its receipt, and counts."""

    def __init__(self, fake_catalog: FakeCatalog, tmp_lake: Path, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fake_catalog, self._tmp_lake, self._error = fake_catalog, tmp_lake, error

    def __call__(self, **arguments: Any) -> EngineRunMaterialization:
        self.calls.append(arguments)
        if self._error is not None:
            raise self._error
        bars = [stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25")]
        # Called from a worker thread, which has no loop of its own.
        asyncio.run(
            store_underlying_day(
                self._fake_catalog, self._tmp_lake, bars, symbol=arguments["symbol"], day=arguments["start"]
            )
        )
        return EngineRunMaterialization(
            availability_hash="a-hash", fetched_artifact_count=1, reused_artifact_count=0, incomplete_summary=None
        )
