"""A small world for the instrument facts: a lake of real minute files, a vendor that answers in SDK models, a live line.

Shared by the adapter tests and the endpoint test, so each drives the real writer, the real reader and the SDK's own
snapshot model rather than a stand-in for them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polygon.rest.models import TickerSnapshot

from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.services.instrument_facts_sources import LakeCloseSource
from app.services.live_bar_aggregator import LiveLineStatus
from app.services.polygon_client import PolygonClientService

ET = ZoneInfo("America/New_York")


def et_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).timestamp() * 1000)


def write_session(root: Path, symbol: str, day: date, prices: list[tuple[int, int, str]]) -> None:
    """One session's minute file as the lake writer encodes it: ``(hour, minute, close)`` per bar."""
    bars = [
        MinuteTradeBar(
            bar_start_et=datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal(100),
        )
        for hour, minute, close in prices
    ]
    folder = root / "equity" / "usa" / "minute" / symbol.lower()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day:%Y%m%d}_trade.zip").write_bytes(build_minute_trade_zip_bytes(symbol, f"{day:%Y%m%d}", bars))


def lake_source(root: Path) -> LakeCloseSource:
    return LakeCloseSource("polygon_split_adjusted", root_for=lambda _mode: root, admission=lambda path: path.read_bytes())


def vendor_row(symbol: str, *, minute_close: float | None, day_close: float | None, updated_ns: int | None) -> dict[str, Any]:
    """A row as the vendor's v2 snapshot answers it, parsed by the SDK's own model."""
    body: dict[str, Any] = {"ticker": symbol, "todaysChange": 0.0, "todaysChangePerc": 0.0}
    if minute_close is not None:
        body["min"] = {"av": 1000, "o": 1.0, "h": 1.0, "l": 1.0, "c": minute_close, "v": 10, "vw": 1.0, "t": 1, "n": 1}
    if day_close is not None:
        body["day"] = {"o": 1.0, "h": 1.0, "l": 1.0, "c": day_close, "v": 10, "vw": 1.0}
    if updated_ns is not None:
        body["updated"] = updated_ns
    return {"ticker": symbol, "snapshot": TickerSnapshot.from_dict(body)}


class RecordingVendor:
    """Stands in for the SDK's ``RESTClient``: records each call, answers with SDK snapshot models."""

    def __init__(self, rows: list[dict[str, Any]], error: Exception | None = None) -> None:
        self.rows, self.error = rows, error
        self.calls: list[tuple[str, Any]] = []

    def get_snapshot_all(self, market_type: str, tickers: list[str] | None = None) -> list[TickerSnapshot]:
        self.calls.append((market_type, tickers))
        if self.error is not None:
            raise self.error
        return [row["snapshot"] for row in self.rows if tickers is None or row["ticker"] in tickers]


def polygon_service(vendor: RecordingVendor) -> PolygonClientService:
    service = PolygonClientService()
    service.client = vendor  # type: ignore[assignment]
    return service


def minute_bar(end_ms: int, close: str = "760.25") -> IbkrMinuteBar:
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
        fetched_at_ms=end_ms,
    )


class FakeAggregator:
    def __init__(self, status: str, bars: list[IbkrMinuteBar]) -> None:
        self._status, self._bars = status, bars
        self.subscribed: list[str] = []

    def status(self, symbol: str) -> LiveLineStatus:
        return LiveLineStatus(status=self._status)  # type: ignore[arg-type]

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[IbkrMinuteBar]:
        return list(self._bars)

    def ensure_subscribed(self, symbol: str) -> None:  # pragma: no cover - the read must never call it
        self.subscribed.append(symbol)


