"""The adapters behind the instrument facts, each against its own transport seam (#3502).

The lake read goes through the real writer and the real reader over real zip files; the vendor snapshot through the
SDK's own response model; the live line through the aggregator's own bar and status types. A source that fails says
why and raises nothing.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.services.instrument_facts_service import Miss, Observation
from app.services.instrument_facts_sources import (
    LIVE_QUOTE_MAX_AGE_MS,
    AggregatorLiveQuoteSource,
    PolygonSnapshotSource,
    ServiceArchiveSource,
)
from tests._helpers.instrument_facts_world import (
    FakeAggregator,
    RecordingVendor,
    et_ms,
    lake_source,
    minute_bar,
    polygon_service,
    vendor_row,
    write_session,
)

WED, THU = date(2026, 10, 21), date(2026, 10, 22)
NOW = et_ms(THU, 11)


# --- the lake's closes ---------------------------------------------------------------------------------------------------


def test_a_sessions_close_is_its_last_regular_hours_bar_and_after_hours_prints_do_not_move_it(tmp_path: Path) -> None:
    write_session(tmp_path, "SPY", THU, [(9, 30, "759.1000"), (15, 59, "760.0000"), (16, 0, "761.5000"), (17, 30, "799.0000")])
    write_session(tmp_path, "SPY", WED, [(9, 30, "757.0000"), (15, 59, "758.2500")])

    closes = lake_source(tmp_path).closes(["SPY"], [WED, THU])

    assert closes["SPY"][WED] == pytest.approx(758.25, abs=1e-9, rel=0)
    assert closes["SPY"][THU] == pytest.approx(760.0, abs=1e-9, rel=0)


def test_a_half_days_close_is_the_bar_before_its_early_close(tmp_path: Path) -> None:
    friday = date(2026, 11, 27)  # closes 13:00 ET
    write_session(tmp_path, "SPY", friday, [(9, 30, "700.0000"), (12, 59, "702.5000"), (13, 0, "710.0000"), (15, 59, "720.0000")])

    closes = lake_source(tmp_path).closes(["SPY"], [friday])

    assert closes["SPY"][friday] == pytest.approx(702.5, abs=1e-9, rel=0)


def test_a_session_the_lake_does_not_hold_is_a_miss_for_that_session_alone(tmp_path: Path) -> None:
    write_session(tmp_path, "SPY", THU, [(15, 59, "760.0000")])

    closes = lake_source(tmp_path).closes(["SPY", "QQQ"], [WED, THU])

    assert isinstance(closes["SPY"][WED], Miss)
    assert closes["SPY"][THU] == pytest.approx(760.0, abs=1e-9, rel=0)
    assert all(isinstance(closes["QQQ"][day], Miss) for day in (WED, THU))


def test_an_unreadable_file_is_a_logged_miss_not_an_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    folder = tmp_path / "equity" / "usa" / "minute" / "spy"
    folder.mkdir(parents=True)
    (folder / f"{THU:%Y%m%d}_trade.zip").write_bytes(b"not a zip")

    with caplog.at_level(logging.WARNING, logger="app.services.instrument_facts_sources"):
        closes = lake_source(tmp_path).closes(["SPY"], [THU])

    miss = closes["SPY"][THU]
    assert isinstance(miss, Miss)
    assert "BadZipFile" in miss.reason
    assert [record.action for record in caplog.records if hasattr(record, "action")] == ["instrument_facts_close_unreadable"]


def test_a_file_with_a_non_numeric_row_is_a_miss_for_that_session_and_the_next_session_still_reads(tmp_path: Path) -> None:
    import io
    import zipfile

    write_session(tmp_path, "SPY", WED, [(15, 59, "758.2500")])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{THU:%Y%m%d}_spy_minute_trade.csv", "34200000,x,y,z,w,1\n")
    (tmp_path / "equity" / "usa" / "minute" / "spy" / f"{THU:%Y%m%d}_trade.zip").write_bytes(buffer.getvalue())

    closes = lake_source(tmp_path).closes(["SPY"], [WED, THU])

    assert closes["SPY"][WED] == pytest.approx(758.25, abs=1e-9, rel=0)
    assert isinstance(closes["SPY"][THU], Miss)


def test_a_symbol_the_lake_cannot_hold_is_a_miss_that_never_touches_the_filesystem(tmp_path: Path) -> None:
    closes = lake_source(tmp_path).closes(["BRK-B"], [THU])

    miss = closes["BRK-B"][THU]
    assert isinstance(miss, Miss)
    assert "BRK-B" in miss.reason


# --- the vendor snapshot -----------------------------------------------------------------------------------------------


UPDATED_NS = (NOW - 15 * 60_000) * 1_000_000  # the vendor's stamp for a trade 15 minutes ago


async def test_one_vendor_request_answers_every_symbol_and_reads_the_newest_minute_close() -> None:
    vendor = RecordingVendor(
        [
            vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=UPDATED_NS),
            vendor_row("QQQ", minute_close=None, day_close=612.4, updated_ns=UPDATED_NS),
        ]
    )

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY", "QQQ", "XYZ"], NOW)

    assert vendor.calls == [("stocks", ["SPY", "QQQ", "XYZ"])]
    # SPY's price is its minute bar's close, as of that minute's end (the bar began two minutes before the stamp).
    assert readings["SPY"] == Observation(price=760.4, as_of_ms=NOW - 16 * 60_000)
    # QQQ has no minute bar, so its day close carries the only time the vendor gives: the snapshot's update stamp.
    assert readings["QQQ"] == Observation(price=612.4, as_of_ms=NOW - 15 * 60_000)
    assert isinstance(readings["XYZ"], Miss)


async def test_a_minute_close_is_as_of_the_end_of_its_own_minute_and_not_the_later_update_stamp() -> None:
    minute_start = NOW - 20 * 60_000
    # A quote moved the snapshot's stamp five minutes after the minute the last trade fell in.
    updated = minute_start + 5 * 60_000
    vendor = RecordingVendor(
        [vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=updated * 1_000_000, minute_start_ms=minute_start)]
    )

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY"], NOW)

    assert readings["SPY"] == Observation(price=760.4, as_of_ms=minute_start + 60_000)


async def test_a_minute_still_forming_is_never_stated_later_than_the_snapshots_own_stamp() -> None:
    updated = NOW - 15 * 60_000
    # The minute began 20 s before the vendor's stamp, so its end is still ahead of that stamp.
    vendor = RecordingVendor(
        [vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=updated * 1_000_000, minute_start_ms=updated - 20_000)]
    )

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY"], NOW)

    assert readings["SPY"] == Observation(price=760.4, as_of_ms=updated)


@pytest.mark.parametrize("minute_start_ms", [1, 1_792_000_000], ids=["epoch start", "seconds, not ms"])
async def test_a_minute_close_without_a_readable_time_is_a_miss_not_a_guessed_time(minute_start_ms: int) -> None:
    vendor = RecordingVendor(
        [vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=UPDATED_NS, minute_start_ms=minute_start_ms)]
    )

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY"], NOW)

    miss = readings["SPY"]
    assert isinstance(miss, Miss)
    assert "minute bar carries no readable time" in miss.reason


async def test_a_snapshot_before_the_vendor_repopulates_it_is_a_miss_not_a_zero_price() -> None:
    vendor = RecordingVendor([vendor_row("SPY", minute_close=0.0, day_close=0.0, updated_ns=0)])

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY"], NOW)

    miss = readings["SPY"]
    assert isinstance(miss, Miss)
    assert "no trade price" in miss.reason


@pytest.mark.parametrize(
    "updated_ns",
    [None, 0, 1_792_000_000_000, (NOW + 10 * 60_000) * 1_000_000],
    ids=["no stamp", "zero", "a millisecond stamp", "stamped in the future"],
)
async def test_a_snapshot_without_a_trustworthy_time_is_a_miss(updated_ns: int | None) -> None:
    vendor = RecordingVendor([vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=updated_ns)])

    readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY"], NOW)

    assert isinstance(readings["SPY"], Miss)


async def test_a_failed_vendor_request_is_one_logged_reason_for_every_symbol(caplog: pytest.LogCaptureFixture) -> None:
    vendor = RecordingVendor([], error=ConnectionError("vendor unreachable"))

    with caplog.at_level(logging.WARNING, logger="app.services.instrument_facts_sources"):
        readings = await PolygonSnapshotSource(polygon_service(vendor)).latest(["SPY", "QQQ"], NOW)

    assert {symbol: isinstance(reading, Miss) for symbol, reading in readings.items()} == {"SPY": True, "QQQ": True}
    assert all("ConnectionError" in reading.reason for reading in readings.values() if isinstance(reading, Miss))
    assert [record.action for record in caplog.records if hasattr(record, "action")] == ["instrument_facts_snapshot_failed"]


class _OrderedThrottle:
    """Records each request the client paces, against the vendor's own calls, in one order."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def acquire(self, label: str = "polygon", on_event: object | None = None) -> None:
        self._events.append(f"acquire:{label}")


async def test_the_one_snapshot_request_is_paced_before_it_is_sent_however_many_symbols_it_names() -> None:
    events: list[str] = []
    vendor = RecordingVendor([vendor_row("SPY", minute_close=760.4, day_close=760.1, updated_ns=UPDATED_NS)])
    service = polygon_service(vendor)
    service._throttle = _OrderedThrottle(events)  # type: ignore[assignment]
    inner = vendor.get_snapshot_all

    def _recording(market_type: str, tickers: list[str] | None = None) -> object:
        events.append("request")
        return inner(market_type, tickers)

    vendor.get_snapshot_all = _recording  # type: ignore[method-assign]

    await PolygonSnapshotSource(service).latest(["SPY", "QQQ", "IWM"], NOW)

    assert events == ["acquire:snapshots:3", "request"]


class _StuckVendor(RecordingVendor):
    """A vendor that does not answer until the test lets it."""

    def __init__(self) -> None:
        super().__init__([])
        self.release = threading.Event()

    def get_snapshot_all(self, market_type: str, tickers: list[str] | None = None) -> list[object]:  # type: ignore[override]
        self.release.wait(timeout=10)
        return []


async def test_a_vendor_that_does_not_answer_in_time_costs_the_rows_their_snapshot_not_the_page_its_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vendor = _StuckVendor()
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.instrument_facts_sources"):
            readings = await PolygonSnapshotSource(polygon_service(vendor), deadline_s=0.05).latest(["SPY", "QQQ"], NOW)
    finally:
        vendor.release.set()

    assert {symbol: isinstance(reading, Miss) for symbol, reading in readings.items()} == {"SPY": True, "QQQ": True}
    assert all("did not answer within 0.05 s" in reading.reason for reading in readings.values() if isinstance(reading, Miss))
    assert [record.action for record in caplog.records if hasattr(record, "action")] == ["instrument_facts_snapshot_timeout"]


async def test_no_symbols_make_no_vendor_request() -> None:
    vendor = RecordingVendor([])

    assert await PolygonSnapshotSource(polygon_service(vendor)).latest([], NOW) == {}
    assert vendor.calls == []


# --- the live line -------------------------------------------------------------------------------------------------------


def test_a_streaming_line_with_a_fresh_bar_is_the_live_price_at_that_bars_close() -> None:
    line = FakeAggregator("streaming", [minute_bar(NOW - 90_000, "759.00"), minute_bar(NOW - 30_000, "760.25")])

    reading = AggregatorLiveQuoteSource(line).latest("SPY", NOW)

    assert reading == Observation(price=760.25, as_of_ms=NOW - 30_000)
    assert line.subscribed == []


@pytest.mark.parametrize(
    ("state", "bars"),
    [
        ("idle", []),
        ("errored", [minute_bar(NOW - 30_000)]),
        ("resubscribing", [minute_bar(NOW - 30_000)]),
        ("streaming", []),
        ("streaming", [minute_bar(NOW - LIVE_QUOTE_MAX_AGE_MS - 1)]),
        ("streaming", [minute_bar(NOW + 1)]),
    ],
    ids=["no line", "errored line", "reconnecting line", "no closed bar", "stale bar", "bar from the future"],
)
def test_a_line_that_is_not_streaming_a_fresh_bar_is_not_a_live_price(state: str, bars: list[IbkrMinuteBar]) -> None:
    reading = AggregatorLiveQuoteSource(FakeAggregator(state, bars)).latest("SPY", NOW)

    assert isinstance(reading, Miss)
    assert "SPY" in reading.reason


# --- the archive -----------------------------------------------------------------------------------------------------------


def test_no_archive_service_is_a_miss_that_says_so() -> None:
    reading = ServiceArchiveSource(None).health()

    assert isinstance(reading, Miss)


def test_unreadable_receipts_are_a_logged_miss(caplog: pytest.LogCaptureFixture) -> None:
    def unreadable() -> None:
        raise OSError("state file unreadable")

    service = SimpleNamespace(health=unreadable)

    with caplog.at_level(logging.WARNING, logger="app.services.instrument_facts_sources"):
        reading = ServiceArchiveSource(service).health()  # type: ignore[arg-type]

    assert isinstance(reading, Miss)
    assert [record.action for record in caplog.records if hasattr(record, "action")] == ["instrument_facts_archive_unreadable"]


def test_readable_receipts_pass_through() -> None:
    health = object()
    service = SimpleNamespace(health=lambda: health)

    assert ServiceArchiveSource(service).health() is health  # type: ignore[arg-type]

