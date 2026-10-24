"""The reliability study's lake inputs: warmup, session, identity and refusals (#3523; PRD #3490 R12-R15, T05-T09).

Every test crosses the real seams as ``test_study_inputs.py`` does: pinned vendor bars are served at the vendor's HTTP
boundary, the real ``ensure_data`` fetches and validates them, the real lake writer encodes them, the in-memory catalog
stand-in arbitrates, and admission reads them back through committed-byte admission. The study's own code sits on top:
its warmup sizing (``resolve_indicator_window``, unchanged), its fold, its identity, its refusals.

Scope: what the study adds to admission. The admission cases themselves (cold, held, one gap, floor, corrupt adjustment,
lease contention) are in ``test_study_inputs.py``; here they appear once each, through the study, so the counts the data
source line shows are the counts admission reached.

The reliability study keeps no result cache (it never had one, and its random baseline is an unseeded Monte Carlo
draw), so T07 and T09 reduce to the identity a stored or cached result would be keyed on: the tests below pin when it
changes and when it does not.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from app.data_lake import run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.data_lake.polygon_fetcher import polygon_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.indicator_reliability import compute_forward_return
from app.research.study_inputs import lead_in_sessions
from app.services.dataset_service import calculate_dynamic_indicators, resolve_indicator_window
from app.services.indicator_reliability_service import (
    SUPPORTED_MINUTE_MULTIPLIERS,
    AdmittedReliabilityInputs,
    ReliabilityParams,
    ReliabilityRefusal,
    admit_reliability_inputs,
    study_bar_minutes,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day, lake_file, minute_bar
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history

ET = ZoneInfo("America/New_York")

#: 2025-01-01 00:00 UTC: after every window used here, inside the provider's five-year history.
NOW_MS = 1_735_689_600_000

MARCH = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))  # ten sessions; Mar 10 is the spring-forward Sunday
JULY = expected_sessions(date(2024, 7, 1), date(2024, 7, 5))  # Jul 3 is an early close; Jul 4 a holiday

#: A lookback of 200 bars is 1,000 warmup bars: three fully traded one-minute sessions.
EMA_200 = {"indicator_name": "ema", "indicator_params": {"length": 200}}
#: The default test indicator, RSI(14), warms up on 140 bars: one session.
RSI_LEAD_SESSIONS = 1


class World:
    def __init__(self, catalog: FakeCatalog, lake: Path) -> None:
        self.catalog, self.lake = catalog, lake


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    with respx.mock:
        mock_launcher()
        yield World(catalog, lake)


def params(window: list[date], **overrides) -> ReliabilityParams:
    base = {
        "symbol": "SPY",
        "indicator_name": "rsi",
        "indicator_params": {"length": 14},
        "from_date": window[0],
        "to_date": window[-1],
        "timespan": "minute",
        "multiplier": 1,
        "horizons": (1, 5),
    }
    return ReliabilityParams(**{**base, **overrides})


def admit(p: ReliabilityParams, *, now_ms: int = NOW_MS) -> AdmittedReliabilityInputs:
    return admit_reliability_inputs(p, now_ms=now_ms)


def refusal_of(p: ReliabilityParams, *, now_ms: int = NOW_MS) -> ReliabilityRefusal:
    with pytest.raises(ReliabilityRefusal) as raised:
        admit(p, now_ms=now_ms)
    return raised.value


def tape_for(days: list[date], **options) -> dict[date, list[VendorBar]]:
    return vendor_history(days, seed=11, start_price=150.0, **options)


def full_tape(window: list[date], lead: int = RSI_LEAD_SESSIONS, **options) -> dict[date, list[VendorBar]]:
    """The vendor's bars for the window and the ``lead`` sessions the default indicator's warmup reads before it."""
    return tape_for([*lead_in_sessions(window[0], lead), *window], **options)


def counts(group: dict) -> tuple[int, int, int, int]:
    """(reused, added, missing, failed)."""
    return group["reused"], group["added"], group["missing"], group["failed"]


def digests(study: AdmittedReliabilityInputs) -> tuple[str, str, str]:
    source = study.data_source()
    return source["identity_digest"], source["snapshot_digest"], source["result_identity_digest"]


def clock_for(today: date) -> int:
    """'Now' as ``today``'s ET midnight: the one instant the warmup sizing and admission are both handed."""
    return et_midnight_ms(today)


# ---------------------------------------------------------------------------
# The bar size: what the lake can form exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minutes", SUPPORTED_MINUTE_MULTIPLIERS)
def test_every_minute_length_that_divides_the_hour_is_supported(minutes):
    assert study_bar_minutes("minute", minutes) == minutes
    assert 60 % minutes == 0


def test_one_hour_is_sixty_minutes():
    assert study_bar_minutes("hour", 1) == 60


@pytest.mark.parametrize(
    ("timespan", "multiplier", "phrase"),
    [
        pytest.param("day", 1, "daily rollup folds extended-hours trades", id="daily"),
        pytest.param("day", 5, "daily rollup folds extended-hours trades", id="five_days"),
        pytest.param("minute", 7, "wall-clock boundaries", id="a_length_that_starts_bars_off_the_hour"),
        pytest.param("minute", 90, "wall-clock boundaries", id="a_length_longer_than_an_hour"),
        pytest.param("hour", 2, "wall-clock boundaries", id="two_hours"),
    ],
)
def test_a_bar_size_the_lake_cannot_form_exactly_is_refused_before_the_lake_is_touched(world, timespan, multiplier, phrase):
    refusal = refusal_of(params(MARCH, timespan=timespan, multiplier=multiplier))

    assert refusal.code == "resolution_unsupported"
    assert phrase in refusal.reason
    assert refusal.coverage is None and refusal.data_source is None
    assert world.catalog.rows == {}, "a request that can never run costs the lake nothing"


# ---------------------------------------------------------------------------
# Warmup: the unchanged sizing, admitted with the window and reported
# ---------------------------------------------------------------------------


@respx.mock
def test_the_lead_in_is_the_sessions_the_sizing_names_admitted_beside_the_window_and_counted_apart(world):
    window = MARCH[4:]
    sizing = resolve_indicator_window(window[0].isoformat(), max_lookback=200, bar_minutes=1, now_ms=NOW_MS)
    lead = expected_sessions(date.fromisoformat(sizing.fetch_from), window[0] - timedelta(days=1))
    assert (sizing.warmup_bars, len(lead)) == (1000, 3), "three fully traded one-minute sessions cover 1,000 bars"
    mock_vendor("SPY", tape_for([*lead, *window], regular_keep=1.0))

    study = admit(params(window, **EMA_200))

    source = study.data_source()
    assert counts(source["coverage"]["warmup"]) == (0, 3, 0, 0)
    assert counts(source["coverage"]["window"]) == (0, len(window), 0, 0)
    assert source["warmup"] == {
        "policy": "allow_shortened", "requested_bars": 1000, "requested_sessions": 3, "admitted_sessions": 3,
        "admitted_bars": 3 * 390, "shortened": False, "shortfall_bars": 0, "cause": "none", "notice": None,
    }
    lead_rows = study.frame[study.frame["timestamp"] < study.window_start_ms]
    assert len(lead_rows) == 3 * 390
    assert study.window_start_ms == et_midnight_ms(window[0])
    assert lead_rows["timestamp"].min() >= session_open_ms_utc(lead[0]), "the lead-in starts at the sizing's first session"
    assert study.max_lookback == 200


@respx.mock
def test_a_lead_in_cut_short_by_the_providers_history_floor_is_reported_and_the_run_goes_ahead(world):
    today = date(2029, 3, 6)
    assert polygon_history_floor(today) == date(2024, 3, 8), "the provider's history begins on Friday Mar 8"
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 15))
    now = clock_for(today)
    mock_vendor("SPY", tape_for(expected_sessions(date(2024, 3, 8), window[-1]), regular_keep=1.0))

    study = admit(params(window, **EMA_200), now_ms=now)

    warmup = study.warmup
    assert (warmup.requested_bars, warmup.requested_sessions, warmup.admitted_sessions) == (1000, 1, 1)
    assert warmup.admitted_bars == 390 and warmup.shortfall_bars == 610
    assert warmup.shortened and warmup.cause == "provider_history_floor"
    assert counts(study.data_source()["coverage"]["warmup"]) == (0, 1, 0, 0)


@respx.mock
def test_require_full_refuses_a_lead_in_the_floor_cuts_short_and_says_how_short(world):
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 15))
    now = clock_for(date(2029, 3, 6))
    mock_vendor("SPY", tape_for(expected_sessions(date(2024, 3, 8), window[-1]), regular_keep=1.0))

    refusal = refusal_of(params(window, warmup_policy="require_full", **EMA_200), now_ms=now)

    assert refusal.code == "warmup_shortened"
    assert "1000 bars" in refusal.reason and "390" in refusal.reason
    assert "provider's history does not reach back further" in refusal.reason
    warmup = refusal.data_source["warmup"]
    assert (warmup["shortened"], warmup["cause"], warmup["policy"]) == (True, "provider_history_floor", "require_full")
    assert refusal.data_source["snapshot_digest"] is not None, "the bytes were admitted; the policy refused the result"
    assert counts(refusal.data_source["coverage"]["window"]) == (0, len(window), 0, 0)


@respx.mock
def test_a_retained_window_older_than_the_floor_stays_readable_with_no_lead_in_and_says_so(world):
    """The sizing never reaches before the floor, so a window the floor already passed gets no lead-in at all."""
    window = expected_sessions(date(2024, 3, 4), date(2024, 3, 7))
    mock_vendor("SPY", full_tape(window))
    admit(params(window))  # the lake holds the window (and its one lead-in session) while it is in the provider's history
    now = clock_for(date(2029, 3, 6))  # the Mar 8 floor has now passed the window

    study = admit(params(window, **EMA_200), now_ms=now)

    assert (study.warmup.requested_sessions, study.warmup.admitted_sessions, study.warmup.admitted_bars) == (0, 0, 0)
    assert study.warmup.shortened and study.warmup.cause == "provider_history_floor"
    assert counts(study.data_source()["coverage"]["window"]) == (len(window), 0, 0, 0), "retained days stay readable"


@respx.mock
def test_a_window_day_older_than_the_floor_that_the_lake_never_held_is_refused_and_never_asked_of_the_vendor(world):
    window = expected_sessions(date(2024, 3, 4), date(2024, 3, 7))
    calls = VendorCalls()
    mock_vendor("SPY", full_tape(window), calls=calls)
    now = clock_for(date(2029, 3, 6))  # the Mar 8 floor has passed the whole window

    refusal = refusal_of(params(window), now_ms=now)

    assert refusal.code == "data_unavailable_from_provider"
    assert counts(refusal.data_source["coverage"]["window"]) == (0, 0, 0, len(window))
    assert "provider's history, which starts 2024-03-08" in refusal.reason
    assert calls.count == 0, "a day the provider no longer serves is never asked of it"


@respx.mock
def test_a_symbol_whose_history_starts_inside_the_lead_in_runs_on_the_sessions_it_has_and_counts_the_rest(world):
    window = MARCH[4:]
    lead = lead_in_sessions(window[0], 3)
    tape = tape_for([*lead, *window], regular_keep=1.0)
    del tape[lead[0]]  # the provider has no bars for the oldest lead-in session
    mock_vendor("SPY", tape)

    study = admit(params(window, **EMA_200))

    warmup = study.warmup
    assert (warmup.requested_sessions, warmup.admitted_sessions) == (3, 2)
    assert warmup.shortened and warmup.cause == "symbol_history_starts" and warmup.admitted_bars == 2 * 390
    assert counts(study.data_source()["coverage"]["warmup"]) == (0, 2, 0, 1), "two admitted, and the one the vendor had no bars for"
    assert study.admitted.coverage.dropped == (lead[0],)
    lead_rows = study.frame[study.frame["timestamp"] < study.window_start_ms]
    assert lead_rows["timestamp"].min() >= session_open_ms_utc(lead[1])
    assert study.identity.inputs.warmup_sessions == 2, "the identity is the lead-in the numbers came from"


@respx.mock
def test_a_window_that_opens_on_the_symbols_first_session_runs_with_no_lead_in_and_says_so(world):
    window = MARCH[4:]
    mock_vendor("SPY", tape_for(window, regular_keep=1.0))  # the provider has nothing before the window

    study = admit(params(window, **EMA_200))

    warmup = study.warmup
    assert (warmup.requested_sessions, warmup.admitted_sessions, warmup.admitted_bars) == (3, 0, 0)
    assert warmup.shortened and warmup.cause == "symbol_history_starts" and warmup.shortfall_bars == 1000
    assert counts(study.data_source()["coverage"]["warmup"]) == (0, 0, 0, 3), "all three were asked for and none had bars"
    assert len(study.admitted.coverage.dropped) == 3
    assert study.frame["timestamp"].min() >= study.window_start_ms


@respx.mock
def test_require_full_refuses_a_symbol_listed_inside_the_lead_in_with_warmup_shortened(world):
    """The request field's own description promises this code (#3523); a late listing used to refuse as a data outage."""
    window = MARCH[4:]
    lead = lead_in_sessions(window[0], 3)
    tape = tape_for([*lead, *window], regular_keep=1.0)
    del tape[lead[0]]
    mock_vendor("SPY", tape)

    refusal = refusal_of(params(window, warmup_policy="require_full", **EMA_200))
    shorter = admit(params(window, **EMA_200))

    assert refusal.code == "warmup_shortened"
    assert "1000 bars" in refusal.reason and "780" in refusal.reason
    assert "symbol has no bars before its first listed session" in refusal.reason and "Start the window later" in refusal.reason
    warmup = refusal.data_source["warmup"]
    assert (warmup["shortened"], warmup["cause"], warmup["policy"], warmup["admitted_sessions"]) == (True, "symbol_history_starts", "require_full", 2)
    assert refusal.coverage.dropped == (lead[0],) and counts(refusal.data_source["coverage"]["warmup"]) == (0, 2, 0, 1)
    assert refusal.data_source["snapshot_digest"] is not None, "the bytes were admitted; the policy refused the result"
    assert refusal.data_source["identity_digest"] == digests(shorter)[0], "the refusal names the lead-in that was read"


@respx.mock
def test_require_full_over_sparse_sessions_does_not_tell_the_caller_to_start_later(world):
    window = MARCH[4:]
    mock_vendor("SPY", tape_for([*lead_in_sessions(window[0], 3), *window], regular_keep=0.4))

    refusal = refusal_of(params(window, warmup_policy="require_full", **EMA_200))

    assert refusal.code == "warmup_shortened" and refusal.data_source["warmup"]["cause"] == "sparse_session_bars"
    assert "Allow a shortened warmup" in refusal.reason and "Start the window later" not in refusal.reason


@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
@pytest.mark.parametrize("policy", ["allow_shortened", "require_full"])
@respx.mock
def test_a_tape_the_lake_rejects_on_the_oldest_lead_in_session_refuses_and_is_not_a_symbol_that_lists_late(world, policy, fault):
    """A vendor failure is not 'the symbol's history starts here' (#3523), and it stays in the counts.

    The oldest lead-in session's tape has a duplicate (or out-of-order) timestamp, so the lake's publication gate
    refuses it. Only a session the vendor answered with no bars may be dropped from the lead-in.
    """
    window = MARCH[4:]
    lead = lead_in_sessions(window[0], 3)
    tape = tape_for([*lead, *window], regular_keep=1.0)
    bars = tape[lead[0]]
    tape[lead[0]] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", tape)

    refusal = refusal_of(params(window, warmup_policy=policy, **EMA_200))

    assert refusal.code == "data_unavailable_from_provider"
    assert [(i.group, i.trading_date, i.outcome, i.failure_reason) for i in refusal.coverage.issues] == [
        ("warmup", lead[0], "unavailable_from_provider", "validation_failed")
    ]
    assert "validation_failed" in refusal.reason
    assert counts(refusal.data_source["coverage"]["warmup"]) == (0, 2, 0, 1), "the failed session is counted, not zeroed"
    assert refusal.data_source["warmup"] is None and refusal.data_source["snapshot_digest"] is None


@pytest.mark.parametrize("policy", ["allow_shortened", "require_full"])
@respx.mock
def test_a_hole_after_an_admitted_lead_in_session_is_a_gap_and_refuses_under_either_policy(world, policy):
    window = MARCH[4:]
    lead = lead_in_sessions(window[0], 3)
    tape = tape_for([*lead, *window], regular_keep=1.0)
    del tape[lead[1]]  # an outage day between two sessions with bars: the indicator would run across it
    mock_vendor("SPY", tape)

    refusal = refusal_of(params(window, warmup_policy=policy, **EMA_200))

    assert refusal.code == "data_unavailable_from_provider"
    assert [(i.group, i.trading_date) for i in refusal.coverage.issues] == [("warmup", lead[1])]
    assert refusal.data_source["snapshot_digest"] is None, "nothing was admitted, so nothing is fingerprinted"


@respx.mock
def test_sessions_that_all_exist_but_hold_few_bars_are_reported_as_a_sparse_lead_in(world):
    window = MARCH[4:]
    mock_vendor("SPY", tape_for([*lead_in_sessions(window[0], 3), *window], regular_keep=0.4))

    study = admit(params(window, **EMA_200))

    warmup = study.warmup
    assert (warmup.requested_sessions, warmup.admitted_sessions) == (3, 3)
    assert warmup.shortened and warmup.cause == "sparse_session_bars"
    assert 0 < warmup.admitted_bars < 1000 and warmup.shortfall_bars == 1000 - warmup.admitted_bars


@respx.mock
def test_an_unreadable_lead_in_session_refuses_as_corrupt_and_is_filed_under_the_warmup(world):
    window = MARCH[4:]
    lead = lead_in_sessions(window[0], 3)
    mock_vendor("SPY", tape_for([*lead, *window], regular_keep=1.0))
    admit(params(window, **EMA_200))
    rows = [minute_bar(lead[1], 10, 0), minute_bar(lead[1], 10, 1), minute_bar(lead[1], 10, 1)]
    commit_bars(world.catalog, world.lake, lead[1], rows, mode="polygon_split_adjusted")

    refusal = refusal_of(params(window, **EMA_200))

    assert refusal.code == "data_corrupt"
    assert [(i.group, i.trading_date, i.outcome) for i in refusal.coverage.issues] == [("warmup", lead[1], "corrupt")]
    assert "strictly increasing" in refusal.coverage.issues[0].reason


# ---------------------------------------------------------------------------
# The window: held, one gap, fully cold, corrupt adjustment, daily rollup (T05)
# ---------------------------------------------------------------------------


@respx.mock
def test_a_cold_window_is_fetched_then_a_repeat_costs_the_vendor_nothing(world):
    calls = VendorCalls()
    mock_vendor("SPY", full_tape(MARCH), calls=calls)

    first = admit(params(MARCH))
    asked = calls.count
    second = admit(params(MARCH))

    assert counts(first.data_source()["coverage"]["window"]) == (0, len(MARCH), 0, 0)
    assert counts(first.data_source()["coverage"]["warmup"]) == (0, RSI_LEAD_SESSIONS, 0, 0)
    assert counts(second.data_source()["coverage"]["window"]) == (len(MARCH), 0, 0, 0)
    assert counts(second.data_source()["coverage"]["warmup"]) == (RSI_LEAD_SESSIONS, 0, 0, 0)
    assert calls.count == asked, "a held window costs the vendor nothing"
    assert digests(second) == digests(first)


@respx.mock
def test_one_gap_in_the_window_is_filled_and_only_the_gap_is_fetched(world):
    calls = VendorCalls()
    mock_vendor("SPY", full_tape(MARCH), calls=calls)
    admit(params(MARCH[:2]))
    admit(params(MARCH[4:]))  # its one lead-in session, MARCH[3], is held too: only MARCH[2] is missing
    calls.requests.clear()

    study = admit(params(MARCH))

    assert counts(study.data_source()["coverage"]["window"]) == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]]


@respx.mock
def test_a_window_session_the_vendor_cannot_supply_refuses_and_names_it(world):
    tape = full_tape(MARCH)
    hole = MARCH[6]
    del tape[hole]
    mock_vendor("SPY", tape)

    refusal = refusal_of(params(MARCH))

    assert refusal.code == "data_unavailable_from_provider"
    assert [(i.group, i.trading_date, i.outcome) for i in refusal.coverage.issues] == [("window", hole, "unavailable_from_provider")]
    assert counts(refusal.data_source["coverage"]["window"]) == (0, len(MARCH) - 1, 0, 1)
    assert hole.isoformat() in refusal.reason


@respx.mock
def test_a_bad_corporate_action_record_is_rebuilt_and_counted_as_added(world):
    calls = VendorCalls()
    tape = full_tape(MARCH)
    mock_vendor("SPY", tape, adjusted=tape, calls=calls)
    admit(params(MARCH))
    record = companion_path(lake_file(world.lake, "polygon_split_adjusted", MARCH[2]))
    record.write_text(record.read_text().replace('"corporate_action_version": "', '"corporate_action_version": "0'))
    calls.requests.clear()

    study = admit(params(MARCH))

    assert counts(study.data_source()["coverage"]["window"]) == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]]


@respx.mock
def test_a_stale_daily_rollup_neither_blocks_nor_is_rebuilt_by_a_study_that_reads_minutes_only(world):
    """R13: a minute consumer must not be blocked solely by a daily artifact it never reads."""
    mock_vendor("SPY", full_tape(MARCH))
    narrow = run_materialization._build_engine_run_spec(
        symbol="SPY", start=MARCH[0], end=MARCH[1], price_adjustment_mode="polygon_split_adjusted", requester="test"
    )
    asyncio.run(run_materialization._materialize_run_data(narrow, resolution="minute"))
    rollups = [row for row in world.catalog.rows.values() if row["resolution"] == "daily"]
    assert len(rollups) == 1
    rollups[0]["file_sha256"] = "0" * 64  # the rollup no longer matches its receipt
    before = dict(rollups[0])

    study = admit(params(MARCH))

    assert counts(study.data_source()["coverage"]["window"])[:2] == (2, len(MARCH) - 2)
    assert [row for row in world.catalog.rows.values() if row["resolution"] == "daily"] == [before]


# ---------------------------------------------------------------------------
# Session, adjustment, precision (R14)
# ---------------------------------------------------------------------------


def _regular_starts(tape: dict[date, list[VendorBar]], day: date) -> list[int]:
    open_ms, close_ms = session_open_ms_utc(day), session_close_ms_utc(day)
    return [b["t"] for b in tape[day] if open_ms <= b["t"] < close_ms]


@respx.mock
@pytest.mark.parametrize("window", [JULY, MARCH], ids=["early_close", "spring_forward"])
def test_each_session_contributes_exactly_the_calendars_regular_hours(world, window):
    tape = full_tape(window)
    mock_vendor("SPY", tape)

    study = admit(params(window))

    window_rows = study.frame[study.frame["timestamp"] >= study.window_start_ms]
    assert window_rows["timestamp"].tolist() == [t for day in window for t in _regular_starts(tape, day)]
    if window is JULY:
        early = date(2024, 7, 3)
        assert datetime.fromtimestamp(session_close_ms_utc(early) / 1000, ET).hour == 13, "the calendar says 13:00"
        early_starts = [t for t in window_rows["timestamp"] if datetime.fromtimestamp(t / 1000, ET).date() == early]
        assert early_starts and max(early_starts) < session_close_ms_utc(early), "the half day ends at its own close"
        assert date(2024, 7, 4) not in {datetime.fromtimestamp(t / 1000, ET).date() for t in window_rows["timestamp"]}


@respx.mock
def test_hourly_bars_keep_the_bar_that_holds_the_open_and_a_sixty_minute_request_is_the_same_bars(world):
    """#3527: the session filter was blind to the bar length. Seven bars a regular day, four on a half day."""
    tape = full_tape(JULY, regular_keep=1.0)
    mock_vendor("SPY", tape)

    hourly = admit(params(JULY, timespan="hour", multiplier=1)).frame
    sixty = admit(params(JULY, timespan="minute", multiplier=60)).frame

    def starts(day: date) -> list[str]:
        local = [datetime.fromtimestamp(t / 1000, ET) for t in hourly["timestamp"]]
        return [t.strftime("%H:%M") for t in local if t.date() == day]

    assert starts(date(2024, 7, 1)) == ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
    assert starts(date(2024, 7, 3)) == ["09:00", "10:00", "11:00", "12:00"], "a 13:00 close leaves four hourly bars"
    assert hourly.equals(sixty)
    open_bar_start = session_open_ms_utc(date(2024, 7, 1)) - 30 * 60_000
    held = sum(b["v"] for b in tape[date(2024, 7, 1)] if session_open_ms_utc(date(2024, 7, 1)) <= b["t"] < open_bar_start + 3_600_000)
    assert hourly.loc[hourly["timestamp"] == open_bar_start, "volume"].iloc[0] == pytest.approx(held, abs=1e-9, rel=0)


@respx.mock
def test_extended_hours_bars_hold_every_minute_in_them_where_regular_hours_bars_hold_the_minutes_from_the_open(world):
    """Under ``rth`` the 09:00 hourly bar holds 09:30 to 09:59 only; under ``extended`` it holds 09:00 to 09:59."""
    day = JULY[0]
    tape = full_tape(JULY, regular_keep=1.0, extended_keep=1.0)
    mock_vendor("SPY", tape)
    open_ms = session_open_ms_utc(day)
    bucket = open_ms - 30 * 60_000

    regular = admit(params(JULY, timespan="minute", multiplier=60)).frame
    extended = admit(params(JULY, timespan="minute", multiplier=60, session_policy="extended")).frame

    def volume(frame, start: int) -> float:
        return float(frame.loc[frame["timestamp"] == start, "volume"].iloc[0])

    from_the_open = sum(b["v"] for b in tape[day] if open_ms <= b["t"] < bucket + 3_600_000)
    whole_bucket = sum(b["v"] for b in tape[day] if bucket <= b["t"] < bucket + 3_600_000)
    assert whole_bucket > from_the_open > 0
    assert volume(regular, bucket) == pytest.approx(from_the_open, abs=1e-9, rel=0)
    assert volume(extended, bucket) == pytest.approx(whole_bucket, abs=1e-9, rel=0)
    assert extended["timestamp"].is_monotonic_increasing and extended["timestamp"].is_unique

    def hours_on_day(frame) -> list[str]:
        local = [datetime.fromtimestamp(t / 1000, ET) for t in frame["timestamp"]]
        return [t.strftime("%H") for t in local if t.date() == day]

    assert hours_on_day(regular) == ["09", "10", "11", "12", "13", "14", "15"]
    assert hours_on_day(extended) == [f"{hour:02d}" for hour in range(4, 20)], "a bar at every hour from 04:00 to 19:00 ET"


@respx.mock
def test_no_bar_is_read_past_the_requested_end_so_the_last_horizon_bars_have_no_forward_return(world):
    window = MARCH[:5]
    calls = VendorCalls()
    mock_vendor("SPY", full_tape(MARCH, regular_keep=1.0), calls=calls)

    study = admit(params(window))

    last_close = session_close_ms_utc(window[-1])
    assert study.frame["timestamp"].max() == last_close - 60_000, "the window's last bar is its last regular minute"
    assert set(calls.days()) <= {*lead_in_sessions(window[0], RSI_LEAD_SESSIONS), *window}, "nothing after to_date was asked"
    forward = compute_forward_return(study.frame, 5, mask_overnight=True)
    assert forward.iloc[-5:].isna().all(), "the window's final five bars look past the end and are left unlabelled, not filled"
    assert forward.iloc[:-5].notna().sum() > 0


@respx.mock
def test_the_indicators_first_valid_value_sits_in_the_lead_in_when_there_is_one_and_in_the_window_when_there_is_not(world):
    window = expected_sessions(date(2024, 3, 4), date(2024, 3, 7))
    mock_vendor("SPY", full_tape(window, regular_keep=1.0))
    warm = admit(params(window))
    now = clock_for(date(2029, 3, 6))  # the floor has passed the window: the lake still holds it, with no lead-in
    cold = admit(params(window), now_ms=now)
    entry = [{"name": "rsi", "params": {"length": 14}}]

    warm_df, warm_meta = calculate_dynamic_indicators(warm.frame.copy(), entry)
    cold_df, cold_meta = calculate_dynamic_indicators(cold.frame.copy(), entry)

    lead_rows = int((warm.frame["timestamp"] < warm.window_start_ms).sum())
    assert warm_df[warm_meta[0]["column"]].first_valid_index() < lead_rows, "the NaN region is inside the trimmed lead-in"
    assert cold_df[cold_meta[0]["column"]].first_valid_index() > 0, "without a lead-in the NaN region is inside the window"
    assert cold.warmup.shortened and cold.warmup.admitted_bars == 0 and not warm.warmup.shortened


@respx.mock
def test_raw_and_split_adjusted_are_different_identities_and_a_split_moves_the_adjusted_prices(world):
    raw = vendor_history(MARCH, start_price=1000.0)
    raw = {**raw, **vendor_history(lead_in_sessions(MARCH[0], 1), start_price=1000.0)}
    split_day = MARCH[3]
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split])

    adj = admit(params(MARCH))
    unadj = admit(params(MARCH, price_adjustment_mode="raw"))

    before = adj.frame["timestamp"] < session_open_ms_utc(split_day)
    assert float(adj.frame.loc[before, "close"].iloc[0]) == pytest.approx(float(unadj.frame.loc[before, "close"].iloc[0]) / 10, abs=0.01)
    assert adj.frame.loc[~before, "close"].tolist() == unadj.frame.loc[~before, "close"].tolist()
    assert digests(adj)[0] != digests(unadj)[0] and digests(adj)[1] != digests(unadj)[1]
    assert set(adj.data_source()["corporate_action_versions"]) == {"SPY"}
    assert unadj.data_source()["corporate_action_versions"] == {}
    assert adj.data_source()["price_adjustment_mode"] == "polygon_split_adjusted", "the study's default is the adjusted tape"


@respx.mock
def test_prices_finer_than_the_lake_grid_move_by_at_most_half_a_ten_thousandth_and_volume_is_exact(world):
    tape = full_tape(JULY[:1], decimals=5, fractional_volume=True)
    mock_vendor("SPY", tape)

    study = admit(params(JULY[:1]))

    window_rows = study.frame[study.frame["timestamp"] >= study.window_start_ms]
    bars = [b for b in tape[JULY[0]] if session_open_ms_utc(JULY[0]) <= b["t"] < session_close_ms_utc(JULY[0])]
    assert len(window_rows) == len(bars)
    drift = max(abs(float(c) - b["c"]) for c, b in zip(window_rows["close"], bars, strict=True))
    assert 0 < drift <= 5e-5 + 1e-9, "the lake rounds half-up to 1/10,000 (lean_writer.to_deci_cent)"
    assert [Decimal(repr(v)).normalize() for v in window_rows["volume"]] == [Decimal(repr(float(b["v"]))).normalize() for b in bars]


@respx.mock
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_vendor_bars_with_a_duplicate_or_out_of_order_timestamp_never_reach_the_study(world, fault):
    tape = full_tape(MARCH[:4])
    bars = tape[MARCH[1]]
    tape[MARCH[1]] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", tape)

    refusal = refusal_of(params(MARCH[:4]))

    assert refusal.code == "data_unavailable_from_provider"
    assert [(i.group, i.trading_date) for i in refusal.coverage.issues] == [("window", MARCH[1])]
    assert "validation_failed" in refusal.coverage.issues[0].reason
    assert not lake_file(world.lake, "polygon_split_adjusted", MARCH[1]).exists()


@respx.mock
def test_a_session_that_has_not_finished_is_refused_not_read_half_done(world):
    mid_session = int(datetime(2024, 3, 15, 12, 0, tzinfo=ET).timestamp() * 1000)

    refusal = refusal_of(params(MARCH), now_ms=mid_session)

    assert refusal.code == "window_not_complete" and "2024-03-15" in refusal.reason
    assert refusal.data_source["identity_digest"] is not None and refusal.data_source["warmup"] is None


@respx.mock
def test_a_window_with_no_session_and_an_unaddressable_ticker_refuse_with_their_own_codes(world):
    assert refusal_of(params([date(2024, 3, 9), date(2024, 3, 10)])).code == "no_sessions_in_window"
    assert refusal_of(params(MARCH, symbol="BRK-B")).code == "symbol_not_addressable"


# ---------------------------------------------------------------------------
# Identity: a changed policy or admitted byte is a new identity; an identical repeat is the same one (T07, T09)
# ---------------------------------------------------------------------------


@respx.mock
def test_an_identical_repeat_has_the_identity_it_had(world):
    mock_vendor("SPY", full_tape(MARCH))

    first, second = admit(params(MARCH)), admit(params(MARCH))

    assert digests(first) == digests(second)
    assert all(len(d) == 64 for d in digests(first))
    assert first.data_source()["calculation_version"] == "indicator-reliability/1"


@respx.mock
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"indicator_params": {"length": 21}}, id="indicator_parameter"),
        pytest.param({"indicator_name": "ema", "indicator_params": {"length": 14}}, id="indicator"),
        pytest.param({"horizons": (1, 5, 10)}, id="horizons"),
        pytest.param({"include_slope": True}, id="slope"),
        pytest.param({"multiplier": 5}, id="bar_length"),
        pytest.param({"price_adjustment_mode": "raw"}, id="adjustment"),
        pytest.param({"session_policy": "extended"}, id="session"),
        pytest.param({"to_date": MARCH[-2]}, id="window_end"),
    ],
)
def test_a_changed_scientific_input_or_data_policy_is_a_new_identity(world, change):
    mock_vendor("SPY", full_tape(MARCH))
    base = admit(params(MARCH))

    changed = admit(params(MARCH, **change))

    assert digests(changed)[0] != digests(base)[0]
    assert digests(changed)[2] != digests(base)[2]


@respx.mock
def test_repaired_lake_data_under_the_same_request_is_a_new_snapshot_and_a_new_result_identity(world):
    tape = full_tape(MARCH)
    mock_vendor("SPY", tape)
    before = admit(params(MARCH))
    saved = before.data_source()
    commit_bars(world.catalog, world.lake, MARCH[3], corrected_day(MARCH[3], tape[MARCH[3]]), mode="polygon_split_adjusted")

    after = admit(params(MARCH))

    assert digests(after)[0] == digests(before)[0], "same request, same inputs"
    assert digests(after)[1] != digests(before)[1], "new bytes"
    assert digests(after)[2] != digests(before)[2]
    assert counts(after.data_source()["coverage"]["window"]) == (len(MARCH), 0, 0, 0), "a repaired day is still a held day"
    assert before.data_source() == saved, "what a run reported is not rewritten by a later repair"


@respx.mock
def test_the_warmup_policy_is_permission_not_input_so_it_leaves_the_identity_alone(world):
    window = MARCH[4:]
    mock_vendor("SPY", tape_for([*lead_in_sessions(window[0], 3), *window], regular_keep=1.0))

    allowed = admit(params(window, **EMA_200))
    required = admit(params(window, warmup_policy="require_full", **EMA_200))

    assert digests(allowed) == digests(required)
    assert (allowed.warmup.policy, required.warmup.policy) == ("allow_shortened", "require_full")
