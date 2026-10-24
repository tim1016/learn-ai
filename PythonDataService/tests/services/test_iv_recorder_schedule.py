"""The IV30 recorder's clock (#3525): when it fires, that it fires once a day, and that a failure never stops it.

Every test drives the schedule with a fake clock and a fake recorder that writes into an in-memory store. No test asks a
vendor for anything, and none waits: the loop's sleep is injected.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.data_lake.capture_finality import VENDOR_DELAY_MS
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.option_archive.planner import UNDERLYINGS
from app.services import iv_recorder_schedule
from app.services.iv_recorder import InMemoryIvSnapshotStore, RecordedIvSnapshot
from app.services.iv_recorder_schedule import (
    CATCH_UP_MS,
    CLOSE_SLOT,
    TICK_SECONDS,
    IvRecorderSchedule,
    parse_symbols,
    snapshot_day_at,
    snapshot_due_ms,
)
from app.utils.timestamps import to_ms_utc

ET = ZoneInfo("America/New_York")

MONDAY = date(2024, 3, 11)  # an ordinary session, in daylight time
AFTER_FALL_BACK = date(2024, 11, 4)  # an ordinary session, in standard time
EARLY_CLOSE = date(2024, 7, 3)  # closes at 13:00 ET
HOLIDAY = date(2024, 7, 4)
SATURDAY = date(2024, 3, 9)


def at_et(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).timestamp() * 1000)


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def row(ticker: str, ts: int, *, slot: str = CLOSE_SLOT, error: str | None = None) -> RecordedIvSnapshot:
    return RecordedIvSnapshot(
        ticker=ticker, snapshot_ts_ms=ts, slot=slot, spot=0.0 if error else 500.0, rate=0.05, dividend_yield=0.0,
        rate_source="test", dividend_source="test", iv30_vix_style=None if error else 0.2, iv30_parametric=None,
        iv_provenance={}, raw_chain=[], error=error,
    )


class Recorder:
    """The recorder the schedule calls: it writes the row the real one would, and remembers what it was asked."""

    def __init__(self, store: InMemoryIvSnapshotStore) -> None:
        self.store = store
        self.calls: list[tuple[str, int]] = []
        self.fail: set[str] = set()
        self.file_error_row: set[str] = set()

    def __call__(self, ticker: str, asof: datetime) -> RecordedIvSnapshot:
        ts = to_ms_utc(asof)
        self.calls.append((ticker, ts))
        if ticker in self.fail:
            raise RuntimeError("vendor down")
        written = row(ticker, ts, error="polygon_fetch_failed: 502" if ticker in self.file_error_row else None)
        self.store.write(written)
        return written


async def no_sleep(_: float) -> None:
    """A sleep that waits for nothing but still lets the loop run other tasks."""
    await asyncio.sleep(0)


def schedule_for(symbols: tuple[str, ...], clock: Clock, store: InMemoryIvSnapshotStore) -> tuple[IvRecorderSchedule, Recorder]:
    recorder = Recorder(store)
    return IvRecorderSchedule(symbols=symbols, store=store, record=recorder, clock=clock, sleep=no_sleep), recorder


# ---------------------------------------------------------------------------
# When: the calendar's close, plus the vendor's delay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "hour", "minute"),
    [
        pytest.param(MONDAY, 16, 15, id="ordinary session"),
        pytest.param(AFTER_FALL_BACK, 16, 15, id="standard time"),
        pytest.param(EARLY_CLOSE, 13, 15, id="early close"),
    ],
)
def test_the_snapshot_is_taken_a_vendor_delay_after_the_calendar_close(day, hour, minute):
    assert snapshot_due_ms(day) == session_close_ms_utc(day) + VENDOR_DELAY_MS
    assert snapshot_due_ms(day) == at_et(day, hour, minute)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(at_et(MONDAY, 16, 14), None, id="a minute before the instant"),
        pytest.param(at_et(MONDAY, 16, 15), MONDAY, id="at the instant"),
        pytest.param(at_et(MONDAY, 16, 15) + CATCH_UP_MS - 1, MONDAY, id="the last moment it catches up"),
        pytest.param(at_et(MONDAY, 16, 15) + CATCH_UP_MS, None, id="too late: the quotes froze at the close"),
        pytest.param(at_et(EARLY_CLOSE, 13, 14), None, id="early close, before its instant"),
        pytest.param(at_et(EARLY_CLOSE, 13, 15), EARLY_CLOSE, id="early close, at its instant"),
        pytest.param(at_et(EARLY_CLOSE, 16, 15), None, id="early close: the ordinary instant is long past its window"),
        pytest.param(at_et(SATURDAY, 16, 30), None, id="a weekend"),
        pytest.param(at_et(HOLIDAY, 16, 30), None, id="a holiday"),
    ],
)
def test_a_day_is_wanted_from_its_instant_until_the_catch_up_window_ends(now, expected):
    assert snapshot_day_at(now) == expected


# ---------------------------------------------------------------------------
# The clock fires on trading days, at the calendar's instant
# ---------------------------------------------------------------------------


async def test_the_schedule_records_each_symbol_when_the_instant_comes_and_not_before():
    clock = Clock(at_et(MONDAY, 16, 14))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())

    await schedule.tick()
    assert recorder.calls == []

    clock.now = at_et(MONDAY, 16, 15)
    await schedule.tick()

    assert recorder.calls == [("SPY", clock.now), ("QQQ", clock.now)], "each row is stamped with the moment of its snapshot"
    for ticker in ("SPY", "QQQ"):
        (stored,) = schedule.store.read_series(ticker)
        assert stored.slot == CLOSE_SLOT and stored.snapshot_ts_ms == clock.now


async def test_the_schedule_fires_nothing_on_a_weekend_or_a_holiday():
    schedule, recorder = schedule_for(("SPY",), Clock(0), InMemoryIvSnapshotStore())
    for day in (SATURDAY, HOLIDAY):
        for hour in range(0, 24):
            schedule.clock.now = at_et(day, hour, 30)
            await schedule.tick()

    assert recorder.calls == []


async def test_an_early_close_moves_the_instant_with_it():
    clock = Clock(at_et(EARLY_CLOSE, 13, 14))
    schedule, recorder = schedule_for(("SPY",), clock, InMemoryIvSnapshotStore())

    await schedule.tick()
    assert recorder.calls == []

    clock.now = at_et(EARLY_CLOSE, 13, 15)
    await schedule.tick()
    assert recorder.calls == [("SPY", clock.now)]


async def test_a_process_that_starts_after_the_window_does_not_record_the_day():
    """The chain's quotes froze at the close while the underlying kept printing; a late snapshot would pair the two."""
    schedule, recorder = schedule_for(("SPY",), Clock(at_et(MONDAY, 16, 15) + CATCH_UP_MS), InMemoryIvSnapshotStore())

    await schedule.tick()

    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Once a day, across a restart
# ---------------------------------------------------------------------------


async def test_repeated_ticks_in_a_window_record_each_symbol_once():
    clock = Clock(at_et(MONDAY, 16, 15))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())

    for minutes in (0, 1, 30, 100):
        clock.now = at_et(MONDAY, 16, 15) + minutes * 60_000
        await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["SPY", "QQQ"]


async def test_a_restart_in_the_window_does_not_record_a_symbol_the_store_already_holds_for_the_day():
    store = InMemoryIvSnapshotStore()
    clock = Clock(at_et(MONDAY, 16, 15))
    first, first_recorder = schedule_for(("SPY", "QQQ"), clock, store)
    await first.tick()
    assert len(first_recorder.calls) == 2

    clock.now = at_et(MONDAY, 17, 0)
    restarted, restarted_recorder = schedule_for(("SPY", "QQQ"), clock, store)
    await restarted.tick()

    assert restarted_recorder.calls == []
    assert len(store.read_series("SPY")) == 1 and len(store.read_series("QQQ")) == 1


async def test_a_symbol_the_store_lacks_is_recorded_when_its_neighbour_is_already_held():
    store = InMemoryIvSnapshotStore()
    store.write(row("SPY", at_et(MONDAY, 16, 20)))
    schedule, recorder = schedule_for(("SPY", "QQQ"), Clock(at_et(MONDAY, 16, 40)), store)

    await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["QQQ"]


@pytest.mark.parametrize(
    ("existing", "recorded"),
    [
        pytest.param(row("SPY", at_et(MONDAY, 16, 20), slot="15:55"), True, id="another slot is not the close"),
        pytest.param(row("SPY", at_et(MONDAY, 16, 0)), True, id="a close-slot row from before the instant shows the earlier quotes"),
        pytest.param(row("SPY", at_et(date(2024, 3, 8), 16, 20)), True, id="yesterday's close"),
        pytest.param(row("SPY", at_et(MONDAY, 16, 20), error="replication_failed: x"), False, id="an error row is the day's attempt"),
    ],
)
async def test_only_the_days_close_slot_row_after_the_instant_counts_as_recorded(existing, recorded):
    store = InMemoryIvSnapshotStore()
    store.write(existing)
    schedule, recorder = schedule_for(("SPY",), Clock(at_et(MONDAY, 16, 40)), store)

    await schedule.tick()

    assert bool(recorder.calls) is recorded


# ---------------------------------------------------------------------------
# A failure is logged, never silent, and never stops the loop
# ---------------------------------------------------------------------------


def actions(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.action, r.symbol) for r in caplog.records if hasattr(r, "action") and hasattr(r, "symbol")]


async def test_a_vendor_failure_is_logged_with_an_action_and_the_other_symbols_still_record(caplog):
    clock = Clock(at_et(MONDAY, 16, 15))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())
    recorder.fail.add("SPY")

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name):
        await schedule.tick()

    assert actions(caplog) == [("iv_recorder_snapshot_failed", "SPY"), ("iv_recorder_snapshot_recorded", "QQQ")]
    assert len(schedule.store.read_series("QQQ")) == 1 and schedule.store.read_series("SPY") == []


async def test_a_failed_symbol_is_not_asked_again_that_day_but_is_asked_the_next_trading_day():
    clock = Clock(at_et(MONDAY, 16, 15))
    schedule, recorder = schedule_for(("SPY",), clock, InMemoryIvSnapshotStore())
    recorder.fail.add("SPY")

    await schedule.tick()
    clock.now = at_et(MONDAY, 17, 0)
    await schedule.tick()
    assert [ticker for ticker, _ in recorder.calls] == ["SPY"], "no retry storm against a down vendor"

    recorder.fail.clear()
    clock.now = at_et(date(2024, 3, 12), 16, 15)
    await schedule.tick()

    assert len(recorder.calls) == 2 and recorder.calls[1][1] == clock.now
    assert len(schedule.store.read_series("SPY")) == 1


async def test_an_error_row_the_recorder_files_is_logged_and_counts_as_the_days_attempt(caplog):
    store = InMemoryIvSnapshotStore()
    clock = Clock(at_et(MONDAY, 16, 15))
    schedule, recorder = schedule_for(("SPY",), clock, store)
    recorder.file_error_row.add("SPY")

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name):
        await schedule.tick()
    assert actions(caplog) == [("iv_recorder_snapshot_error_row", "SPY")]
    (logged,) = [r for r in caplog.records if getattr(r, "action", "") == "iv_recorder_snapshot_error_row"]
    assert logged.error == "polygon_fetch_failed: 502" and logged.levelno == logging.WARNING

    clock.now = at_et(MONDAY, 17, 0)
    restarted, restarted_recorder = schedule_for(("SPY",), clock, store)
    await restarted.tick()
    assert restarted_recorder.calls == [], "the day's attempt was filed; the next trading day tries again"


async def test_the_loop_survives_a_tick_that_raises_and_keeps_looking(caplog):
    clock = Clock(at_et(MONDAY, 16, 15))
    store = InMemoryIvSnapshotStore()
    recorder = Recorder(store)
    sleeps: list[float] = []

    class StopLoop(Exception):
        pass

    reads = 0

    def flaky_clock() -> int:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("calendar unavailable")
        return clock.now

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    schedule = IvRecorderSchedule(symbols=("SPY",), store=store, record=recorder, clock=flaky_clock, sleep=sleep)

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name), pytest.raises(StopLoop):
        await schedule._run()

    assert sleeps == [TICK_SECONDS, TICK_SECONDS]
    assert [r.action for r in caplog.records if hasattr(r, "action")] == ["iv_recorder_tick_failed", "iv_recorder_snapshot_recorded"]
    assert len(store.read_series("SPY")) == 1


async def test_start_and_stop_own_one_task():
    schedule, _ = schedule_for(("SPY",), Clock(at_et(SATURDAY, 12)), InMemoryIvSnapshotStore())

    schedule.start()
    first = schedule._task
    schedule.start()
    assert schedule._task is first, "a second start does not begin a second loop"
    await asyncio.sleep(0)

    await schedule.stop()
    assert first is not None and first.done() and schedule._task is None


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_the_default_symbols_are_the_option_archives_underlyings():
    assert parse_symbols(type(settings).model_fields["IV_RECORDER_SYMBOLS"].default) == UNDERLYINGS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("SPY,QQQ,IWM", ("SPY", "QQQ", "IWM")), (" spy , QQQ,spy,", ("SPY", "QQQ")), ("", ()), (" , ", ())],
)
def test_symbols_are_split_upper_cased_and_deduplicated(raw, expected):
    assert parse_symbols(raw) == expected


def test_a_symbol_that_is_not_a_ticker_refuses_to_boot_and_names_the_setting():
    with pytest.raises(ValueError, match=r"IV_RECORDER_SYMBOLS names '9X'"):
        parse_symbols("SPY,9X")


def test_an_empty_symbols_setting_turns_the_schedule_off(monkeypatch):
    monkeypatch.setattr(settings, "IV_RECORDER_SYMBOLS", "")

    assert IvRecorderSchedule.from_settings() is None


def test_the_schedule_the_data_plane_runs_files_the_close_slot_into_the_process_store(monkeypatch):
    monkeypatch.setattr(settings, "IV_RECORDER_SYMBOLS", "spy, iwm")
    asked: list[dict] = []

    def fake_record(**kwargs):
        asked.append(kwargs)
        return row(kwargs["ticker"], to_ms_utc(kwargs["asof"]))

    monkeypatch.setattr(iv_recorder_schedule, "record_iv_snapshot", fake_record)
    schedule = IvRecorderSchedule.from_settings()
    assert schedule is not None and schedule.symbols == ("SPY", "IWM")

    asof = datetime.fromtimestamp(at_et(MONDAY, 16, 15) / 1000, tz=ZoneInfo("UTC"))
    schedule._record("SPY", asof)

    (call,) = asked
    assert call["ticker"] == "SPY" and call["slot"] == CLOSE_SLOT and call["asof"] == asof
    assert call["store"] is schedule.store
