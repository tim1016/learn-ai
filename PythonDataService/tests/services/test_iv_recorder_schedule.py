"""The IV30 recorder's clock (#3525): when it fires, that it fires once a day, that it does not catch up, and that a failure never stops it.

Every test drives the schedule with a fake clock and a fake recorder that writes into an in-memory store. No test asks a
vendor for anything, and none waits: the loop's sleep is injected.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.option_archive.planner import UNDERLYINGS
from app.services import iv_recorder_schedule
from app.services.iv_recorder import OPTIONS_SNAPSHOT_DELAY_MS, InMemoryIvSnapshotStore, RecordedIvSnapshot
from app.services.iv_recorder_schedule import (
    GRACE_MS,
    SLOT_BEFORE_CLOSE_MS,
    SNAPSHOT_SLOT,
    TICK_SECONDS,
    IvRecorderSchedule,
    slot_ms,
    snapshot_day_at,
    snapshot_due_ms,
)
from app.utils.symbols import parse_symbol_list
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


def row(ticker: str, ts: int, *, slot: str = SNAPSHOT_SLOT, error: str | None = None, asked: int | None = ...) -> RecordedIvSnapshot:
    """A row as the recorder files it: ``ts`` is the quote time, and the vendor was asked ``asked`` (by default one delay later)."""
    return RecordedIvSnapshot(
        ticker=ticker, snapshot_ts_ms=ts, asked_at_ms=ts + OPTIONS_SNAPSHOT_DELAY_MS if asked is ... else asked, slot=slot,
        spot=0.0 if error else 500.0, rate=0.05, dividend_yield=0.0,
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
        asked = to_ms_utc(asof)
        self.calls.append((ticker, asked))
        if ticker in self.fail:
            raise RuntimeError("vendor down")
        written = row(
            ticker, asked - OPTIONS_SNAPSHOT_DELAY_MS, asked=asked, error="polygon_fetch_failed: 502" if ticker in self.file_error_row else None
        )
        self.store.write(written)
        return written


async def no_sleep(_: float) -> None:
    """A sleep that waits for nothing but still lets the loop run other tasks."""
    await asyncio.sleep(0)


def schedule_for(symbols: tuple[str, ...], clock: Clock, store: InMemoryIvSnapshotStore) -> tuple[IvRecorderSchedule, Recorder]:
    recorder = Recorder(store)
    return IvRecorderSchedule(symbols=symbols, store=store, record=recorder, clock=clock, sleep=no_sleep), recorder


# ---------------------------------------------------------------------------
# When: five minutes before the calendar's close, asked a snapshot delay later
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "slot", "asked"),
    [
        pytest.param(MONDAY, (15, 55), (16, 10), id="ordinary session"),
        pytest.param(AFTER_FALL_BACK, (15, 55), (16, 10), id="standard time"),
        pytest.param(EARLY_CLOSE, (12, 55), (13, 10), id="early close"),
    ],
)
def test_the_slot_is_five_minutes_before_the_calendar_close_and_the_vendor_is_asked_a_snapshot_delay_later(day, slot, asked):
    assert slot_ms(day) == session_close_ms_utc(day) - SLOT_BEFORE_CLOSE_MS == at_et(day, *slot)
    assert snapshot_due_ms(day) == slot_ms(day) + OPTIONS_SNAPSHOT_DELAY_MS == at_et(day, *asked)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(at_et(MONDAY, 16, 9), None, id="a minute before the instant"),
        pytest.param(at_et(MONDAY, 16, 10), MONDAY, id="at the instant"),
        pytest.param(at_et(MONDAY, 16, 10) + GRACE_MS - 1, MONDAY, id="the last moment inside the grace"),
        pytest.param(at_et(MONDAY, 16, 10) + GRACE_MS, None, id="past the grace: the quotes are of a moment after the close"),
        pytest.param(at_et(EARLY_CLOSE, 13, 9), None, id="early close, before its instant"),
        pytest.param(at_et(EARLY_CLOSE, 13, 10), EARLY_CLOSE, id="early close, at its instant"),
        pytest.param(at_et(EARLY_CLOSE, 16, 10), None, id="early close: the ordinary instant is long past its grace"),
        pytest.param(at_et(SATURDAY, 16, 30), None, id="a weekend"),
        pytest.param(at_et(HOLIDAY, 16, 30), None, id="a holiday"),
    ],
)
def test_a_day_is_wanted_from_its_instant_until_the_grace_ends(now, expected):
    assert snapshot_day_at(now) == expected


def test_the_grace_ends_while_the_quotes_asked_for_are_still_from_before_the_close():
    """Asked at the grace's last moment, the delayed snapshot shows the market at the close less one millisecond: inside the 15:45 bar."""
    last_asked = snapshot_due_ms(MONDAY) + GRACE_MS - 1

    assert last_asked - OPTIONS_SNAPSHOT_DELAY_MS == session_close_ms_utc(MONDAY) - 1


# ---------------------------------------------------------------------------
# The clock fires on trading days, at the calendar's instant
# ---------------------------------------------------------------------------


async def test_the_schedule_records_each_symbol_when_the_instant_comes_and_not_before():
    clock = Clock(at_et(MONDAY, 16, 9))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())

    await schedule.tick()
    assert recorder.calls == []

    clock.now = at_et(MONDAY, 16, 10)
    await schedule.tick()

    assert recorder.calls == [("SPY", clock.now), ("QQQ", clock.now)], "the vendor is asked at the instant"
    for ticker in ("SPY", "QQQ"):
        (stored,) = schedule.store.read_series(ticker)
        assert stored.slot == SNAPSHOT_SLOT == "15:55"
        assert stored.snapshot_ts_ms == at_et(MONDAY, 15, 55), "and the row sits at the quotes' time, the slot"
        assert stored.asked_at_ms == clock.now


async def test_the_schedule_fires_nothing_on_a_weekend_or_a_holiday():
    schedule, recorder = schedule_for(("SPY",), Clock(0), InMemoryIvSnapshotStore())
    for day in (SATURDAY, HOLIDAY):
        for hour in range(0, 24):
            schedule.clock.now = at_et(day, hour, 30)
            await schedule.tick()

    assert recorder.calls == []


async def test_an_early_close_moves_the_instant_with_it_and_the_slot_name_stays():
    clock = Clock(at_et(EARLY_CLOSE, 13, 9))
    schedule, recorder = schedule_for(("SPY",), clock, InMemoryIvSnapshotStore())

    await schedule.tick()
    assert recorder.calls == []

    clock.now = at_et(EARLY_CLOSE, 13, 10)
    await schedule.tick()
    assert recorder.calls == [("SPY", clock.now)]
    (stored,) = schedule.store.read_series("SPY")
    assert stored.slot == "15:55" and stored.snapshot_ts_ms == at_et(EARLY_CLOSE, 12, 55)


# ---------------------------------------------------------------------------
# Late: no catch-up
# ---------------------------------------------------------------------------


async def test_a_process_that_starts_inside_the_grace_still_records_the_day():
    schedule, recorder = schedule_for(("SPY",), Clock(at_et(MONDAY, 16, 10) + GRACE_MS - 1), InMemoryIvSnapshotStore())

    await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["SPY"]


async def test_a_process_that_starts_after_the_grace_skips_the_day_and_says_so(caplog):
    """Later quotes would be filed under the 15:55 slot; the day is left empty and logged, not caught up."""
    schedule, recorder = schedule_for(("SPY", "QQQ"), Clock(at_et(MONDAY, 16, 10) + GRACE_MS), InMemoryIvSnapshotStore())

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name):
        await schedule.tick()
        schedule.clock.now += 60_000
        await schedule.tick()

    assert recorder.calls == []
    assert schedule.store.read_series("SPY") == [] and schedule.store.read_series("QQQ") == []
    assert actions(caplog) == [("iv_recorder_snapshot_skipped_late", "SPY"), ("iv_recorder_snapshot_skipped_late", "QQQ")], "once per symbol"
    (logged, _) = [r for r in caplog.records if getattr(r, "action", "") == "iv_recorder_snapshot_skipped_late"]
    assert logged.day == MONDAY.isoformat() and logged.late_by_ms == GRACE_MS and logged.levelno == logging.WARNING


async def test_a_restart_after_the_grace_logs_no_skip_for_a_day_the_store_already_holds():
    store = InMemoryIvSnapshotStore()
    first, _ = schedule_for(("SPY",), Clock(at_et(MONDAY, 16, 10)), store)
    await first.tick()

    restarted, restarted_recorder = schedule_for(("SPY",), Clock(at_et(MONDAY, 19, 0)), store)
    await restarted.tick()

    assert restarted_recorder.calls == [] and len(store.read_series("SPY")) == 1


async def test_a_day_skipped_late_does_not_stop_the_next_trading_day():
    clock = Clock(at_et(MONDAY, 17, 0))
    schedule, recorder = schedule_for(("SPY",), clock, InMemoryIvSnapshotStore())
    await schedule.tick()
    assert recorder.calls == []

    clock.now = at_et(date(2024, 3, 12), 16, 10)
    await schedule.tick()

    assert recorder.calls == [("SPY", clock.now)]


# ---------------------------------------------------------------------------
# Once a day, across a restart
# ---------------------------------------------------------------------------


async def test_repeated_ticks_in_the_grace_record_each_symbol_once():
    clock = Clock(at_et(MONDAY, 16, 10))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())

    for seconds in (0, 30, 60, 120, 240):
        clock.now = at_et(MONDAY, 16, 10) + seconds * 1000
        await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["SPY", "QQQ"]


async def test_a_restart_in_the_grace_does_not_record_a_symbol_the_store_already_holds_for_the_day():
    store = InMemoryIvSnapshotStore()
    clock = Clock(at_et(MONDAY, 16, 10))
    first, first_recorder = schedule_for(("SPY", "QQQ"), clock, store)
    await first.tick()
    assert len(first_recorder.calls) == 2

    clock.now = at_et(MONDAY, 16, 12)
    restarted, restarted_recorder = schedule_for(("SPY", "QQQ"), clock, store)
    await restarted.tick()

    assert restarted_recorder.calls == []
    assert len(store.read_series("SPY")) == 1 and len(store.read_series("QQQ")) == 1


async def test_a_symbol_the_store_lacks_is_recorded_when_its_neighbour_is_already_held():
    store = InMemoryIvSnapshotStore()
    store.write(row("SPY", at_et(MONDAY, 15, 55)))
    schedule, recorder = schedule_for(("SPY", "QQQ"), Clock(at_et(MONDAY, 16, 12)), store)

    await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["QQQ"]


@pytest.mark.parametrize(
    ("existing", "recorded"),
    [
        pytest.param(row("SPY", at_et(MONDAY, 12, 30), slot="12:30"), True, id="another slot is not this one"),
        pytest.param(row("SPY", at_et(date(2024, 3, 8), 15, 55)), True, id="yesterday's snapshot"),
        pytest.param(row("SPY", at_et(MONDAY, 15, 55), error="replication_failed: x"), False, id="an error row is the day's attempt"),
        pytest.param(row("SPY", at_et(MONDAY, 15, 55)), False, id="today's snapshot"),
        pytest.param(row("SPY", at_et(date(2024, 3, 8), 15, 30), asked=at_et(MONDAY, 16, 10)), False,
                     id="asked today whatever the quote time: a stale chain does not send the schedule back to the vendor"),
        pytest.param(row("SPY", at_et(MONDAY, 15, 55), asked=None), False, id="a row with no asking time falls back to its quote time"),
        pytest.param(row("SPY", at_et(date(2024, 3, 8), 15, 55), asked=None), True, id="and yesterday's, without an asking time"),
    ],
)
async def test_only_this_slots_row_asked_for_on_the_day_counts_as_recorded(existing, recorded):
    store = InMemoryIvSnapshotStore()
    store.write(existing)
    schedule, recorder = schedule_for(("SPY",), Clock(at_et(MONDAY, 16, 12)), store)

    await schedule.tick()

    assert bool(recorder.calls) is recorded


async def test_the_days_existence_check_reads_the_series_and_never_a_raw_chain(tmp_path):
    from app.services.iv_recorder import JsonlIvSnapshotStore

    store = JsonlIvSnapshotStore(tmp_path)
    for day in (date(2024, 3, 7), date(2024, 3, 8)):
        written = row("SPY", at_et(day, 15, 55))
        store.write(RecordedIvSnapshot(**{**{f: getattr(written, f) for f in written.__dataclass_fields__}, "raw_chain": [{"bid": 1.0, "ask": 1.1}] * 50}))
    shutil.rmtree(tmp_path / "raw")
    clock = Clock(at_et(MONDAY, 16, 12))
    schedule, recorder = schedule_for(("SPY",), clock, store)

    await schedule.tick()

    assert [ticker for ticker, _ in recorder.calls] == ["SPY"], "two earlier days held, today's not, and no chain was needed to see it"
    assert len(store.read_series("SPY")) == 3


# ---------------------------------------------------------------------------
# A failure is logged, never silent, and never stops the loop
# ---------------------------------------------------------------------------


def actions(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.action, r.symbol) for r in caplog.records if hasattr(r, "action") and hasattr(r, "symbol")]


async def test_a_vendor_failure_is_logged_with_an_action_and_the_other_symbols_still_record(caplog):
    clock = Clock(at_et(MONDAY, 16, 10))
    schedule, recorder = schedule_for(("SPY", "QQQ"), clock, InMemoryIvSnapshotStore())
    recorder.fail.add("SPY")

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name):
        await schedule.tick()

    assert actions(caplog) == [("iv_recorder_snapshot_failed", "SPY"), ("iv_recorder_snapshot_recorded", "QQQ")]
    assert len(schedule.store.read_series("QQQ")) == 1 and schedule.store.read_series("SPY") == []


async def test_a_failed_symbol_is_not_asked_again_that_day_but_is_asked_the_next_trading_day():
    clock = Clock(at_et(MONDAY, 16, 10))
    schedule, recorder = schedule_for(("SPY",), clock, InMemoryIvSnapshotStore())
    recorder.fail.add("SPY")

    await schedule.tick()
    clock.now = at_et(MONDAY, 16, 12)
    await schedule.tick()
    assert [ticker for ticker, _ in recorder.calls] == ["SPY"], "no retry storm against a down vendor"

    recorder.fail.clear()
    clock.now = at_et(date(2024, 3, 12), 16, 10)
    await schedule.tick()

    assert len(recorder.calls) == 2 and recorder.calls[1][1] == clock.now
    assert len(schedule.store.read_series("SPY")) == 1


async def test_an_error_row_the_recorder_files_is_logged_and_counts_as_the_days_attempt(caplog):
    store = InMemoryIvSnapshotStore()
    clock = Clock(at_et(MONDAY, 16, 10))
    schedule, recorder = schedule_for(("SPY",), clock, store)
    recorder.file_error_row.add("SPY")

    with caplog.at_level(logging.INFO, logger=iv_recorder_schedule.logger.name):
        await schedule.tick()
    assert actions(caplog) == [("iv_recorder_snapshot_error_row", "SPY")]
    (logged,) = [r for r in caplog.records if getattr(r, "action", "") == "iv_recorder_snapshot_error_row"]
    assert logged.error == "polygon_fetch_failed: 502" and logged.levelno == logging.WARNING

    clock.now = at_et(MONDAY, 16, 12)
    restarted, restarted_recorder = schedule_for(("SPY",), clock, store)
    await restarted.tick()
    assert restarted_recorder.calls == [], "the day's attempt was filed; the next trading day tries again"


async def test_the_loop_survives_a_tick_that_raises_and_keeps_looking(caplog):
    clock = Clock(at_et(MONDAY, 16, 10))
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
    assert parse_symbol_list(type(settings).model_fields["IV_RECORDER_SYMBOLS"].default, setting="IV_RECORDER_SYMBOLS") == UNDERLYINGS


def test_an_empty_symbols_setting_turns_the_schedule_off(monkeypatch):
    monkeypatch.setattr(settings, "IV_RECORDER_SYMBOLS", "")

    assert IvRecorderSchedule.from_settings() is None


def test_the_schedule_the_data_plane_runs_files_the_snapshot_slot_into_the_process_store(monkeypatch):
    monkeypatch.setattr(settings, "IV_RECORDER_SYMBOLS", "spy, iwm")
    asked: list[dict] = []

    def fake_record(**kwargs):
        asked.append(kwargs)
        return row(kwargs["ticker"], to_ms_utc(kwargs["asof"]) - OPTIONS_SNAPSHOT_DELAY_MS)

    monkeypatch.setattr(iv_recorder_schedule, "record_iv_snapshot", fake_record)
    schedule = IvRecorderSchedule.from_settings()
    assert schedule is not None and schedule.symbols == ("SPY", "IWM")

    asof = datetime.fromtimestamp(at_et(MONDAY, 16, 10) / 1000, tz=ZoneInfo("UTC"))
    schedule._record("SPY", asof)

    (call,) = asked
    assert call["ticker"] == "SPY" and call["slot"] == SNAPSHOT_SLOT and call["asof"] == asof
    assert call["store"] is schedule.store
