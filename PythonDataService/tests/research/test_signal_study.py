"""The Signal study computes from admitted lake inputs and gives the numbers it gave on the same bars (#3522; R14, T08).

Two claims, kept apart as PRD #3490 R14 requires.

1. *Same canonical input, same outputs.* The signal engine is not touched by the
   source change. Given the same bars it gives the same report, at the repo's
   strict-float default (``atol=1e-9, rtol=0``). The bars here travel the real
   lake path (vendor HTTP, ``ensure_data``, the lake writer, committed-byte
   admission, the reader) and are compared with the vendor's own, on a grid the
   lake holds exactly. The window is long enough for the engine to reach its
   walk-forward folds, and crosses the fall-back DST change, two early closes
   and two holidays.
2. *What the source change itself moves* (precision, adjustment, session, the
   forward-label tail, the bar lengths the study now refuses) is pinned case by
   case, with the cause named in the reconcile taxonomy. The whole-payload
   provider-versus-lake comparison lives in ``test_signal_study_lake_vs_provider.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import respx

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.features.ta_features import TechnicalFeatures
from app.research.signal.config import SignalConfig
from app.research.study_inputs import StudyInputRefusal
from app.research.target import compute_15min_forward_return, compute_forward_log_return
from app.services.signal_study_service import (
    HOUR_ALIGNED_BAR_MINUTES,
    SignalStudyParams,
    bar_minutes_that_reach_the_gate,
    bars_in_span,
    max_target_share,
    serialize_signal_report,
    session_minute_spans,
    signal_study,
    signal_study_identity,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import ATOL, DENSE, RTOL, assert_same, cache_key_of, vendor_rows
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests._helpers.signal_study import EARLY_CLOSES, LONG, SHORT, engine_report, lake_rows, study, ungated_rows

ET = ZoneInfo("America/New_York")
HORIZON = SignalConfig().horizon

#: The vendor's tape and the exchange's session, as New York minutes past midnight: 04:00-19:59 and 09:30-15:59.
EXTENDED_SPAN = (240, 1199)
REGULAR_SPAN = (570, 959)
#: 13:00 close.
EARLY_CLOSE_SPAN = (570, 779)


def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, window: list[date], **options: Any) -> dict[date, list[VendorBar]]:
    install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    tape = vendor_history(window, seed=11, start_price=150.0, **{**DENSE, **options})
    mock_vendor("SPY", tape)
    return tape


@pytest.fixture
def short_tape(monkeypatch: pytest.MonkeyPatch, tmp_path):
    with respx.mock:
        mock_launcher()
        yield _serve(monkeypatch, tmp_path, SHORT)


@pytest.fixture
def long_tape(monkeypatch: pytest.MonkeyPatch, tmp_path):
    with respx.mock:
        mock_launcher()
        yield _serve(monkeypatch, tmp_path, LONG)


def _extended_lengths() -> tuple[int, ...]:
    return bar_minutes_that_reach_the_gate(session_minute_spans("extended", SHORT[0], SHORT[-1]), HORIZON)


# ---------------------------------------------------------------------------
# The bars: the lake hands back the vendor's own on-grid bars, at every supported size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplier", _extended_lengths())
def test_lake_bars_equal_the_vendors_own_bars_for_a_price_grid_the_lake_holds(short_tape, multiplier):
    rows, _ = lake_rows(study(SHORT, multiplier=multiplier))

    expected = vendor_rows(short_tape, bar_minutes=multiplier)

    assert [r["timestamp"] for r in rows] == [r["timestamp"] for r in expected]
    for column in ("open", "high", "low", "close", "volume"):
        np.testing.assert_allclose([r[column] for r in rows], [r[column] for r in expected], atol=ATOL, rtol=RTOL)


# ---------------------------------------------------------------------------
# Same canonical input -> same outputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"multiplier": 15}, id="default_15_minute_momentum"),
        pytest.param(
            {"multiplier": 5, "feature_name": "rsi_14", "flip_sign": False, "regime_gate_enabled": False},
            id="5_minute_rsi_ungated_unflipped",
        ),
    ],
)
def test_the_whole_report_is_the_same_on_lake_bars_as_on_the_vendors_bars(long_tape, options):
    params = study(LONG, **options)
    rows, _ = lake_rows(params)
    expected = vendor_rows(long_tape, bar_minutes=params.multiplier)

    ours = engine_report(rows, params)
    reference = engine_report(expected, params)

    assert ours.error is None and reference.error is None, (ours.error, reference.error)
    assert ours.walk_forward is not None and len(ours.walk_forward.windows) >= 1, "the walk-forward folds are exercised"
    assert ours.bars_used == len(expected)
    assert_same(serialize_signal_report(ours), serialize_signal_report(reference))


def test_the_window_crosses_the_dst_change_the_early_closes_and_the_holidays(long_tape):
    assert all(day in LONG for day in EARLY_CLOSES)
    assert date(2024, 11, 28) not in LONG and date(2024, 12, 25) not in LONG, "Thanksgiving and Christmas are not sessions"
    for day in EARLY_CLOSES:
        assert datetime.fromtimestamp(session_close_ms_utc(day) / 1000, ET).hour == 13, "the calendar names the early close"
    before, after = date(2024, 11, 1), date(2024, 11, 4)
    assert (session_open_ms_utc(before) % 86_400_000) != (session_open_ms_utc(after) % 86_400_000), "DST moved the UTC open"

    rows, _ = lake_rows(study(LONG))

    days = {datetime.fromtimestamp(r["timestamp"] / 1000, ET).date() for r in rows}
    assert days == set(LONG), "every scheduled session contributes bars and no other date does"


def test_a_hole_in_the_tape_is_a_hole_in_the_bars_and_costs_the_targets_that_straddle_it(short_tape):
    day = SHORT[2]
    hole_start = session_open_ms_utc(day) + 30 * 60_000
    hole_end = hole_start + 30 * 60_000
    holed = {d: [b for b in bars if not (hole_start <= b["t"] < hole_end)] for d, bars in short_tape.items()}
    respx.mock.reset()
    mock_launcher()
    mock_vendor("SPY", holed)

    params = study(SHORT, multiplier=5)
    rows, _ = lake_rows(params)

    expected = vendor_rows(holed, bar_minutes=5)
    assert [r["timestamp"] for r in rows] == [r["timestamp"] for r in expected], "nothing is filled, on either path"
    assert not any(hole_start <= r["timestamp"] < hole_end for r in rows)
    target = compute_forward_log_return(rows, horizon_minutes=75, bar_minutes=5)
    assert target.invalid_reason_counts["window_gap"] > 0, "a target whose forward bar is past the hole is masked, not bridged"


@pytest.mark.parametrize(
    ("feature", "first_valid"),
    [
        ("momentum_5m", 5),
        # pandas-ta seeds Wilder's RSI at the first difference, so it emits from the second bar. The study has
        # always run on that; the region is pinned so a change to it is seen, not absorbed.
        ("rsi_14", 1),
        ("macd_signal", 33),
    ],
)
def test_the_first_valid_value_sits_where_the_indicator_windows_say_and_nothing_is_read_before_the_window(
    short_tape, feature, first_valid
):
    rows, _ = lake_rows(study(SHORT, multiplier=5))

    values = TechnicalFeatures.compute_feature(feature, rows)

    # No warmup is fetched before the window, so each indicator's first valid value is its own window into the
    # window: the series runs continuous across the overnight gap, as it has always done.
    assert values.first_valid_index() == first_valid
    assert values.iloc[:first_valid].isna().all() and values.iloc[first_valid:].notna().all()
    assert datetime.fromtimestamp(rows[0]["timestamp"] / 1000, ET).date() == SHORT[0]
    assert signal_study_identity(study(SHORT)).inputs.warmup_sessions == 0


@pytest.mark.parametrize("multiplier", [1, 5, 15])
def test_no_bar_is_read_past_the_requested_end_so_the_last_fifteen_bars_have_no_target(short_tape, multiplier):
    rows, admitted = lake_rows(study(SHORT, multiplier=multiplier))

    returns = compute_15min_forward_return(rows, HORIZON)
    target = compute_forward_log_return(rows, horizon_minutes=15 * multiplier, bar_minutes=multiplier)

    assert returns.iloc[-15:].isna().all(), "the horizon is 15 bars, whatever the bar length"
    assert target.horizon_bars == 15 and target.horizon_minutes == 15 * multiplier
    assert target.invalid_reason_counts["window_runs_off_end"] == 15
    assert max(r["timestamp"] for r in rows) <= max(b.start_ms for b in admitted.window_bars)
    assert all(datetime.fromtimestamp(r["timestamp"] / 1000, ET).date() <= SHORT[-1] for r in rows)
    assert target.invalid_reason_counts.get("cross_session", 0) >= (len(SHORT) - 1) * 14, (
        "each session's last bars look into the next session and are masked, not filled"
    )


# ---------------------------------------------------------------------------
# The session policy (D02): what extended and regular hours each do to the target and the gate
# ---------------------------------------------------------------------------


def _share_valid(rows: list[dict]) -> float:
    return float(compute_15min_forward_return(rows, HORIZON).notna().mean())


def _measured_against_the_refusal(session: str, cases: list[tuple[int, float, bool]], monkeypatch, tmp_path) -> None:
    """Fold one pinned tape to each length and hold the study's gate against what the bars and the engine say."""
    window = LONG[:45]  # long enough that even 26 regular 15-minute bars a day clear the engine's 500-bar minimum
    with respx.mock:
        mock_launcher()
        _serve(monkeypatch, tmp_path, window)
        for multiplier, share, accepted in cases:
            params = study(window, multiplier=multiplier, session=session)
            rows = ungated_rows(params, multiplier)
            measured = _share_valid(rows)
            spans = session_minute_spans(session, window[0], window[-1])

            # Measured on the seeded tape: every bar of a gap-free bucket is present, so all but the 1-minute share sit
            # on the ceiling. The bound is the measured value to four places; it moves only if the tape or the fold does.
            assert measured == pytest.approx(share, abs=1e-4, rel=0), f"{session} {multiplier}-minute share"
            assert measured <= max(max_target_share(multiplier, HORIZON, *span) for span in spans) + 1e-12, (
                "no data beats the ceiling"
            )
            report = engine_report(rows, params)
            if accepted:
                signal_study_identity(params)
                assert (measured >= 0.70) and report.error is None, f"{session} {multiplier}: accepted, so it runs"
            else:
                with pytest.raises(StudyInputRefusal) as raised:
                    signal_study_identity(params)
                assert raised.value.code == "resolution_unsupported"
                assert measured < 0.70, f"{session} {multiplier}: refused only what the engine would stop"
                assert report.error == "Forward return series failed validation", "the run the refusal saves"


def test_extended_hours_clear_the_gate_to_15_minutes_and_not_at_20(monkeypatch, tmp_path):
    """Cause: ``data-quality`` (session scope), measured on the pinned tape. The choice between the two is the owner's (D02).

    The shares are of bars with a valid 15-bar target; the engine refuses a series under 70% (``validate_return_series``).
    """
    _measured_against_the_refusal(
        "extended",
        [(1, 0.9824, True), (5, 0.9219, True), (15, 0.7656, True), (20, 0.6875, False)],
        monkeypatch,
        tmp_path,
    )


def test_regular_hours_cut_the_valid_targets_and_at_15_minutes_below_the_gate(monkeypatch, tmp_path):
    """Cause: ``data-quality`` (session scope). Regular hours leave 26 fifteen-minute bars a day and 15 of them look
    past the close: the default run would stop producing results, so it is refused up front instead (D02)."""
    _measured_against_the_refusal(
        "regular",
        [(1, 0.9613, True), (5, 0.8077, True), (6, 0.7692, True), (10, 0.6154, False), (15, 0.4231, False)],
        monkeypatch,
        tmp_path,
    )


# ---------------------------------------------------------------------------
# #3527: the bar sizes the target can never be formed on
# ---------------------------------------------------------------------------


def _continuous_days(bar_minutes: int, first: int, last: int, days: int = 3) -> list[dict]:
    """Gap-free bars on the grid of a span of the day, repeated for whole dates: the most targets a bar size can have."""
    start = et_midnight_ms(date(2024, 3, 4))
    rows = []
    for day in range(days):
        midnight = start + day * 86_400_000
        for index, minute in enumerate(range(first // bar_minutes * bar_minutes, last + 1, bar_minutes)):
            rows.append({"timestamp": midnight + minute * 60_000, "close": 100.0 + 0.01 * index + day})
    return rows


@pytest.mark.parametrize(
    ("bar_minutes", "span"),
    [
        *[(minutes, (0, 1439)) for minutes in (1, 5, 15, 20, 29, 30, 60, 240)],
        *[(minutes, REGULAR_SPAN) for minutes in (1, 5, 6, 10, 15, 20)],
        *[(minutes, EARLY_CLOSE_SPAN) for minutes in (4, 5)],
        *[(minutes, EXTENDED_SPAN) for minutes in (15, 20)],
    ],
)
def test_the_ceiling_on_valid_targets_is_what_the_target_function_gives_on_gap_free_bars(bar_minutes, span):
    ceiling = max_target_share(bar_minutes, HORIZON, *span)

    measured = _share_valid(_continuous_days(bar_minutes, *span))

    assert measured <= ceiling + 1e-12, "no data can beat the ceiling"
    if 1440 % bar_minutes == 0:
        assert measured == pytest.approx(ceiling, abs=1e-12, rel=0), "and gap-free bars on the grid reach it exactly"


def test_the_bars_of_a_span_are_counted_on_the_grid_that_starts_at_midnight():
    assert bars_in_span(15, *REGULAR_SPAN) == 26
    assert bars_in_span(20, *REGULAR_SPAN) == 20, "the 09:30 minute sits in the 09:20 bar"
    assert bars_in_span(4, *REGULAR_SPAN) == 98
    assert bars_in_span(7, 0, 1439) == 206 == -(-1440 // 7), "the whole day is ceil(1440 / m) for any length"


def test_the_spans_come_from_the_calendar_and_do_not_move_with_the_clock_change():
    assert session_minute_spans("regular", SHORT[0], SHORT[-1]) == {REGULAR_SPAN}, "across the spring-forward Sunday"
    assert session_minute_spans("regular", LONG[0], LONG[-1]) == {REGULAR_SPAN, EARLY_CLOSE_SPAN}, "across fall back"
    assert session_minute_spans("extended", SHORT[0], SHORT[-1]) == {EXTENDED_SPAN}
    assert session_minute_spans("extended", LONG[0], LONG[-1]) == {EXTENDED_SPAN, (240, 1019)}, "17:00 on an early close"
    assert session_minute_spans("regular", date(2024, 3, 9), date(2024, 3, 10)) == frozenset(), "a weekend holds no session"


@pytest.mark.parametrize(
    ("session", "window", "reach"),
    [
        pytest.param("extended", SHORT, (1, 2, 3, 4, 5, 6, 10, 12, 15), id="extended_hours"),
        pytest.param("regular", SHORT, (1, 2, 3, 4, 5, 6), id="regular_hours"),
        pytest.param("regular", LONG, (1, 2, 3, 4, 5, 6), id="regular_hours_with_early_closes_among_full_days"),
        pytest.param("regular", [EARLY_CLOSES[0]], (1, 2, 3, 4), id="regular_hours_on_an_early_close_alone"),
    ],
)
def test_the_bar_lengths_that_reach_the_gate_are_derived_from_the_sessions_the_window_holds(session, window, reach):
    spans = session_minute_spans(session, window[0], window[-1])

    assert bar_minutes_that_reach_the_gate(spans, HORIZON) == reach
    assert set(reach) <= set(HOUR_ALIGNED_BAR_MINUTES)
    for minutes in HOUR_ALIGNED_BAR_MINUTES:
        best = max(max_target_share(minutes, HORIZON, *span) for span in spans)
        assert (best >= 0.70) is (minutes in reach), minutes


@pytest.mark.parametrize(
    ("timespan", "multiplier", "per_day"),
    [("hour", 1, 24), ("day", 1, 1), ("minute", 30, 48), ("minute", 45, 32), ("minute", 60, 24), ("minute", 240, 6)],
)
def test_bar_sizes_no_session_can_use_refuse_with_a_reason_that_names_the_numbers(timespan, multiplier, per_day):
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], timespan, multiplier))

    assert raised.value.code == "resolution_unsupported"
    assert f"a day holds at most {per_day} of them" in raised.value.reason
    assert "needs 70%" in raised.value.reason
    assert "Use minute bars of 1, 2, 3, 4, 5, 6, 10, 12, 15 minutes." in raised.value.reason
    assert raised.value.coverage is None


NOT_ON_THE_HOUR = [minutes for minutes in range(1, 30) if 60 % minutes]


def test_there_are_nineteen_minute_lengths_up_to_29_that_do_not_divide_the_hour():
    assert NOT_ON_THE_HOUR == [7, 8, 9, 11, 13, 14, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29]


@pytest.mark.parametrize("multiplier", NOT_ON_THE_HOUR)
def test_a_bar_length_that_does_not_divide_the_hour_is_refused_before_the_lake_is_touched(multiplier):
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], "minute", multiplier))

    assert raised.value.code == "resolution_unsupported"
    assert f"{multiplier}-minute bars do not divide the hour" in raised.value.reason
    assert "Use minute bars of 1, 2, 3, 4, 5, 6, 10, 12, 15 minutes." in raised.value.reason
    assert raised.value.coverage is None


@pytest.mark.parametrize(
    ("session", "multiplier", "longest", "share"),
    [
        ("regular", 15, 26, "42.3"),
        ("regular", 10, 39, "61.5"),
        ("regular", 20, 20, "25.0"),
        ("extended", 20, 48, "68.8"),
    ],
)
def test_a_length_the_sessions_of_the_policy_cannot_fill_is_refused_naming_the_session(
    session, multiplier, longest, share
):
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(study(SHORT, multiplier=multiplier, session=session))

    reason = raised.value.reason
    assert raised.value.code == "resolution_unsupported"
    assert f"under {'regular' if session == 'regular' else 'extended'}-hours sessions" in reason
    assert f"the longest session in this window holds at most {longest} of them" in reason
    assert f"at most {share}% of bars can have a target" in reason and "needs 70%" in reason
    assert raised.value.coverage is None


def test_a_session_gate_that_depends_on_the_window_follows_the_calendars_early_close():
    early = [EARLY_CLOSES[0]]

    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(study(early, multiplier=5, session="regular"))
    assert "holds at most 42 of them" in raised.value.reason, "a 13:00 close leaves 42 five-minute bars"
    assert signal_study_identity(study(SHORT, multiplier=5, session="regular")).bar_minutes == 5


def test_a_window_with_no_session_leaves_the_refusal_to_admission():
    weekend = [date(2024, 3, 9), date(2024, 3, 10)]

    identity = signal_study_identity(study(weekend, multiplier=15, session="regular"))

    assert identity.bar_minutes == 15, "no session to measure: admission refuses it as no_sessions_in_window"


def test_a_bar_unit_the_study_does_not_read_refuses_with_a_reason():
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], "week", 1))

    assert raised.value.code == "resolution_unsupported" and "'week'" in raised.value.reason


@pytest.mark.parametrize("multiplier", _extended_lengths())
def test_every_hour_aligned_minute_bar_that_can_reach_the_gate_is_accepted(multiplier):
    identity = signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], "minute", multiplier))

    assert identity.bar_minutes == multiplier
    assert identity.look_forward_bars == 15
    assert identity.as_dict()["warmup_sessions"] == 0, "the study fetches no warmup"
    assert identity.as_dict()["look_forward_extends_window"] is False


def test_the_look_forward_in_the_identity_is_what_the_engine_is_configured_with():
    params = study(SHORT)

    assert signal_study_identity(params).look_forward_bars == params.config().horizon


def _first_bucket_minutes(rows: list[dict]) -> dict[date, int]:
    """For each session, the minute of the New York day its first folded bar starts at."""
    first: dict[date, int] = {}
    for row in rows:
        local = datetime.fromtimestamp(row["timestamp"] / 1000, ET)
        first.setdefault(local.date(), local.hour * 60 + local.minute)
    return first


def test_a_bar_length_off_the_hour_starts_its_bars_at_a_different_minute_each_session_which_is_why_it_is_refused(short_tape):
    """Cause: ``timestamp`` (bar alignment). The fold floors the exchange clock in whole bars counted from 1970."""
    params = study(SHORT)

    drift = {minutes: _first_bucket_minutes(ungated_rows(params, minutes)) for minutes in (7, 29)}
    steady = {minutes: _first_bucket_minutes(ungated_rows(params, minutes)) for minutes in _extended_lengths()}

    for minutes, firsts in drift.items():
        assert len(set(firsts.values())) > 1, f"{minutes}-minute bars start at {sorted(set(firsts.values()))} minutes past midnight"
    assert len(set(drift[7].values())) >= 3, "7-minute bars open at three different minutes across the ten sessions"
    for minutes, firsts in steady.items():
        assert set(firsts.values()) == {EXTENDED_SPAN[0] // minutes * minutes}, f"{minutes}-minute bars start on the same minute"


# ---------------------------------------------------------------------------
# What identifies a result
# ---------------------------------------------------------------------------


def test_regular_hours_is_a_different_sample_and_a_different_cache_identity(short_tape):
    extended_rows, extended = lake_rows(study(SHORT, multiplier=5, session="extended"))
    regular_rows, regular = lake_rows(study(SHORT, multiplier=5, session="regular"))

    extended_identity = signal_study_identity(study(SHORT, multiplier=5, session="extended"))
    regular_identity = signal_study_identity(study(SHORT, multiplier=5, session="regular"))

    assert len(regular_rows) < len(extended_rows), "extended hours add pre-market and after-hours bars"
    assert extended_identity.digest() != regular_identity.digest()
    assert cache_key_of(signal_study(study(SHORT, multiplier=5)), extended_identity, extended) != cache_key_of(
        signal_study(study(SHORT, multiplier=5)), regular_identity, regular
    )
    assert extended_identity.inputs.session_policy == "extended", "the default is the session the study has always computed on"
