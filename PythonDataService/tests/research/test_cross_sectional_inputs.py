"""Admitted lake stock bars for the cross-sectional study (#3524; PRD #3490 R12-R15, R25, T05, T08, T18).

Every test here crosses the real seams, as ``tests/research/test_study_inputs.py`` does for the minute window: pinned
vendor bars are served at the vendor's HTTP boundary, the real ``ensure_data`` fetches and validates them, the real
lake writer encodes them, the catalog is the in-memory stand-in the lake suites share, and admission reads them back
through committed-byte admission and the corporate-action receipt check into a temporary lake root. The study's
daily rows are then folded from what admission handed back. No test hands the study the bars it expects.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import respx

from app.data_lake import run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.data_lake.capture_finality import capture_final_ms
from app.data_lake.path_policy import lake_root_for
from app.engine.data.lean_format import LeanDailyDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.cross_sectional_inputs import (
    AdmittedMember,
    CrossSectionalIdentity,
    UniverseAdmission,
    admit_universe,
    cross_sectional_identity,
    daily_rows,
    refusal_code_for,
    stock_bars_block,
    universe_coverage,
    universe_refusal,
)
from app.research.lake_study import FoldedStudyIdentity, data_source_block
from app.research.study_inputs import MinuteWindowIdentity, StudyInputRefusal, admit_minute_inputs
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, lake_file, minute_bar
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history

ET = ZoneInfo("America/New_York")

#: 2025-01-01 00:00 UTC: after every window used here, so each session is complete and in the provider's history.
NOW_MS = 1_735_689_600_000
#: Five years on: the 2024 windows below are older than the provider's history, so only held days can be read.
LATER_MS = 1_893_456_000_000

MARCH = expected_sessions(date(2024, 3, 7), date(2024, 3, 13))  # Mar 10 is the spring-forward Sunday
JULY = expected_sessions(date(2024, 7, 1), date(2024, 7, 5))  # Jul 3 is an early close; Jul 4 a holiday


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def identity_for(
    symbols: list[str],
    days: list[date],
    *,
    session: str = "regular",
    mode: str = "raw",
) -> CrossSectionalIdentity:
    return cross_sectional_identity(
        symbols, days[0], days[-1], timespan="day", multiplier=1, session_policy=session, price_adjustment_mode=mode  # type: ignore[arg-type]
    )


def admit(identity: CrossSectionalIdentity, *, now_ms: int = NOW_MS) -> UniverseAdmission:
    return admit_universe(identity, requester="test", now_ms=now_ms)


def block_of(identity: CrossSectionalIdentity, admission: UniverseAdmission) -> dict:
    return stock_bars_block(identity, admission, calculation_version="test/1")


def member_counts(admission: UniverseAdmission, ticker: str) -> tuple[int, int, int, int]:
    """(reused, added, missing, failed) of the member's window."""
    group = admission.coverage[ticker].window
    return group.reused, group.added, group.missing, group.failed


# ---------------------------------------------------------------------------
# T05: held, one gap, fully cold, corrupt adjustment, daily rollup
# ---------------------------------------------------------------------------


@respx.mock
def test_a_cold_universe_is_fetched_per_member_and_each_member_session_counts_as_added(catalog, lake):
    mock_launcher()
    calls = {"SPY": VendorCalls(), "QQQ": VendorCalls()}
    tapes = {"SPY": vendor_history(MARCH, seed=3), "QQQ": vendor_history(MARCH, seed=4, start_price=400.0)}
    for symbol, tape in tapes.items():
        mock_vendor(symbol, tape, calls=calls[symbol])

    admission = admit(identity_for(["SPY", "QQQ"], MARCH))

    assert list(admission.members) == ["SPY", "QQQ"] and admission.excluded == {}
    for symbol in ("SPY", "QQQ"):
        assert member_counts(admission, symbol) == (0, len(MARCH), 0, 0)
        assert calls[symbol].days() == MARCH, "each missing session of each member costs exactly one vendor call"
        assert len(admission.members[symbol].rows) == len(MARCH), "one daily row per session"
    block = block_of(identity_for(["SPY", "QQQ"], MARCH), admission)
    assert block["kind"] == "lake"
    assert block["coverage"]["window"] == {
        "expected_sessions": 2 * len(MARCH), "reused": 0, "added": 2 * len(MARCH), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert [m["status"] for m in block["members"]] == ["admitted", "admitted"]


@respx.mock
def test_the_stock_bars_block_carries_every_key_of_the_shared_lake_data_source_block_and_states_what_a_universe_changes(catalog, lake):
    """The browser reads this block with the mapper every lake study shares, so it must hold the shared block's keys."""
    mock_launcher()
    for symbol, seed in (("SPY", 3), ("QQQ", 4)):
        mock_vendor(symbol, vendor_history(MARCH, seed=seed))
    identity = identity_for(["SPY", "QQQ"], MARCH)
    admission = admit(identity)

    block = block_of(identity, admission)
    one = admission.members["SPY"].admitted
    shared = data_source_block(FoldedStudyIdentity(identity.member("SPY"), 1), calculation_version="test/1", coverage=one.coverage, admitted=one)

    assert set(shared) - set(block) == {"data_root_id", "corporate_action_versions"}, "all but the two a member's own receipt carries"
    for member in block["members"]:
        assert {"data_root_id", "corporate_action_versions"} <= set(member)
    assert block["symbol"] == "SPY, QQQ" and block["bar_minutes"] is None and block["calculation_version"] == "test/1"
    assert block["effective_window"] == {"start_ms": et_midnight_ms(MARCH[0]), "end_ms": et_midnight_ms(MARCH[-1])}
    assert block["bars_used"] == 2 * len(MARCH) and block["minute_bars_read"] == sum(len(m.admitted.window_bars) for m in admission.members.values())
    assert block["cache"] == {"hit": False, "cached_at_ms": None}


@respx.mock
def test_a_universe_refusal_is_the_shared_refusal_with_the_members_sessions_summed_and_named(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", {day: bars for day, bars in vendor_history(MARCH, seed=3).items() if day != MARCH[1]})
    mock_vendor("QQQ", {day: bars for day, bars in vendor_history(MARCH, seed=4).items() if day not in (MARCH[2], MARCH[3])})
    identity = identity_for(["SPY", "QQQ"], MARCH)
    admission = admit(identity)

    refusal = universe_refusal(identity, admission)
    payload = refusal.as_dict()

    assert admission.members == {} and refusal.code == "data_unavailable_from_provider"
    assert set(payload) == {"code", "reason", "sessions", "sessions_total", "coverage"}, "the shape every lake refusal has"
    assert payload["sessions_total"] == 3 and payload["coverage"]["window"]["failed"] == 3
    assert payload["coverage"]["window"]["expected_sessions"] == 2 * len(MARCH)
    assert [s["reason"].split(":")[0] for s in payload["sessions"]] == ["SPY", "QQQ", "QQQ"], "each session names whose it is"
    assert universe_coverage(admission).window.failed == 3
    assert "any of the 2 requested members" in refusal.reason and "SPY: " in refusal.reason and "QQQ: " in refusal.reason


@respx.mock
def test_a_held_universe_reuses_every_member_session_without_asking_the_vendor(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    first = admit(identity_for(["SPY"], MARCH))
    calls_after_first = calls.count

    second = admit(identity_for(["SPY"], MARCH))

    assert member_counts(second, "SPY") == (len(MARCH), 0, 0, 0)
    assert calls.count == calls_after_first, "a held window must cost the vendor nothing"
    assert second.members["SPY"].admitted.snapshot_digest == first.members["SPY"].admitted.snapshot_digest
    assert second.members["SPY"].rows == first.members["SPY"].rows


@respx.mock
def test_one_gap_is_filled_and_counted_apart_from_the_sessions_already_held(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    admit(identity_for(["SPY"], MARCH[:2]))
    admit(identity_for(["SPY"], MARCH[3:]))
    calls.requests.clear()

    admission = admit(identity_for(["SPY"], MARCH))

    assert member_counts(admission, "SPY") == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]], "only the gap is fetched"


@respx.mock
def test_a_member_the_vendor_cannot_fill_is_excluded_with_its_reason_and_the_others_are_kept(catalog, lake):
    """T18: one unavailable member. It is named, with a code and the counts, and the rest of the universe is admitted."""
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH, seed=3))
    qqq = vendor_history(MARCH, seed=4, start_price=400.0)
    mock_vendor("QQQ", {day: bars for day, bars in qqq.items() if day != MARCH[2]})

    admission = admit(identity_for(["SPY", "QQQ"], MARCH))

    assert list(admission.members) == ["SPY"]
    excluded = admission.excluded["QQQ"]
    assert (excluded.stage, excluded.code, excluded.retryable) == ("stock_bars", "data_unavailable_from_provider", True)
    assert MARCH[2].isoformat() in excluded.reason
    assert member_counts(admission, "QQQ") == (0, len(MARCH) - 1, 0, 1)
    assert [(s["outcome"], s["group"]) for s in excluded.refusal["sessions"]] == [("unavailable_from_provider", "window")]
    block = block_of(identity_for(["SPY", "QQQ"], MARCH), admission)
    by_ticker = {m["ticker"]: m for m in block["members"]}
    assert by_ticker["SPY"]["status"] == "admitted" and by_ticker["QQQ"]["status"] == "excluded"
    assert by_ticker["QQQ"]["coverage"]["window"]["failed"] == 1
    assert block["coverage"]["window"]["failed"] == 1 and block["coverage"]["window"]["added"] == 2 * len(MARCH) - 1
    assert block["snapshot_digest"] is not None


@respx.mock
def test_a_universe_nothing_can_serve_admits_no_member_and_names_each_reason(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", {})
    mock_vendor("QQQ", {})

    admission = admit(identity_for(["SPY", "QQQ"], MARCH))

    assert admission.members == {}
    assert set(admission.excluded) == {"SPY", "QQQ"}
    assert {e.code for e in admission.excluded.values()} == {"data_unavailable_from_provider"}
    assert refusal_code_for(admission.excluded) == "data_unavailable_from_provider"
    assert block_of(identity_for(["SPY", "QQQ"], MARCH), admission)["snapshot_digest"] is None
    assert block_of(identity_for(["SPY", "QQQ"], MARCH), admission)["effective_window"] is None, "nothing admitted, no window read"


def _capture_before_final(catalog: FakeCatalog, symbol: str, day: date) -> dict:
    """Make the catalog say ``symbol``'s held ``day`` was captured a minute before its regular session was final."""
    row = next(
        r
        for r in catalog.rows.values()
        if r["symbol"] == symbol and r["trading_date"] == day and r["resolution"] == "minute"
    )
    row["completed_at_ms"] = capture_final_ms(day, "regular") - 1
    return row


@respx.mock
def test_a_member_whose_held_day_was_captured_before_it_was_final_is_excluded_and_the_vendor_is_not_asked(catalog, lake):
    """#3521: an early-captured day is refused, never refetched, and a universe of such members still names the code."""
    mock_launcher()
    calls = {"SPY": VendorCalls(), "QQQ": VendorCalls()}
    mock_vendor("SPY", vendor_history(MARCH, seed=3), calls=calls["SPY"])
    mock_vendor("QQQ", vendor_history(MARCH, seed=4, start_price=400.0), calls=calls["QQQ"])
    admit(identity_for(["SPY", "QQQ"], MARCH))
    day = MARCH[2]
    qqq_row = _capture_before_final(catalog, "QQQ", day)
    row_before = dict(qqq_row)
    for log in calls.values():
        log.requests.clear()

    admission = admit(identity_for(["SPY", "QQQ"], MARCH))

    assert list(admission.members) == ["SPY"]
    excluded = admission.excluded["QQQ"]
    assert (excluded.stage, excluded.code, excluded.retryable) == ("stock_bars", "captured_before_final", True)
    assert day.isoformat() in excluded.reason
    assert member_counts(admission, "QQQ") == (len(MARCH) - 1, 0, 0, 1), "the day is neither added nor read"
    assert [(s["outcome"], s["group"]) for s in excluded.refusal["sessions"]] == [("captured_before_final", "window")]
    assert all(log.count == 0 for log in calls.values()), "the study asks the vendor for nothing"
    assert qqq_row == row_before, "the shared day is as committed as it was"
    window = block_of(identity_for(["SPY", "QQQ"], MARCH), admission)["coverage"]["window"]
    assert (window["captured_before_final"], window["failed"]) == (1, 1), "the universe's counts carry the early day"

    _capture_before_final(catalog, "SPY", day)
    nothing = admit(identity_for(["SPY", "QQQ"], MARCH))

    assert nothing.members == {} and {e.code for e in nothing.excluded.values()} == {"captured_before_final"}
    assert refusal_code_for(nothing.excluded) == "captured_before_final"
    refusal = universe_refusal(identity_for(["SPY", "QQQ"], MARCH), nothing)
    assert refusal.code == "captured_before_final" and refusal.coverage.window.captured_before_final == 2


@respx.mock
def test_a_retained_day_older_than_the_provider_floor_stays_readable_and_a_missing_one_excludes_the_member(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    admit(identity_for(["SPY"], MARCH[:3]))
    calls.requests.clear()

    held = admit(identity_for(["SPY"], MARCH[:3]), now_ms=LATER_MS)
    short = admit(identity_for(["SPY"], MARCH), now_ms=LATER_MS)

    assert member_counts(held, "SPY") == (3, 0, 0, 0), "retained history outlives the provider's window"
    assert short.members == {} and short.excluded["SPY"].code == "data_unavailable_from_provider"
    assert member_counts(short, "SPY") == (3, 0, 0, len(MARCH) - 3)
    assert calls.count == 0, "a day the provider no longer serves is never asked of it"


def _corrupt_adjustment_record(lake: Path, day: date, symbol: str = "SPY") -> None:
    record = companion_path(lake_file(lake, "polygon_split_adjusted", day, symbol))
    record.write_text(record.read_text().replace('"corporate_action_version": "', '"corporate_action_version": "0'))


@respx.mock
def test_an_adjusted_day_with_a_bad_adjustment_record_is_rebuilt_and_counted_as_added(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), adjusted=vendor_history(MARCH), calls=calls)
    identity = identity_for(["SPY"], MARCH, mode="polygon_split_adjusted")
    admit(identity)
    _corrupt_adjustment_record(lake, MARCH[2])
    calls.requests.clear()

    admission = admit(identity)

    assert member_counts(admission, "SPY") == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]], "only the day whose record failed is fetched again"


@respx.mock
def test_a_bad_adjustment_record_on_a_day_the_provider_no_longer_serves_excludes_the_member_as_corrupt(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH), adjusted=vendor_history(MARCH))
    identity = identity_for(["SPY"], MARCH, mode="polygon_split_adjusted")
    admit(identity)
    _corrupt_adjustment_record(lake, MARCH[2])

    admission = admit(identity, now_ms=LATER_MS)

    excluded = admission.excluded["SPY"]
    assert excluded.code == "data_corrupt" and "AdjustmentVersionError" in excluded.reason
    assert member_counts(admission, "SPY") == (len(MARCH) - 1, 0, 0, 1)


def test_a_window_ending_on_an_unfinished_session_excludes_every_member_with_a_structural_code(catalog, lake):
    mid_session = int(datetime(2024, 3, 13, 12, 0, tzinfo=ET).timestamp() * 1000)

    admission = admit(identity_for(["SPY", "QQQ"], MARCH), now_ms=mid_session)

    assert admission.members == {}
    assert {e.code for e in admission.excluded.values()} == {"window_not_complete"}
    assert not any(e.retryable for e in admission.excluded.values()), "no second attempt can finish a session"
    assert "2024-03-13" in admission.excluded["SPY"].reason


@pytest.mark.parametrize(
    ("timespan", "multiplier"), [("minute", 1), ("hour", 1), ("day", 5), ("minute", 15)], ids=lambda v: str(v)
)
def test_a_resolution_other_than_daily_is_refused_before_the_lake_is_touched(catalog, lake, timespan, multiplier):
    with pytest.raises(StudyInputRefusal) as raised:
        cross_sectional_identity(["SPY"], MARCH[0], MARCH[-1], timespan=timespan, multiplier=multiplier, session_policy="regular")

    assert raised.value.code == "resolution_unsupported"
    assert "daily bars only" in raised.value.reason
    assert catalog.rows == {}, "a request that can never run costs the lake nothing"


# ---------------------------------------------------------------------------
# Daily rows are the lake's own reduction
# ---------------------------------------------------------------------------


def _rollup_rows(symbol: str, mode: str) -> list[dict]:
    """The daily rows the lake's own whole-symbol rollup holds, read back through the daily reader."""
    reader = LeanDailyDataReader(resolve_data_roots(source="polygon", adjusted=mode != "raw"))
    return [
        {"day": datetime.fromtimestamp(b.start_ms / 1000, ET).date(), "open": float(b.open), "high": float(b.high),
         "low": float(b.low), "close": float(b.close), "volume": float(b.volume)}
        for b in reader.iter_bars(symbol, date(2000, 1, 1), date(2100, 1, 1))
    ]


@respx.mock
def test_daily_rows_equal_the_lakes_own_daily_rollup(catalog, lake):
    """Same canonical input, same output, bit for bit: under ``extended`` the study's rows are the rollup's rows.

    The rollup is built by the real materializer for a backtest over the same window; the study never reads it.
    Prices are on the lake's 1/10,000 grid and volumes are exact decimals, so float equality is the right test.
    """
    mock_launcher()
    mock_vendor("SPY", vendor_history(JULY, fractional_volume=True))
    spec = run_materialization._build_engine_run_spec(symbol="SPY", start=JULY[0], end=JULY[-1], requester="test")
    asyncio.run(run_materialization._materialize_run_data(spec, resolution="minute"))

    minutes = admit_minute_inputs(
        MinuteWindowIdentity("SPY", "raw", "extended", JULY[0], JULY[-1]), requester="test", now_ms=NOW_MS
    )
    study = daily_rows(minutes.window_bars)
    rollup = _rollup_rows("SPY", "raw")
    assert [r["day"] for r in rollup] == JULY and len(study) == len(rollup)
    for ours, theirs in zip(study, rollup, strict=True):
        for column in ("open", "high", "low", "close", "volume"):
            assert ours[column] == theirs[column], f"{theirs['day']} {column}"
    assert [r["timestamp"] for r in study] == [session_open_ms_utc(day) for day in JULY]


@respx.mock
def test_a_stale_daily_rollup_neither_blocks_the_study_nor_is_touched_by_it(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    narrow = run_materialization._build_engine_run_spec(symbol="SPY", start=MARCH[0], end=MARCH[1], requester="test")
    asyncio.run(run_materialization._materialize_run_data(narrow, resolution="minute"))
    rollups = lambda: [r for r in catalog.rows.values() if r["resolution"] == "daily"]  # noqa: E731
    assert len(rollups()) == 1
    rollup_before = dict(rollups()[0])

    admission = admit(identity_for(["SPY"], MARCH))

    assert member_counts(admission, "SPY") == (2, len(MARCH) - 2, 0, 0)
    assert rollups() == [rollup_before], "the study left the daily rollup exactly as it found it"


def _pandas_daily(bars: list[VendorBar], day: date, *, regular_only: bool) -> dict[str, float]:
    """An independent reference: filter the vendor's minutes to the session by the calendar, then first/max/min/last/sum."""
    open_ms, close_ms = session_open_ms_utc(day), session_close_ms_utc(day)
    frame = pd.DataFrame([b for b in bars if not regular_only or open_ms <= b["t"] < close_ms]).sort_values("t")
    return {
        "open": float(frame["o"].iloc[0]), "high": float(frame["h"].max()), "low": float(frame["l"].min()),
        "close": float(frame["c"].iloc[-1]), "volume": float(frame["v"].sum()),
    }


@respx.mock
@pytest.mark.parametrize("window", [MARCH, JULY], ids=["dst_week", "early_close_week"])
def test_daily_rows_match_an_independent_reference_across_dst_and_an_early_close(catalog, lake, window):
    """The session policy decides which minutes a day holds; the canonical calendar decides when the session ends."""
    mock_launcher()
    tape = vendor_history(window, seed=9, start_price=75.0)
    mock_vendor("SPY", tape)

    rows = admit(identity_for(["SPY"], window)).members["SPY"].rows

    assert [r["timestamp"] for r in rows] == [session_open_ms_utc(day) for day in window]
    for row, day in zip(rows, window, strict=True):
        expected = _pandas_daily(tape[day], day, regular_only=True)
        for column, value in expected.items():
            np.testing.assert_allclose(row[column], value, atol=1e-9, rtol=0, err_msg=f"{day} {column}")
    if window is JULY:
        early = JULY.index(date(2024, 7, 3))
        assert datetime.fromtimestamp(session_close_ms_utc(JULY[early]) / 1000, ET).hour == 13, "the calendar says 13:00"
        assert date(2024, 7, 4) not in {datetime.fromtimestamp(r["timestamp"] / 1000, ET).date() for r in rows}


def test_daily_rows_fill_nothing_and_a_session_without_a_minute_has_no_row():
    from app.engine.data.trade_bar import TradeBar

    first, third = date(2024, 3, 11), date(2024, 3, 13)
    price = Decimal("10")

    def minute(day: date, offset: int, bump: int) -> TradeBar:
        start = session_open_ms_utc(day) + offset * 60_000
        return TradeBar(symbol="SPY", start_ms=start, end_ms=start + 60_000, open=price, high=price + bump, low=price - bump, close=price + bump, volume=Decimal(bump + 1))

    rows = daily_rows([minute(first, 0, 1), minute(first, 1, 3), minute(third, 5, 2)])

    assert [r["timestamp"] for r in rows] == [session_open_ms_utc(first), session_open_ms_utc(third)], "the 12th has no minute"
    assert (rows[0]["open"], rows[0]["high"], rows[0]["low"], rows[0]["close"], rows[0]["volume"]) == (10.0, 13.0, 7.0, 13.0, 6.0)
    assert daily_rows([]) == []


# ---------------------------------------------------------------------------
# Data the lake must not accept, and data it must keep exactly
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_vendor_bars_with_a_duplicate_or_out_of_order_timestamp_exclude_the_member_and_never_reach_the_lake(catalog, lake, fault):
    mock_launcher()
    tape = vendor_history(MARCH)
    bars = tape[MARCH[1]]
    tape[MARCH[1]] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", tape)

    admission = admit(identity_for(["SPY"], MARCH[:3]))

    assert admission.members == {}
    excluded = admission.excluded["SPY"]
    assert excluded.code == "data_unavailable_from_provider" and "validation_failed" in excluded.reason
    assert not lake_file(lake, "raw", MARCH[1]).exists(), "a corrupt tape never reaches the lake"


@respx.mock
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_a_committed_file_with_duplicate_or_out_of_order_rows_excludes_the_member_as_corrupt(catalog, lake, fault):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    admit(identity_for(["SPY"], MARCH[:3]))
    day = MARCH[1]
    rows = [minute_bar(day, 10, 0), minute_bar(day, 10, 1), minute_bar(day, 10, 2)]
    rows = [*rows, rows[2]] if fault == "duplicate" else [rows[0], rows[2], rows[1]]
    commit_bars(catalog, lake, day, rows)

    admission = admit(identity_for(["SPY"], MARCH[:3]))

    assert admission.excluded["SPY"].code == "data_corrupt"
    assert "strictly increasing" in admission.excluded["SPY"].reason


@respx.mock
def test_prices_finer_than_the_lake_grid_round_half_up_to_the_ten_thousandth_before_the_fold(catalog, lake):
    """The lake's price encoding has a precision boundary (1/10,000, half up). It applies to every minute, then the day folds."""
    mock_launcher()
    tape = vendor_history(JULY[:1], decimals=5, start_price=12.0)
    mock_vendor("SPY", tape)
    quantize = lambda value: float(Decimal(repr(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))  # noqa: E731

    row = admit(identity_for(["SPY"], JULY[:1])).members["SPY"].rows[0]

    bars = [b for b in tape[JULY[0]] if session_open_ms_utc(JULY[0]) <= b["t"] < session_close_ms_utc(JULY[0])]
    assert row["high"] == max(quantize(b["h"]) for b in bars) and row["low"] == min(quantize(b["l"]) for b in bars)
    assert row["open"] == quantize(bars[0]["o"]) and row["close"] == quantize(bars[-1]["c"])
    drift = max(abs(row["high"] - max(b["h"] for b in bars)), abs(row["low"] - min(b["l"] for b in bars)), abs(row["close"] - bars[-1]["c"]))
    assert drift <= 5e-5 + 1e-9, "a day's extreme moves by at most half a ten-thousandth"


@respx.mock
def test_fractional_volume_sums_exactly(catalog, lake):
    mock_launcher()
    tape = vendor_history(JULY[:1], fractional_volume=True)
    mock_vendor("SPY", tape)

    row = admit(identity_for(["SPY"], JULY[:1])).members["SPY"].rows[0]

    exact = sum(Decimal(repr(b["v"])) for b in tape[JULY[0]] if session_open_ms_utc(JULY[0]) <= b["t"] < session_close_ms_utc(JULY[0]))
    assert row["volume"] == float(exact), "six-decimal volume survives the text encoding and sums as an exact decimal"
    assert row["volume"] != int(row["volume"])


@respx.mock
def test_raw_and_split_adjusted_windows_are_different_identities_with_different_daily_rows(catalog, lake):
    """A 10-for-1 split lands on Mar 12: the adjusted tape divides the earlier days' prices by ten."""
    mock_launcher()
    raw = vendor_history(MARCH, start_price=1000.0)
    split_day = MARCH[3]
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split])
    raw_identity = identity_for(["SPY"], MARCH, mode="raw")
    adjusted_identity = identity_for(["SPY"], MARCH, mode="polygon_split_adjusted")

    raw_in, adjusted_in = admit(raw_identity), admit(adjusted_identity)

    raw_rows, adjusted_rows = raw_in.members["SPY"].rows, adjusted_in.members["SPY"].rows
    before = [i for i, day in enumerate(MARCH) if day < split_day]
    for i in before:
        assert adjusted_rows[i]["close"] == pytest.approx(raw_rows[i]["close"] / 10, abs=0.01)
    after = [i for i in range(len(MARCH)) if i not in before]
    assert [adjusted_rows[i]["close"] for i in after] == [raw_rows[i]["close"] for i in after]
    assert raw_identity.digest() != adjusted_identity.digest()
    assert raw_in.members["SPY"].admitted.snapshot_digest != adjusted_in.members["SPY"].admitted.snapshot_digest
    assert raw_in.members["SPY"].admitted.snapshot.corporate_action_versions == {}
    assert set(adjusted_in.members["SPY"].admitted.snapshot.corporate_action_versions) == {"SPY"}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_identity_digest_follows_every_scientific_input_and_ignores_member_order():
    base = identity_for(["SPY", "QQQ"], MARCH)

    assert base.digest() == identity_for(["QQQ", "SPY"], MARCH).digest(), "the order a universe is typed in is not science"
    for changed in (
        identity_for(["SPY", "QQQ"], MARCH, mode="polygon_split_adjusted"),
        identity_for(["SPY", "QQQ"], MARCH[:-1]),
        identity_for(["SPY", "IWM"], MARCH),
    ):
        assert changed.digest() != base.digest()
    wire = base.as_dict()
    assert wire["window_start_ms"] < wire["window_end_ms"] and wire["bar_resolution"] == "day"
    assert wire["bar_timestamp_anchor"] == "session_open" and wire["base_resolution"] == "minute"


def test_an_admitted_member_reports_its_own_digests_and_counts(catalog, lake):
    with respx.mock:
        mock_launcher()
        mock_vendor("SPY", vendor_history(MARCH))
        admission = admit(identity_for(["SPY"], MARCH))

    member: AdmittedMember = admission.members["SPY"]
    described = member.as_dict()
    assert described["status"] == "admitted" and described["daily_bars"] == len(MARCH)
    assert len(described["snapshot_digest"]) == 64 and len(described["identity_digest"]) == 64
    assert described["minute_bars_read"] == len(member.admitted.window_bars)
    assert lake_root_for(lake_file(lake, "raw", MARCH[0])) is not None
