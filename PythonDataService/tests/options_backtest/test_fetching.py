"""The download phase of a run, over the lake's own functions with the vendor faked at the HTTP layer (#3030).

Three consecutive sessions, Wednesday 2026-10-07 to Friday 2026-10-09, are fetched into a lake in a temporary folder.
"Now" is Monday 2026-10-12 14:00 New York, more than a day after the last of them, so every session is over under
any rule for when a session's data is final. Hand-worked figures:

* A same-day at-the-money call. The underlying closes at 500.20 in every minute, so the nearest strike is 500 and the
  one contract a session needs is that day's 500 call. Three sessions, three contracts.
* Eight legs, calls four strikes above and below the money (steps -4 to -1 and 1 to 4 from 500): the strikes
  496 to 499 and 501 to 504, so a session needs eight contracts. The vendor may be asked for at most four at once.
* The first failing contract of a batch of eight, with four allowed in flight: the first four requests start
  together, the first of them fails when its answer comes, and the other four never start. Four requests, not eight.
* The captured days. Eight contracts are stored; then three of them are the only ones a later run needs and the other
  five are new: the lake is asked once which are captured, and only the five are captured, five requests.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake import option_bars
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_lake import MAX_VENDOR_REQUESTS_IN_FLIGHT
from app.options.contract import OptionContract
from app.options_backtest import fetching
from app.options_backtest.fetching import (
    DownloadFailed,
    DownloadStopped,
    ensure_run_data,
)
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.service import failure_of
from app.options_backtest.setup import MS_PER_MINUTE
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root, replace_the_stored_file
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import FRI, HALF_DAY, THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor, eight_legs_setup, ladder_day
from tests.options_backtest._words import assert_plain
from tests.options_backtest.setup_fixtures import leg, setup

# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000
D = Decimal
TUE = date(2026, 10, 6)
SPY_500_CALL = ("call", 500)
THREE_SESSIONS = [WED, THU, FRI]


def _day(**changes: Any) -> Day:
    return Day(underlying=steady("500.20"), options={SPY_500_CALL: flat("1.00", 1, 390)}, **changes)


def _scene(*days: date) -> dict[date, Day]:
    return {day: _day() for day in days}


def _same_day_call() -> Any:
    return setup(legs=[leg(expiry={"kind": "same_day"})])


async def _fetch(written: Any, sessions: list[date], *, stop_after: int | None = None, **kwargs: Any) -> Any:
    progress: list[tuple[int, int]] = []

    def should_stop() -> bool:
        return stop_after is not None and len(progress) >= stop_after

    report = await asyncio.to_thread(
        ensure_run_data,
        written,
        sessions,
        now_ms=NOW_MS,
        on_progress=lambda done, total: progress.append((done, total)),
        should_stop=should_stop,
        **kwargs,
    )
    return report, progress


@respx.mock
async def test_a_range_is_brought_into_the_lake_and_reads_back_as_the_vendor_sent_it(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS))
    vendor.mount()

    report, progress = await _fetch(_same_day_call(), THREE_SESSIONS)

    # Three sessions, each with one listing and one contract day; nothing was in the lake before.
    assert (report.sessions_prepared, report.listings_fetched, report.listings_reused) == (3, 3, 0)
    assert (report.option_days_fetched, report.option_days_reused) == (3, 0)
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert sorted(vendor.stock_requests) == THREE_SESSIONS
    assert sorted(vendor.listing_requests) == THREE_SESSIONS
    assert sorted(vendor.option_requests) == [f"SPY{day:%y%m%d}C00500000" for day in THREE_SESSIONS]

    reader = LakeRunData("SPY")
    call = OptionContract.of(root="SPY", expiry_date=THU, right="call", strike=500)
    underlying = await asyncio.to_thread(reader.underlying_day, THU)
    listing = await asyncio.to_thread(reader.listing, THU)
    option_day = await asyncio.to_thread(reader.option_day, call, THU)
    assert underlying is not None and len(underlying.value) == 390
    assert {bar.close for bar in underlying.value} == {D("500.20")}
    assert listing is not None and call in {listed.contract for listed in listing.value.contracts}
    assert option_day is not None and len(option_day.value) == 390


@respx.mock
async def test_a_second_run_over_the_same_range_asks_the_vendor_for_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS))
    vendor.mount()
    await _fetch(_same_day_call(), THREE_SESSIONS)
    asked_the_first_time = vendor.vendor_requests

    report, _ = await _fetch(_same_day_call(), THREE_SESSIONS)

    assert vendor.vendor_requests == asked_the_first_time
    assert (report.listings_fetched, report.listings_reused) == (0, 3)
    assert (report.option_days_fetched, report.option_days_reused) == (0, 3)


@respx.mock
async def test_a_session_whose_window_does_not_fit_fetches_nothing(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    # Enter 205 minutes after the open, leave 10 before the close: fine on a full day, not on the 13:00 half day.
    late = setup(
        entry={"anchor": "open", "offset_ms": 205 * MS_PER_MINUTE},
        exit={"anchor": "close", "offset_ms": 10 * MS_PER_MINUTE},
        legs=[leg(expiry={"kind": "same_day"})],
    )
    vendor = Vendor(_scene(THU))  # the half day is not in the scene: asking for it would be a KeyError
    vendor.mount()

    report, progress = await _fetch(late, [THU, HALF_DAY])

    assert report.sessions_prepared == 1
    assert progress == [(1, 2), (2, 2)]
    assert HALF_DAY not in vendor.stock_requests and HALF_DAY not in vendor.listing_requests


@respx.mock
async def test_at_most_four_contract_requests_are_in_flight_at_once(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    ladder = [(-4, "a"), (-3, "b"), (-2, "c"), (-1, "d"), (1, "e"), (2, "f"), (3, "g"), (4, "h")]
    eight_legs = setup(
        legs=[
            leg(leg_id=name, expiry={"kind": "same_day"}, strike={"kind": "atm_offset", "steps": steps})
            for steps, name in ladder
        ]
    )
    day = Day(
        underlying=steady("500.20"),
        options={("call", strike): flat("1.00", 1, 390) for strike in range(496, 505)},
        strikes=tuple(range(496, 505)),
    )
    vendor = Vendor({WED: day}, option_latency_s=0.05)
    vendor.mount()

    await _fetch(eight_legs, [WED])

    assert MAX_VENDOR_REQUESTS_IN_FLIGHT == 4
    assert len(vendor.option_requests) == 8
    assert vendor.peak_in_flight == 4


@respx.mock
async def test_once_one_request_of_a_batch_has_failed_no_other_starts_and_the_session_and_reason_are_named(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    ladder = [(-4, "a"), (-3, "b"), (-2, "c"), (-1, "d"), (1, "e"), (2, "f"), (3, "g"), (4, "h")]
    eight_legs = setup(
        legs=[
            leg(leg_id=name, expiry={"kind": "same_day"}, strike={"kind": "atm_offset", "steps": steps})
            for steps, name in ladder
        ]
    )
    day = Day(
        underlying=steady("500.20"),
        options={("call", strike): flat("1.00", 1, 390) for strike in range(496, 505)},
        strikes=tuple(range(496, 505)),
    )
    # The contracts go in OCC-symbol order: the 496 call is the first of the batch.
    vendor = Vendor({WED: day}, option_latency_s=0.05, option_errors={"SPY261007C00496000": 429})
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _fetch(eight_legs, [WED])

    assert caught.value.session == WED
    assert caught.value.reason == "provider_rate_limited"
    assert "SPY261007C00496000" in caught.value.what
    assert len(vendor.option_requests) == 4, "the four in flight finished; the other four never began"


@respx.mock
async def test_a_failure_in_a_later_session_leaves_the_earlier_ones_in_the_lake_and_fetches_no_more(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS), option_errors={"SPY261008C00500000": 500})
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _fetch(_same_day_call(), THREE_SESSIONS)

    assert caught.value.session == THU
    assert caught.value.reason == "provider_api_error"
    assert FRI not in vendor.listing_requests, "nothing after the failed session was fetched"
    wednesdays_call = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=500)
    assert await asyncio.to_thread(LakeRunData("SPY").option_day, wednesdays_call, WED) is not None


@respx.mock
async def test_a_listing_the_vendor_cannot_give_is_a_failure_naming_the_session(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS), listing_errors={WED: 503})
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _fetch(_same_day_call(), THREE_SESSIONS)

    assert caught.value.session == WED
    assert "listing" in caught.value.what
    assert caught.value.reason == "provider_api_error"
    assert vendor.option_requests == []


@respx.mock
async def test_the_underlying_that_cannot_be_fetched_stops_the_phase_before_any_listing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS), stock_errors={THU: 500})
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _fetch(_same_day_call(), THREE_SESSIONS)

    assert caught.value.session is None
    assert "SPY" in caught.value.what
    assert vendor.listing_requests == []


@respx.mock
async def test_a_session_whose_data_the_lake_will_not_take_yet_is_a_failure_with_the_lakes_reason(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(FRI))
    vendor.mount()
    inside_friday = 1791828000000 - 3 * 24 * 3600 * 1000  # Friday 2026-10-09 14:00 New York

    with pytest.raises(DownloadFailed) as caught:
        await asyncio.to_thread(
            ensure_run_data, _same_day_call(), [FRI], now_ms=inside_friday, should_stop=lambda: False
        )

    assert caught.value.reason == "session_not_over"
    assert caught.value.session == FRI
    assert vendor.listing_requests == [] and vendor.option_requests == []


@respx.mock
async def test_a_stop_between_sessions_ends_the_phase_and_asks_for_no_more(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS))
    vendor.mount()

    with pytest.raises(DownloadStopped) as caught:
        await _fetch(_same_day_call(), THREE_SESSIONS, stop_after=1)

    assert caught.value.sessions_done == 1
    assert vendor.listing_requests == [WED]


@respx.mock
async def test_the_underlying_is_asked_for_in_chunks_each_just_before_the_first_session_in_it(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Five sessions in chunks of two: [Mon, Tue], [Wed, Thu], [Fri]. A chunk is asked for when the phase reaches its
    # first session, so the progress counts sessions from the start and a failure at the third fetches no more bars.
    monday = date(2026, 10, 5)
    five = [monday, TUE, WED, THU, FRI]
    vendor = Vendor(_scene(*five))
    vendor.mount()
    real = fetching.materialize_engine_run

    def asked_for(*, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        vendor.requests.append(f"CHUNK {start} {end}")
        return real(symbol=symbol, start=start, end=end, **kwargs)

    monkeypatch.setattr(fetching, "materialize_engine_run", asked_for)
    monkeypatch.setattr(fetching, "UNDERLYING_CHUNK_SESSIONS", 2)

    await _fetch(_same_day_call(), five)

    marks = [
        (request, "listing" if "contracts" in request else "chunk" if request.startswith("CHUNK") else "other")
        for request in vendor.requests
    ]
    chunk_marks = [request for request, kind in marks if kind == "chunk"]
    assert chunk_marks == [f"CHUNK {monday} {TUE}", f"CHUNK {WED} {THU}", f"CHUNK {FRI} {FRI}"]
    # The sessions' listings were asked for in date order, and no listing is asked for before its chunk.
    kinds_in_order = [kind for _, kind in marks if kind in ("chunk", "listing")]
    assert kinds_in_order == ["chunk", "listing", "listing", "chunk", "listing", "listing", "chunk", "listing"]


def test_a_stop_before_the_first_chunk_asks_the_lake_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[date, date]] = []
    monkeypatch.setattr(
        fetching, "materialize_engine_run", lambda *, symbol, start, end, **kwargs: calls.append((start, end))
    )

    with pytest.raises(DownloadStopped):
        ensure_run_data(_same_day_call(), THREE_SESSIONS, now_ms=NOW_MS, should_stop=lambda: True)

    assert calls == []


# ── A session that is not over ───────────────────────────────────────────
# The stock pipeline that fetches the underlying has no gate on a session that is not over: it would store the minutes
# so far as the whole day. Check data refuses such a session first, but the phase is a function of its own, so the
# lake's rule is asked of each session in a chunk before the chunk is. Hand-worked figures: the lake takes a session's
# data from New York midnight after it (EDT, UTC-4, so 04:00 UTC the next day). 2026-10-08 is epoch day 20734 and
# 20734 x 86 400 000 = 1 791 417 600 000 is its UTC midnight, so Wednesday's data is taken from that plus 14 400 000 =
# 1 791 432 000 000. Each following day is 86 400 000 later: Thursday's from 1 791 518 400 000, Friday's from
# 1 791 604 800 000. A session opens at 13:30 UTC = 48 600 000 after its UTC midnight: Wednesday 1 791 379 800 000,
# Thursday 1 791 466 200 000, Friday 1 791 552 600 000. An hour (3 600 000) into Thursday is 1 791 469 800 000 and an
# hour into Friday is 1 791 556 200 000.

WEDNESDAYS_DATA_FROM_MS = 1791432000000
THURSDAYS_OPEN_MS = 1791466200000
AN_HOUR_INTO_THURSDAY_MS = 1791469800000
FRIDAYS_OPEN_MS = 1791552600000
AN_HOUR_INTO_FRIDAY_MS = 1791556200000


def _stock_pipeline_that_must_not_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    asked: list[dict[str, Any]] = []

    def must_not_run(**kwargs: Any) -> Any:
        asked.append(kwargs)
        raise AssertionError("the stock pipeline was asked for a session that is not over")

    monkeypatch.setattr(fetching, "materialize_engine_run", must_not_run)
    return asked


@respx.mock
async def test_a_chunk_that_holds_a_session_not_over_is_refused_before_the_stock_pipeline_is_asked_and_names_the_session(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(_scene(WED, THU))
    vendor.mount()
    asked = _stock_pipeline_that_must_not_run(monkeypatch)

    with pytest.raises(DownloadFailed) as caught:
        await asyncio.to_thread(
            ensure_run_data, _same_day_call(), [WED, THU], now_ms=AN_HOUR_INTO_THURSDAY_MS, should_stop=lambda: False
        )

    assert (caught.value.reason, caught.value.session, caught.value.what) == (
        "session_not_over",
        THU,
        "the minute bars of SPY",
    )
    failure = failure_of(caught.value)
    assert failure.code == "DOWNLOAD_FAILED"
    [item] = failure.items
    assert (item.session_open_ms, item.code) == (THURSDAYS_OPEN_MS, "session_not_over")
    assert_plain(failure.reason)
    assert asked == [] and vendor.vendor_requests == 0, "Wednesday, which is over, is not fetched either: one chunk"


@respx.mock
async def test_a_session_is_taken_from_the_millisecond_the_lake_takes_it_and_not_before(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(_scene(WED))
    vendor.mount()
    real = fetching.materialize_engine_run
    asked = _stock_pipeline_that_must_not_run(monkeypatch)

    with pytest.raises(DownloadFailed) as one_before:
        await asyncio.to_thread(
            ensure_run_data, _same_day_call(), [WED], now_ms=WEDNESDAYS_DATA_FROM_MS - 1, should_stop=lambda: False
        )
    monkeypatch.setattr(fetching, "materialize_engine_run", real)
    report = await asyncio.to_thread(
        ensure_run_data, _same_day_call(), [WED], now_ms=WEDNESDAYS_DATA_FROM_MS, should_stop=lambda: False
    )

    assert one_before.value.reason == "session_not_over" and asked == []
    assert report.sessions_prepared == 1 and vendor.stock_requests == [WED]


@respx.mock
async def test_the_refusal_comes_at_the_chunk_that_holds_the_session_not_over_and_earlier_chunks_are_fetched_as_before(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Chunks of two: [Wed, Thu], [Fri]. An hour into Friday only Friday is not over.
    vendor = Vendor(_scene(*THREE_SESSIONS))
    vendor.mount()
    real = fetching.materialize_engine_run
    asked: list[tuple[date, date]] = []

    def delegating(*, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        asked.append((start, end))
        return real(symbol=symbol, start=start, end=end, **kwargs)

    monkeypatch.setattr(fetching, "materialize_engine_run", delegating)
    monkeypatch.setattr(fetching, "UNDERLYING_CHUNK_SESSIONS", 2)

    with pytest.raises(DownloadFailed) as caught:
        await asyncio.to_thread(
            ensure_run_data, _same_day_call(), THREE_SESSIONS, now_ms=AN_HOUR_INTO_FRIDAY_MS, should_stop=lambda: False
        )

    assert (caught.value.reason, failure_of(caught.value).items[0].session_open_ms) == (
        "session_not_over",
        FRIDAYS_OPEN_MS,
    )
    assert asked == [(WED, THU)], "the chunk of the two finished sessions was fetched, and the chunk of Friday was not"
    assert sorted(vendor.stock_requests) == [WED, THU]


@respx.mock
async def test_a_range_of_finished_sessions_is_asked_of_the_stock_pipeline_in_one_chunk_as_before(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor(_scene(*THREE_SESSIONS))
    vendor.mount()
    real = fetching.materialize_engine_run
    asked: list[tuple[date, date]] = []

    def delegating(*, symbol: str, start: date, end: date, **kwargs: Any) -> Any:
        asked.append((start, end))
        return real(symbol=symbol, start=start, end=end, **kwargs)

    monkeypatch.setattr(fetching, "materialize_engine_run", delegating)

    report, _ = await _fetch(_same_day_call(), THREE_SESSIONS)

    assert asked == [(WED, FRI)] and report.sessions_prepared == 3
    assert sorted(vendor.stock_requests) == THREE_SESSIONS


# ── What the phase breaks when it is broken ──────────────────────────────


@respx.mock
async def test_the_listing_is_read_once_for_a_session_and_handed_to_every_contract_capture(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A capture proves its contract is listed from the stored listing. Handed the listing it reads nothing; left to
    # itself it reads and parses the whole listing for each of the eight contracts.
    reads: list[date] = []
    real_read = option_bars.read_option_listing

    def counted(underlying: str, day: date, **kwargs: Any) -> Any:
        reads.append(day)
        return real_read(underlying, day, **kwargs)

    monkeypatch.setattr(option_bars, "read_option_listing", counted)
    Vendor({WED: ladder_day()}).mount()

    await _fetch(eight_legs_setup(), [WED])

    assert reads == []


@respx.mock
async def test_the_requests_go_underlying_then_listing_then_contracts_and_contracts_in_symbol_order(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()}, option_latency_s=0.05)
    vendor.mount()

    await _fetch(eight_legs_setup(), [WED, THU])

    def kind(request: str) -> str:
        return "contract" if "/O:" in request else "listing" if "contracts" in request else "stock"

    order = [kind(request) for request in vendor.requests]
    assert order == ["stock", "stock", "listing", *["contract"] * 8, "listing", *["contract"] * 8]
    # Wednesday's contracts: the first four to be asked are the first four in symbol order.
    wednesday = [symbol for symbol in vendor.option_requests if symbol.startswith("SPY261007")]
    assert sorted(wednesday[:4]) == sorted(wednesday)[:4]
    assert len(wednesday) == 8


@respx.mock
async def test_an_error_inside_a_batch_of_contracts_stops_the_phase_and_is_not_swallowed(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The catalog stops answering as the third contract of eight is asked for. The phase must end with that error: a
    # phase that ended as if it had succeeded would leave the replay to find a gap.
    real = fetching.ensure_option_day_bars

    async def the_catalog_goes_down(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        if contract.occ_symbol == "SPY261007C00498000":
            raise CatalogUnavailableError("the catalog is down")
        return await real(contract, *args, **kwargs)

    monkeypatch.setattr(fetching, "ensure_option_day_bars", the_catalog_goes_down)
    Vendor({WED: ladder_day()}, option_latency_s=0.05).mount()

    with pytest.raises(CatalogUnavailableError):
        await _fetch(eight_legs_setup(), [WED])


@respx.mock
async def test_an_underlying_still_missing_after_its_fetch_is_a_failure_naming_the_session(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lake said nothing went wrong and holds no bars: the phase may not go on to read a day that is not there.
    monkeypatch.setattr(fetching, "materialize_engine_run", lambda **kwargs: None)
    vendor = Vendor(_scene(WED))
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _fetch(_same_day_call(), [WED])

    assert (caught.value.session, caught.value.reason) == (WED, "not_in_the_lake")
    assert "SPY" in caught.value.what
    assert vendor.option_requests == []


@respx.mock
async def test_a_fully_stored_session_asks_the_lake_to_capture_nothing_and_the_vendor_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    await _fetch(eight_legs_setup(), [WED])
    captures: list[str] = []
    real = fetching.ensure_option_day_bars

    async def counted(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        captures.append(contract.occ_symbol)
        return await real(contract, *args, **kwargs)

    monkeypatch.setattr(fetching, "ensure_option_day_bars", counted)
    asked = vendor.vendor_requests

    report, _ = await _fetch(eight_legs_setup(), [WED])

    assert captures == [], "the lake was asked once which days are stored, and had nothing left to capture"
    assert vendor.vendor_requests == asked
    assert (report.option_days_fetched, report.option_days_reused) == (0, 8)


@respx.mock
async def test_only_the_contract_days_not_stored_are_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    # Three of the eight are stored: the strikes 496, 497 and 498.
    await _fetch(eight_legs_setup(steps=(-4, -3, -2)), [WED])
    assert len(vendor.option_requests) == 3
    captures: list[str] = []
    real = fetching.ensure_option_day_bars

    async def counted(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        captures.append(contract.occ_symbol)
        return await real(contract, *args, **kwargs)

    monkeypatch.setattr(fetching, "ensure_option_day_bars", counted)

    report, _ = await _fetch(eight_legs_setup(), [WED])

    assert len(captures) == 5 and len(vendor.option_requests) == 3 + 5
    assert not {"SPY261007C00496000", "SPY261007C00497000", "SPY261007C00498000"} & set(captures)
    assert (report.option_days_fetched, report.option_days_reused) == (5, 3)


@respx.mock
async def test_a_day_the_catalog_names_as_captured_whose_file_does_not_read_is_captured_again(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check said such a file "will be fetched again", and the replay would fail on it: so the phase repairs it.
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    await _fetch(eight_legs_setup(steps=(-4, -3)), [WED])
    [damaged] = [row for row in fake_catalog.rows.values() if row["symbol"] == "SPY261007C00496000"]
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), damaged["file_path"], b"not a zip file", admit=False)
    asked = len(vendor.option_requests)

    report, _ = await _fetch(eight_legs_setup(steps=(-4, -3)), [WED])

    assert len(vendor.option_requests) == asked + 1, "only the damaged day was fetched again"
    assert (report.option_days_fetched, report.option_days_reused) == (1, 1)
    contract = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=496)
    assert await asyncio.to_thread(LakeRunData("SPY").option_day, contract, WED) is not None


def test_a_download_failure_names_what_failed_and_no_date_in_its_own_text() -> None:
    failure = DownloadFailed(session=WED, what="the bars of SPY261007C00500000", reason="x", detail="2026-10-07 y")

    assert str(failure) == "the bars of SPY261007C00500000 could not be fetched (x)"


@respx.mock
async def test_the_underlying_fetch_says_what_it_is_doing_while_the_progress_is_still_at_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The underlying is the first thing fetched and the longest wait, and progress is counted in sessions from after
    # it. The job's log says which sessions' bars are being fetched and when they are in, with no date.
    monkeypatch.setattr(fetching, "UNDERLYING_CHUNK_SESSIONS", 2)
    Vendor(_scene(*THREE_SESSIONS)).mount()
    lines: list[str] = []

    await _fetch(_same_day_call(), THREE_SESSIONS, on_log=lines.append)

    underlying = [line for line in lines if "minute bars" in line]
    assert underlying == [
        "Fetching SPY minute bars for sessions 1 to 2 of 3.",
        "SPY minute bars are in the lake for 2 of 3 sessions.",
        "Fetching SPY minute bars for sessions 3 to 3 of 3.",
        "SPY minute bars are in the lake for 3 of 3 sessions.",
    ]
    for line in lines:
        assert_plain(line)
