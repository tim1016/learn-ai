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
   forward-label tail) is pinned case by case, with the cause named in the
   reconcile taxonomy. The whole-payload provider-versus-lake comparison lives
   in ``test_signal_study_lake_vs_provider.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import respx

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.features.ta_features import TechnicalFeatures
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs, bars_as_rows, fold_minute_bars
from app.research.target import compute_15min_forward_return, compute_forward_log_return
from app.services.signal_study_service import (
    MAX_BAR_MINUTES,
    SignalStudyParams,
    cache_params,
    max_target_share,
    serialize_signal_report,
    signal_study_identity,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests.research.test_feature_study import ATOL, RTOL, assert_same, vendor_rows

ET = ZoneInfo("America/New_York")
#: 2025-02-03: after every window below, so each session is complete and inside the provider's history.
NOW_MS = 1_738_540_800_000
#: Four months, Sep 3 to Dec 31 2024: DST ends Nov 3, Nov 28 and Dec 25 are holidays, Nov 29 and Dec 24 close at 13:00.
LONG = expected_sessions(date(2024, 9, 3), date(2024, 12, 31))
#: Ten sessions across the spring-forward Sunday (Mar 10).
SHORT = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))
EARLY_CLOSES = [date(2024, 11, 29), date(2024, 12, 24)]

#: Dense enough that the target clears the engine's 70% validity gate at 15-minute bars; the gate is not under test
#: where these are used (and is measured on its own below).
DENSE = {"regular_keep": 0.995, "extended_keep": 0.92}


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


def study(window: list[date], *, multiplier: int = 15, session: str = "extended", **over: Any) -> SignalStudyParams:
    return SignalStudyParams(
        "SPY", over.pop("feature_name", "momentum_5m"), window[0], window[-1], "minute", multiplier,
        session_policy=session, **over,  # type: ignore[arg-type]
    )


def lake_rows(params: SignalStudyParams) -> tuple[list[dict], Any]:
    identity = signal_study_identity(params)
    admitted = admit_minute_inputs(identity.inputs, requester="test", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes)), admitted


def engine_report(rows: list[dict], params: SignalStudyParams):
    return run_signal_engine(
        "SPY", params.feature_name, rows, params.from_date.isoformat(), params.to_date.isoformat(), params.config()
    )


# ---------------------------------------------------------------------------
# The bars: the lake hands back the vendor's own on-grid bars, at every supported size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplier", [1, 3, 5, 15, 20])
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

    returns = compute_15min_forward_return(rows, SignalConfig().horizon)
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
    returns = compute_15min_forward_return(rows, SignalConfig().horizon)
    return float(returns.notna().mean())


@pytest.mark.parametrize(
    ("session", "multiplier", "share", "clears_the_gate"),
    [
        # Extended hours: 16 hours of bars a day, so a 15-bar target is valid for most of them at every size.
        ("extended", 1, 0.98, True),
        ("extended", 5, 0.92, True),
        ("extended", 15, 0.766, True),
        # Regular hours: 390 minutes. 15-minute bars leave 26 a day and 15 of them look past the close.
        ("regular", 1, 0.96, True),
        ("regular", 5, 0.81, True),
        ("regular", 15, 11 / 26, False),
    ],
)
def test_regular_hours_cut_the_valid_targets_and_at_15_minutes_below_the_gate(
    monkeypatch, tmp_path, session, multiplier, share, clears_the_gate
):
    """Cause: ``data-quality`` (session scope), measured on the pinned tape. The choice between the two is the owner's (D02).

    The shares are of bars with a valid 15-bar target; the engine refuses a series under 70% (``validate_return_series``).
    """
    window = LONG[:45]  # long enough that even 26 regular 15-minute bars a day clear the engine's 500-bar minimum
    with respx.mock:
        mock_launcher()
        _serve(monkeypatch, tmp_path, window)
        params = study(window, multiplier=multiplier, session=session)
        rows, _ = lake_rows(params)

        measured = _share_valid(rows)
        report = engine_report(rows, params)

    assert measured == pytest.approx(share, abs=0.012)
    assert (measured >= 0.70) is clears_the_gate
    if clears_the_gate:
        assert report.error is None
    else:
        assert report.error == "Forward return series failed validation", "the default run would stop producing results"


# ---------------------------------------------------------------------------
# #3527: the bar sizes the target can never be formed on
# ---------------------------------------------------------------------------


def _continuous_days(bar_minutes: int, days: int = 3) -> list[dict]:
    """Gap-free bars across whole New York dates: the most targets a bar size can ever have."""
    start = et_midnight_ms(date(2024, 3, 4))
    rows = []
    for day in range(days):
        midnight = start + day * 86_400_000
        for index in range(-(-1440 // bar_minutes)):
            rows.append({"timestamp": midnight + index * bar_minutes * 60_000, "close": 100.0 + 0.01 * index + day})
    return rows


@pytest.mark.parametrize("bar_minutes", [1, 5, 15, 20, 29, 30, 60, 240])
def test_the_ceiling_on_valid_targets_is_what_the_target_function_gives_on_gap_free_bars(bar_minutes):
    ceiling = max_target_share(bar_minutes, SignalConfig().horizon)

    measured = _share_valid(_continuous_days(bar_minutes))

    assert measured <= ceiling + 1e-12, "no data can beat the ceiling"
    if 1440 % bar_minutes == 0:
        assert measured == pytest.approx(ceiling, abs=1e-12, rel=0), "and gap-free whole-day bars reach it exactly"


def test_the_longest_bar_that_can_reach_the_gate_is_derived_not_chosen():
    assert MAX_BAR_MINUTES == 29
    assert max_target_share(MAX_BAR_MINUTES, 15) >= 0.70 > max_target_share(MAX_BAR_MINUTES + 1, 15)
    assert [max_target_share(m, 15) for m in (60, 240)] == [0.375, 0.0]


@pytest.mark.parametrize(
    ("timespan", "multiplier", "per_day"),
    [("hour", 1, 24), ("day", 1, 1), ("minute", 30, 48), ("minute", 60, 24), ("minute", 240, 6)],
)
def test_bar_sizes_that_can_never_reach_the_gate_refuse_with_a_reason_that_names_the_numbers(timespan, multiplier, per_day):
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], timespan, multiplier))

    assert raised.value.code == "resolution_unsupported"
    assert f"a day holds at most {per_day} of them" in raised.value.reason
    assert "needs 70%" in raised.value.reason and f"up to {MAX_BAR_MINUTES} minutes" in raised.value.reason
    assert raised.value.coverage is None


def test_a_bar_unit_the_study_does_not_read_refuses_with_a_reason():
    with pytest.raises(StudyInputRefusal) as raised:
        signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], "week", 1))

    assert raised.value.code == "resolution_unsupported" and "'week'" in raised.value.reason


@pytest.mark.parametrize("multiplier", [1, 3, 5, 15, 20, 29])
def test_every_minute_bar_that_can_reach_the_gate_is_accepted(multiplier):
    identity = signal_study_identity(SignalStudyParams("SPY", "momentum_5m", SHORT[0], SHORT[-1], "minute", multiplier))

    assert identity.bar_minutes == multiplier
    assert identity.look_forward_bars == 15
    assert identity.as_dict()["warmup_sessions"] == 0, "the study fetches no warmup"
    assert identity.as_dict()["look_forward_extends_window"] is False


# ---------------------------------------------------------------------------
# What identifies a result
# ---------------------------------------------------------------------------


def test_regular_hours_is_a_different_sample_and_a_different_cache_identity(short_tape):
    extended_rows, extended = lake_rows(study(SHORT, multiplier=5, session="extended"))
    regular_rows, regular = lake_rows(study(SHORT, multiplier=5, session="regular"))

    extended_identity = signal_study_identity(study(SHORT, session="extended"))
    regular_identity = signal_study_identity(study(SHORT, session="regular"))

    assert len(regular_rows) < len(extended_rows), "extended hours add pre-market and after-hours bars"
    assert extended_identity.digest() != regular_identity.digest()
    config = study(SHORT).config()
    assert cache_params(extended_identity, extended, config) != cache_params(regular_identity, regular, config)
    assert extended_identity.inputs.session_policy == "extended", "the default is the session the study has always computed on"
