"""GET /api/markets/instrument-facts over ASGI: live, delayed, last close and unavailable (#3502).

The service is the real one, over the real adapters at their transports: the lake's real zip files through the real
reader, the vendor's snapshot as the SDK's own model, a live line as the aggregator's own bar and status types, the
archive's real health projection over a receipt document. Only the clock is fixed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.option_archive.health import project_health
from app.option_archive.planner import SessionOutcome
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveStateDoc
from app.routers.instrument_facts import get_instrument_facts_service, router
from app.schemas.instrument_facts import MAX_FACT_SYMBOLS
from app.schemas.option_archive import OptionArchiveHealth
from app.services.instrument_facts_service import CLOSES_ADJUSTMENT, InstrumentFactsService
from app.services.instrument_facts_sources import (
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

# Monday the 19th to Friday the 23rd of October 2026, then the weekend.
MON, TUE, WED, THU, FRI, SAT = (date(2026, 10, day) for day in (19, 20, 21, 22, 23, 24))
NS_PER_MS = 1_000_000

pytestmark = pytest.mark.asyncio


def make_app(
    now_ms: int,
    lake: Path,
    *,
    aggregator: FakeAggregator | None = None,
    vendor: RecordingVendor | None = None,
    archive: object | None = None,
) -> FastAPI:
    service = InstrumentFactsService(
        live=AggregatorLiveQuoteSource(aggregator or FakeAggregator("idle", [])),
        snapshots=PolygonSnapshotSource(polygon_service(vendor or RecordingVendor([]))),
        closes=lake_source(lake),
        archive=ServiceArchiveSource(archive),  # type: ignore[arg-type]
        clock=lambda: now_ms,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/markets")
    app.dependency_overrides[get_instrument_facts_service] = lambda: service
    return app


async def get(app: FastAPI, symbols: str) -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/markets/instrument-facts", params={"symbols": symbols})
    return response.status_code, response.json()


def _health(state: ArchiveStateDoc, now_ms: int) -> OptionArchiveHealth:
    return project_health(state, now_ms=now_ms, config=ArchiveSettings(ENABLED=True))


def lake_with_closes(root: Path) -> None:
    write_session(root, "SPY", TUE, [(15, 59, "750.0000")])
    write_session(root, "SPY", WED, [(15, 59, "758.2500")])
    write_session(root, "SPY", THU, [(15, 59, "766.8900")])
    write_session(root, "SPY", FRI, [(9, 30, "760.0000"), (15, 59, "759.9700"), (16, 30, "770.0000")])


async def test_a_live_line_answers_a_live_quote_and_asks_the_vendor_nothing(tmp_path: Path) -> None:
    lake_with_closes(tmp_path)
    now = et_ms(THU, 11)
    line = FakeAggregator("streaming", [minute_bar(now - 20_000, "767.50")])
    vendor = RecordingVendor([])

    status, body = await get(make_app(now, tmp_path, aggregator=line, vendor=vendor), "SPY")

    assert status == 200
    quote = body["facts"][0]["quote"]
    assert (quote["status"], quote["basis"], quote["price"], quote["as_of_ms"]) == ("available", "live_quote", 767.5, now - 20_000)
    assert quote["move"]["comparison_close"] == {"price": 758.25, "session_close_ms": et_ms(WED, 16)}
    assert quote["move"]["change"] == pytest.approx(9.25, abs=1e-9, rel=0)
    assert quote["move"]["change_percent"] == pytest.approx(100 * 9.25 / 758.25, abs=1e-9, rel=0)
    assert quote["not_used"] == []
    assert vendor.calls == []
    assert line.subscribed == []


async def test_without_a_gateway_session_the_delayed_snapshot_answers_and_says_it_is_not_live(tmp_path: Path) -> None:
    lake_with_closes(tmp_path)
    now = et_ms(THU, 11)
    trade_ms = now - 16 * 60_000
    vendor = RecordingVendor([vendor_row("SPY", minute_close=767.1, day_close=767.0, updated_ns=trade_ms * NS_PER_MS)])

    status, body = await get(make_app(now, tmp_path, vendor=vendor), "SPY")

    assert status == 200
    quote = body["facts"][0]["quote"]
    assert (quote["basis"], quote["price"], quote["as_of_ms"]) == ("delayed_snapshot", 767.1, trade_ms)
    assert [item["basis"] for item in quote["not_used"]] == ["live_quote"]
    assert vendor.calls == [("stocks", ["SPY"])]


async def test_on_a_weekend_the_last_completed_close_answers_at_the_calendars_close(tmp_path: Path) -> None:
    lake_with_closes(tmp_path)
    now = et_ms(SAT, 12)
    cleared = RecordingVendor([vendor_row("SPY", minute_close=0.0, day_close=0.0, updated_ns=0)])

    status, body = await get(make_app(now, tmp_path, vendor=cleared), "SPY")

    assert status == 200
    quote = body["facts"][0]["quote"]
    # Friday's 16:30 print is after hours and is not the close.
    assert (quote["basis"], quote["price"], quote["as_of_ms"]) == ("last_close", 759.97, et_ms(FRI, 16))
    assert [item["basis"] for item in quote["not_used"]] == ["live_quote", "delayed_snapshot"]
    assert quote["move"]["comparison_close"] == {"price": 766.89, "session_close_ms": et_ms(THU, 16)}
    assert quote["move"]["change"] == -6.92
    assert quote["move"]["change_percent"] == pytest.approx(-100 * 6.92 / 766.89, abs=1e-9, rel=0)


async def test_when_every_source_fails_the_quote_is_unavailable_and_the_rest_of_the_answer_stands(tmp_path: Path) -> None:
    now = et_ms(THU, 11)
    broken_vendor = RecordingVendor([], error=ConnectionError("unreachable"))
    state = ArchiveStateDoc(version=1)
    archive = SimpleNamespace(health=lambda: _health(state, now))

    status, body = await get(make_app(now, tmp_path, vendor=broken_vendor, archive=archive), "SPY")

    assert status == 200
    fact = body["facts"][0]
    assert fact["quote"]["status"] == "unavailable"
    assert [item["basis"] for item in fact["quote"]["not_used"]] == ["live_quote", "delayed_snapshot", "last_close"]
    assert "ConnectionError" in fact["quote"]["not_used"][1]["reason"]
    assert fact["options_archive"]["kind"] == "none_stored"
    assert body["closes_adjustment"] == CLOSES_ADJUSTMENT


async def test_a_price_without_its_comparison_close_still_shows_with_the_reason_it_has_no_move(tmp_path: Path) -> None:
    write_session(tmp_path, "SPY", TUE, [(15, 59, "750.0000")])  # no Wednesday, the close to measure from
    now = et_ms(THU, 11)
    line = FakeAggregator("streaming", [minute_bar(now - 20_000, "767.50")])

    _, body = await get(make_app(now, tmp_path, aggregator=line), "SPY")

    quote = body["facts"][0]["quote"]
    assert quote["price"] == 767.5
    assert quote["move"] is None
    assert str(WED) in quote["move_unavailable_reason"]


async def test_the_archive_badge_separates_some_days_stored_from_every_required_day(tmp_path: Path) -> None:
    now = et_ms(SAT, 12)
    state = ArchiveStateDoc(version=1, baseline_ms=et_ms(MON, 9, 30))
    sessions = [et_ms(day, 9, 30) for day in (MON, TUE, WED, THU, FRI)]
    for session_ms in sessions:
        state.session("SPY", session_ms).outcome = SessionOutcome.COMPLETE
    for session_ms in sessions[:3]:
        state.session("QQQ", session_ms).outcome = SessionOutcome.COMPLETE
    archive = SimpleNamespace(health=lambda: _health(state, now))

    status, body = await get(make_app(now, tmp_path, archive=archive), "SPY,QQQ,IWM,AAPL")

    assert status == 200
    badges = {fact["symbol"]: fact["options_archive"] for fact in body["facts"]}
    assert badges["SPY"]["kind"] == "complete"
    assert badges["SPY"]["last_archived_session_ms"] == sessions[-1]
    assert badges["QQQ"]["kind"] == "some_days"
    assert (badges["QQQ"]["last_archived_session_ms"], badges["QQQ"]["missing_sessions"]) == (sessions[2], 2)
    assert badges["IWM"]["kind"] == "none_stored"
    assert badges["AAPL"] is None


async def test_a_page_of_twenty_rows_is_one_vendor_request_and_opens_no_live_line(tmp_path: Path) -> None:
    now = et_ms(THU, 11)
    symbols = [f"S{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]
    line = FakeAggregator("idle", [])
    vendor = RecordingVendor([vendor_row(symbol, minute_close=10.0, day_close=10.0, updated_ns=(now - 16 * 60_000) * NS_PER_MS) for symbol in symbols])

    status, body = await get(make_app(now, tmp_path, aggregator=line, vendor=vendor), ",".join(symbols))

    assert status == 200
    assert [fact["symbol"] for fact in body["facts"]] == symbols
    assert vendor.calls == [("stocks", symbols)]
    assert line.subscribed == []
    assert {fact["quote"]["basis"] for fact in body["facts"]} == {"delayed_snapshot"}


async def test_every_instant_on_the_wire_is_an_integer_millisecond_count(tmp_path: Path) -> None:
    lake_with_closes(tmp_path)
    now = et_ms(SAT, 12)

    _, body = await get(make_app(now, tmp_path), "SPY")

    quote = body["facts"][0]["quote"]
    assert type(body["generated_ms"]) is int
    assert body["generated_ms"] == now
    assert type(quote["as_of_ms"]) is int
    assert type(quote["move"]["comparison_close"]["session_close_ms"]) is int


@pytest.mark.parametrize(
    "symbols",
    [
        ",".join(f"A{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:MAX_FACT_SYMBOLS]) + ",ZZ",
        "SPY,SPY",
        "spy",
        "SPY,,QQQ",
        "NOTASYMBOL1",
        "SPY;QQQ",
    ],
    ids=["one over the bound", "a symbol twice", "lowercase", "an empty entry", "not a US-equity symbol", "wrong separator"],
)
async def test_a_list_that_is_not_a_bounded_set_of_us_equity_symbols_is_a_422(tmp_path: Path, symbols: str) -> None:
    status, body = await get(make_app(et_ms(THU, 11), tmp_path), symbols)

    assert status == 422
    assert body["detail"]


async def test_a_request_with_no_symbols_is_a_422(tmp_path: Path) -> None:
    app = make_app(et_ms(THU, 11), tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/markets/instrument-facts")

    assert response.status_code == 422
