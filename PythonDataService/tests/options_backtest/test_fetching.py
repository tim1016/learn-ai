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
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx

from app.lean_sidecar.trading_calendar import expected_sessions
from app.options.contract import OptionContract
from app.options_backtest import fetching
from app.options_backtest.fetching import (
    MAX_VENDOR_REQUESTS_IN_FLIGHT,
    DownloadFailed,
    DownloadStopped,
    ensure_run_data,
)
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.setup import MS_PER_MINUTE
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import FRI, HALF_DAY, THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import leg, setup

# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000
D = Decimal
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


def test_the_underlying_is_asked_for_in_chunks_of_sessions_each_with_its_own_stop_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[date, date]] = []
    monkeypatch.setattr(
        fetching,
        "materialize_engine_run",
        lambda *, symbol, start, end, resolution, price_adjustment_mode: calls.append((start, end)),
    )
    monkeypatch.setattr(fetching, "UNDERLYING_CHUNK_SESSIONS", 2)
    five = [date(2026, 10, 5), date(2026, 10, 6), WED, THU, FRI]

    with pytest.raises(DownloadStopped) as caught:
        # Stop once all three chunks are asked for: at the first session.
        ensure_run_data(_same_day_call(), five, now_ms=NOW_MS, should_stop=lambda: len(calls) >= 3)

    assert calls == [(date(2026, 10, 5), date(2026, 10, 6)), (WED, THU), (FRI, FRI)]
    assert caught.value.sessions_done == 0


def test_a_stop_before_the_first_chunk_asks_the_lake_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[date, date]] = []
    monkeypatch.setattr(
        fetching, "materialize_engine_run", lambda *, symbol, start, end, **kwargs: calls.append((start, end))
    )

    with pytest.raises(DownloadStopped):
        ensure_run_data(_same_day_call(), THREE_SESSIONS, now_ms=NOW_MS, should_stop=lambda: True)

    assert calls == []


def test_the_underlying_is_asked_for_twenty_five_sessions_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[date, date]] = []
    monkeypatch.setattr(
        fetching,
        "materialize_engine_run",
        lambda *, symbol, start, end, resolution, price_adjustment_mode: calls.append((start, end)),
    )
    sessions = expected_sessions(date(2026, 8, 3), date(2026, 9, 30))[:26]
    assert len(sessions) == 26

    with pytest.raises(DownloadStopped):
        ensure_run_data(_same_day_call(), sessions, now_ms=NOW_MS, should_stop=lambda: len(calls) >= 2)

    # The first twenty-five sessions are one call, the twenty-sixth the next.
    assert calls == [(sessions[0], sessions[24]), (sessions[25], sessions[25])]
