"""The price series of a saved session, read from a small lake and checked against what the run read (#3030).

The run is the three-session one of ``_detail_scene.py``, made over a lake in a temporary folder with the lake's own
writers, so each pin is the sha256 of a real stored file. Hand-worked figures:

* Wednesday's underlying closed at 500.20 in every minute from m1 to m390: 390 points, the first closing at the open
  plus one minute and the last at the close.
* Wednesday's call 500 has bars from m5 to m61 except m40 and m41: 57 minutes less 2 = 55 points. Its closes at the turns
  are 4.00 (m5), 4.10 (m9), 5.00 (m10), 5.05 (m11), 5.00 (m19), 4.00 (m20), 3.80 (m21), 3.70 (m29), 4.80 (m30), 4.70
  (m31), 4.60 (m39), 4.60 (m42), 4.60 (m60), 4.50 (m61).
* Wednesday's put 500 has bars from m5 to m61: 57 points; 2.00 at m5, 2.02 from m6 to m60, 2.12 at m61.
* Wednesday's put 495 has bars at m0, m5 to m60 and m391, m392. Only m5 to m60 are across the session: 56 points of 1.00.
  The bar closing at the open (m0) and the two closing after the close are in the lake and are not drawn.
* Thursday's underlying has no bar at m5: 389 points. Friday's call 500 has bars from m5 to m60: 56 points, 4.00 at m5
  and 4.10 after; its put 495 has the same 56 as Wednesday's.

Every reason a series is not shown has words of its own, held to the reason by ``test_each_reason_has_its_own_words``.
A file the operating system refuses (a permission, a folder) is ``unreadable`` for a contract as for the underlying, and a
day holding a bar no session can place is ``unreadable`` too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.options.contract import OptionContract
from app.options_backtest import lake_data
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.results import SessionStatus, SkipReason
from app.options_backtest.run import run_sessions
from app.options_backtest.session_result import PinnedDay, SessionPins, SessionResult
from app.options_backtest.session_series import (
    SERIES_WORDS,
    Series,
    SeriesReason,
    SessionSeries,
    contracts_to_chart,
    read_session_series,
)
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._detail_scene import (
    CALL_500,
    FRI_CALL_500,
    FRI_PUT_495,
    PUT_495,
    PUT_500,
    SESSIONS,
    THU_OPEN_MS,
    WED_OPEN_MS,
    minute,
    run_the_scene,
    the_scene,
    the_setup,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import (
    option_day_zip,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    stock_day_zip,
)
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import HALF_DAY, WED, Day, fake_data, steady
from tests.options_backtest.setup_fixtures import setup_body

D = Decimal
ET = ZoneInfo("America/New_York")
STOCK_FILE = "equity/usa/minute/spy/20261007_trade.zip"


def _bars_file(symbol: str) -> str:
    return f"option/usa/minute/spy/{symbol.lower()}/20261007_trade.zip"


async def _series_of(session: SessionResult) -> SessionSeries:
    # The reader blocks on files and on the catalog, and refuses the event loop's thread.
    return await asyncio.to_thread(read_session_series, session, run_id=41, underlying="SPY", data=LakeRunData("SPY"))


def _by_symbol(series: SessionSeries) -> dict[str, Series]:
    return {each.symbol: each for each in series.contracts}


def _minutes(series: Series, open_ms: int) -> list[int]:
    return [(point.at_ms - open_ms) // 60_000 for point in series.points]


def _closes_at(series: Series, open_ms: int, turns: dict[int, str]) -> None:
    by_minute = {(point.at_ms - open_ms) // 60_000: point.close for point in series.points}
    assert {at: by_minute[at] for at in turns} == {at: D(price) for at, price in turns.items()}


# ── What is drawn ────────────────────────────────────────────────────────


@respx.mock
async def test_the_busy_days_series_are_the_closes_the_lake_holds_with_a_gap_where_nothing_traded(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()

    series = await _series_of(result.sessions[0])

    underlying = series.underlying
    assert underlying.shown and underlying.contract is None and underlying.symbol == "SPY"
    assert len(underlying.points) == 390
    assert _minutes(underlying, WED_OPEN_MS) == list(range(1, 391))
    assert {point.close for point in underlying.points} == {D("500.20")}
    assert underlying.points[-1].at_ms == result.sessions[0].session_close_ms

    assert [each.symbol for each in series.contracts] == [CALL_500, PUT_495, PUT_500]
    contracts = _by_symbol(series)
    call = contracts[CALL_500]
    assert call.shown and call.contract is not None and call.contract.occ_symbol == CALL_500
    # Nothing traded in m40 and m41, and nothing is filled in or carried across them.
    assert _minutes(call, WED_OPEN_MS) == [m for m in range(5, 62) if m not in (40, 41)]
    _closes_at(
        call,
        WED_OPEN_MS,
        {5: "4.00", 9: "4.10", 10: "5.00", 11: "5.05", 19: "5.00", 20: "4.00", 21: "3.80", 29: "3.70", 30: "4.80",
         31: "4.70", 39: "4.60", 42: "4.60", 60: "4.60", 61: "4.50"},
    )  # fmt: skip
    put = contracts[PUT_500]
    assert _minutes(put, WED_OPEN_MS) == list(range(5, 62))
    _closes_at(put, WED_OPEN_MS, {5: "2.00", 6: "2.02", 7: "2.02", 60: "2.02", 61: "2.12"})
    assert [point.at_ms for point in call.points][:2] == [minute(WED_OPEN_MS, 5), minute(WED_OPEN_MS, 6)]


@respx.mock
async def test_a_bar_closing_at_the_open_or_after_the_close_is_in_the_lake_and_is_not_drawn(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()

    series = await _series_of(result.sessions[0])

    # The 495 put's file holds bars closing at m0 (the open itself), m391 and m392 as well as m5 to m60.
    lag = _by_symbol(series)[PUT_495]
    assert _minutes(lag, WED_OPEN_MS) == list(range(5, 61))
    assert {point.close for point in lag.points} == {D("1.00")}


@respx.mock
async def test_a_contract_that_only_took_a_starting_price_has_a_series_and_one_that_was_only_picked_has_none(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    friday = result.sessions[2]

    series = await _series_of(friday)

    # Friday's put 500 was picked and its buy dropped; the call and the 495 put took a starting price. No trade at all.
    assert friday.outcome.trades == ()
    assert [each.symbol for each in series.contracts] == [FRI_CALL_500, FRI_PUT_495]
    call, lag = series.contracts
    assert _minutes(call, friday.session_open_ms) == list(range(5, 61)) and _minutes(
        lag, friday.session_open_ms
    ) == list(range(5, 61))
    _closes_at(call, friday.session_open_ms, {5: "4.00", 6: "4.10", 60: "4.10"})


@respx.mock
async def test_a_session_skipped_for_want_of_an_underlying_price_still_has_the_underlyings_series_and_no_contract(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    thursday = result.sessions[1]

    series = await _series_of(thursday)

    assert series.contracts == ()
    assert series.underlying.shown
    assert _minutes(series.underlying, THU_OPEN_MS) == [m for m in range(1, 391) if m != 5]


def test_the_contracts_to_chart_are_those_traded_or_given_a_starting_price_by_their_symbols() -> None:
    # Pure: Wednesday's outcome names three contracts (the call twice) and Friday's the call and the 495 put.
    result = run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))

    assert [c.occ_symbol for c in contracts_to_chart(result.sessions[0].outcome)] == [CALL_500, PUT_495, PUT_500]
    assert [c.occ_symbol for c in contracts_to_chart(result.sessions[1].outcome)] == []
    assert [c.occ_symbol for c in contracts_to_chart(result.sessions[2].outcome)] == [FRI_CALL_500, FRI_PUT_495]


# ── The pin check ────────────────────────────────────────────────────────


def _reasons(series: SessionSeries) -> dict[str, SeriesReason | None]:
    return {
        series.underlying.symbol: series.underlying.reason,
        **{each.symbol: each.reason for each in series.contracts},
    }


@respx.mock
async def test_a_contract_file_replaced_by_other_bytes_is_not_served_and_the_rest_still_is(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    other_bars = build_minute_trade_zip_bytes(
        symbol=PUT_500,
        trading_date_yyyymmdd="20261007",
        bars=[
            MinuteTradeBar(datetime(2026, 10, 7, 9, 30, tzinfo=ET), D("2.05"), D("2.05"), D("2.05"), D("2.05"), D(5))
        ],
    )
    # The catalog is given a receipt for the new bytes, so they read cleanly: only the pin tells them from the old.
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), _bars_file(PUT_500), other_bars, admit=True)

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {"SPY": None, CALL_500: None, PUT_495: None, PUT_500: SeriesReason.CHANGED}
    changed = _by_symbol(series)[PUT_500]
    assert not changed.shown and changed.points == () and changed.contract is not None
    assert len(_by_symbol(series)[CALL_500].points) == 55


@respx.mock
async def test_bytes_the_catalog_does_not_admit_cannot_be_confirmed_for_the_underlying_or_for_a_contract(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_FILE, b"changed after its receipt", admit=False)
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), _bars_file(CALL_500), b"changed after its receipt", admit=False
    )

    series = await _series_of(result.sessions[0])

    # Nobody saw these files change: the catalog holds no receipt for the bytes in them, so they cannot be checked against
    # what the run read. That is not "changed".
    assert _reasons(series) == {
        "SPY": SeriesReason.NOT_CONFIRMED,
        CALL_500: SeriesReason.NOT_CONFIRMED,
        PUT_495: None,
        PUT_500: None,
    }


@respx.mock
async def test_a_file_that_is_gone_is_not_served_whichever_series_it_is(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    (raw_root(tmp_lake) / STOCK_FILE).unlink()
    (raw_root(tmp_lake) / _bars_file(PUT_495)).unlink()

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {
        "SPY": SeriesReason.GONE,
        CALL_500: None,
        PUT_495: SeriesReason.GONE,
        PUT_500: None,
    }
    assert series.underlying.points == () and _by_symbol(series)[PUT_495].points == ()


@respx.mock
async def test_a_damaged_file_that_the_catalog_admits_is_unreadable(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    result = await run_the_scene()
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_FILE, b"admitted, but not a zip", admit=True)
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), _bars_file(PUT_500), b"admitted, but not a zip", admit=True
    )

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {
        "SPY": SeriesReason.UNREADABLE,
        CALL_500: None,
        PUT_495: None,
        PUT_500: SeriesReason.UNREADABLE,
    }


@respx.mock
async def test_each_series_withheld_as_unreadable_is_logged_once_with_the_kind_of_fault_and_no_path_and_a_shown_one_is_not(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    result = await run_the_scene()
    # Wednesday has four series: SPY, and the contracts call 500, put 495 and put 500. Two cannot be read, one a damaged
    # underlying file the catalog admits, the other an operating system error on call 500 (its text carries a made-up
    # server path). The other two are shown, so the log holds two warnings and none for them.
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_FILE, b"admitted, but not a zip", admit=True)
    real = lake_data.read_option_day_bars

    def refuse_one(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        if contract.occ_symbol == CALL_500:
            raise PermissionError(13, "Permission denied", "/a/server/path/lake/raw/option/usa/minute/spy/x.zip")
        return real(contract, *args, **kwargs)

    monkeypatch.setattr(lake_data, "read_option_day_bars", refuse_one)
    caplog.set_level(logging.WARNING, logger="app.options_backtest.session_series")

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {
        "SPY": SeriesReason.UNREADABLE,
        CALL_500: SeriesReason.UNREADABLE,
        PUT_495: None,
        PUT_500: None,
    }
    warned = [
        record for record in caplog.records if getattr(record, "action", None) == "options_backtest_series_unreadable"
    ]
    # Run 41 (what ``_series_of`` reads under) and the Wednesday that opened at 1_791_379_800_000 (2026-10-07 09:30 ET).
    assert sorted((r.series, r.error_type, r.run_id, r.session_open_ms) for r in warned) == [
        ("SPY", "UnderlyingDayUnreadable", 41, 1_791_379_800_000),
        (CALL_500, "PermissionError", 41, 1_791_379_800_000),
    ]
    # Nothing of the error's own text, which names the file by its path on the server's disk, is in any record.
    assert not any("/a/server/path" in str(vars(record)) for record in warned)


@respx.mock
async def test_a_catalog_that_cannot_be_asked_is_the_lakes_fault_and_says_nothing_of_any_file(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_the_scene()

    async def down(*args: object, **kwargs: object) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)
    (raw_root(tmp_lake) / _bars_file(PUT_495)).unlink()

    series = await _series_of(result.sessions[0])

    # A file that is not there is gone whatever the catalog says; every file that is there cannot be judged.
    assert _reasons(series) == {
        "SPY": SeriesReason.LAKE_UNAVAILABLE,
        CALL_500: SeriesReason.LAKE_UNAVAILABLE,
        PUT_495: SeriesReason.GONE,
        PUT_500: SeriesReason.LAKE_UNAVAILABLE,
    }


@respx.mock
async def test_a_pin_that_is_not_the_files_hash_is_a_changed_file_though_nothing_in_the_lake_moved(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    session = result.sessions[0]
    wrong = "0" * 64
    pins = session.pins
    tampered = replace(
        session,
        pins=SessionPins(
            underlying_sha256=wrong,
            listing_sha256=pins.listing_sha256,
            option_days=tuple(
                PinnedDay(day.contract, wrong if day.contract.occ_symbol == CALL_500 else day.sha256)
                for day in pins.option_days
            ),
        ),
    )

    series = await _series_of(tampered)

    assert _reasons(series) == {
        "SPY": SeriesReason.CHANGED,
        CALL_500: SeriesReason.CHANGED,
        PUT_495: None,
        PUT_500: None,
    }


@respx.mock
async def test_a_series_the_run_kept_no_pin_for_is_not_served_unchecked(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    session = result.sessions[0]
    pins = session.pins
    # A session skipped on its window pinned nothing at all; a contract the pins do not name has no pin either.
    nothing = replace(session, pins=SessionPins(None, pins.listing_sha256, ()))
    one_missing = replace(
        session,
        pins=SessionPins(
            pins.underlying_sha256,
            pins.listing_sha256,
            tuple(d for d in pins.option_days if d.contract.occ_symbol != PUT_500),
        ),
    )

    nothing_series = await _series_of(nothing)
    one_series = await _series_of(one_missing)

    assert set(_reasons(nothing_series).values()) == {SeriesReason.NO_PIN}
    assert _reasons(one_series) == {"SPY": None, CALL_500: None, PUT_495: None, PUT_500: SeriesReason.NO_PIN}


def test_every_reason_has_words_and_none_of_them_names_a_date_a_path_or_a_file() -> None:
    assert set(SERIES_WORDS) == set(SeriesReason)
    for words in SERIES_WORDS.values():
        assert words and "20" not in words and "/" not in words and ".zip" not in words


def test_each_reason_has_its_own_words_which_say_only_what_is_true_of_it() -> None:
    # Each text is written out against its reason, so two texts swapped, or one reason given another's, fail here.
    assert {reason.value: words for reason, words in SERIES_WORDS.items()} == {
        "session_skipped": "This session was skipped, so no bars were read for it.",
        "no_pin": "The run kept no record of reading these bars, so there is nothing to check them against.",
        "gone": "The stored bars are no longer in the lake.",
        "changed": "The stored bars have changed since this run.",
        "not_confirmed": "The stored bars cannot be confirmed against what this run read.",
        "unreadable": "The stored bars cannot be read now.",
        "lake_unavailable": "The lake could not be reached, so the bars cannot be checked against this run now.",
    }
    assert len(set(SERIES_WORDS.values())) == len(SeriesReason), "two reasons share their words"


# ── A file the operating system refuses, and a day no session can place ──


@contextmanager
def _refused(path: Path) -> Iterator[None]:
    """``path`` (a file or a folder) with no permission at all; its permissions come back afterwards."""
    before = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        yield
    finally:
        path.chmod(before)


def _the_file_is_refused(path: Path) -> None:
    with pytest.raises(PermissionError):
        path.read_bytes()


@respx.mock
async def test_a_contract_file_the_operating_system_refuses_is_unreadable_and_the_rest_still_is(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    file = raw_root(tmp_lake) / _bars_file(PUT_500)

    with _refused(file):
        _the_file_is_refused(file)  # the permission took: this run is not one that bypasses it
        series = await _series_of(result.sessions[0])

    assert _reasons(series) == {"SPY": None, CALL_500: None, PUT_495: None, PUT_500: SeriesReason.UNREADABLE}
    assert _by_symbol(series)[PUT_500].points == ()


@respx.mock
async def test_a_contract_folder_the_operating_system_refuses_is_unreadable_and_the_rest_still_is(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    folder = (raw_root(tmp_lake) / _bars_file(PUT_500)).parent

    with _refused(folder):
        _the_file_is_refused(folder / "20261007_trade.zip")
        series = await _series_of(result.sessions[0])

    assert _reasons(series) == {"SPY": None, CALL_500: None, PUT_495: None, PUT_500: SeriesReason.UNREADABLE}


@respx.mock
async def test_a_contract_read_that_raises_an_operating_system_error_is_unreadable_and_names_no_path(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await run_the_scene()
    real = lake_data.read_option_day_bars

    def refuse_one(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        if contract.occ_symbol == CALL_500:
            raise PermissionError(13, "Permission denied", "/a/server/path/lake/raw/option/usa/minute/spy/x.zip")
        return real(contract, *args, **kwargs)

    monkeypatch.setattr(lake_data, "read_option_day_bars", refuse_one)

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {"SPY": None, CALL_500: SeriesReason.UNREADABLE, PUT_495: None, PUT_500: None}
    # What reaches the page is the reason's fixed words: the error text, with its path, goes nowhere.
    assert "/a/server/path" not in SERIES_WORDS[SeriesReason.UNREADABLE]


@respx.mock
async def test_the_underlying_file_the_operating_system_refuses_is_unreadable_too(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    file = raw_root(tmp_lake) / STOCK_FILE

    with _refused(file):
        _the_file_is_refused(file)
        series = await _series_of(result.sessions[0])

    assert _reasons(series) == {"SPY": SeriesReason.UNREADABLE, CALL_500: None, PUT_495: None, PUT_500: None}


@respx.mock
async def test_a_stored_day_holding_a_bar_no_session_can_place_is_an_unreadable_series_for_a_contract_and_the_underlying(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    # A bar starting half a minute past the minute closes off a whole minute, and the lake reader refuses the whole day.
    # The stored files are put in place of the captured ones with the catalog's receipt for them, so they read cleanly:
    # only the bar tells them from a good day. Minute m5 starts at the open plus five minutes.
    put = OptionContract.from_occ_symbol(PUT_500)
    off_the_minute = [WED_OPEN_MS + 5 * MS_PER_MINUTE, WED_OPEN_MS + 6 * MS_PER_MINUTE + 30_000]
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), _bars_file(PUT_500), option_day_zip(put, off_the_minute, day=WED), admit=True
    )
    replace_the_stored_file(
        fake_catalog,
        raw_root(tmp_lake),
        STOCK_FILE,
        stock_day_zip(
            [
                stock_bar(WED_OPEN_MS + MS_PER_MINUTE, "500.20", "500.20", "500.20", "500.20"),
                stock_bar(WED_OPEN_MS + 2 * MS_PER_MINUTE + 30_000, "500.20", "500.20", "500.20", "500.20"),
            ],
            day=WED,
        ),
        admit=True,
    )

    series = await _series_of(result.sessions[0])

    assert _reasons(series) == {
        "SPY": SeriesReason.UNREADABLE,
        CALL_500: None,
        PUT_495: None,
        PUT_500: SeriesReason.UNREADABLE,
    }
    assert series.underlying.points == () and _by_symbol(series)[PUT_500].points == ()
    assert len(_by_symbol(series)[CALL_500].points) == 55


# ── A session skipped on its window read nothing ─────────────────────────


async def test_a_session_skipped_on_its_window_has_no_series_and_says_the_session_was_skipped_not_that_a_record_is_missing() -> (
    None
):
    # Entry 190 minutes after the open and exit 30 before the close: 190 to 360 on a full day, 190 to 180 on a half day.
    written = OptionsBacktestSetup.model_validate(
        setup_body(
            entry={"anchor": "open", "offset_ms": 190 * MS_PER_MINUTE},
            exit={"anchor": "close", "offset_ms": 30 * MS_PER_MINUTE},
        )
    )
    result = run_sessions(written, [HALF_DAY], fake_data({HALF_DAY: Day(underlying=steady("500.20"))}))
    [session] = result.sessions
    assert (session.outcome.status, session.outcome.skip_reason) == (
        SessionStatus.SKIPPED,
        SkipReason.WINDOW_DOES_NOT_FIT,
    )

    # Nothing is read for it, so no lake needs to be set up: the series is judged from what the run kept.
    series = await _series_of(session)

    assert series.contracts == ()
    assert (series.underlying.reason, series.underlying.points) == (SeriesReason.SESSION_SKIPPED, ())
