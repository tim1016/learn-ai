"""The Realized vs IV study computes from admitted lake inputs and gives the numbers it gave on the same bars (#3525; R14, T08).

Two claims, kept apart as PRD #3490 R14 requires.

1. *Same canonical input, same outputs.* The estimators and the composition are not touched by the source change.
   Given the same bars and IV they give the same series, at the repo's strict-float default (``atol=1e-9, rtol=0``).
   The bars here travel the real lake path (vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
   the reader, the fold) and are compared with the vendor's own, on a price grid the lake holds exactly. The reference
   side calls the estimators directly, as the retired route did: ``Validated against`` in
   ``app/engine/edge/realized_vs_iv_series.py`` names this file.
2. *What the source change itself moves* (precision, the sanitizer, the session, the window end) is pinned case by
   case in ``test_realized_vs_iv_lake_vs_provider.py``, with the cause named in the reconcile taxonomy.

Not provable offline: that Polygon's own 15-minute and day aggregates equal a fold of its minute aggregates. The
vendor side here is an independent stand-in (``tests/_helpers/realized_vs_iv.py``) for both, and a day bar is taken
to be the regular session, as the census could not confirm. Both are assumptions of the lake path, unverified
against live data.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import respx

from app.engine.edge.features_realtime.hf_realized_vol import hf_realized_vol_trd252
from app.engine.edge.features_realtime.realized_vol import yang_zhang
from app.engine.edge.labels_oracle.forward_rv import forward_rv
from app.engine.edge.labels_oracle.hf_forward_rv import hf_forward_rv_trd252
from app.engine.edge.vrp import compute_vrp, vrp_signal
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.lake_study import StudyHooks, StudyRun
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs, bars_as_rows, fold_minute_bars
from app.services.iv_recorder import InMemoryIvSnapshotStore, RecordedIvSnapshot, get_iv_store, set_iv_store
from app.services.realized_vs_iv_study_service import (
    BAR_MINUTES,
    DAY_BAR_MINUTES,
    DEFAULT_SESSION_POLICY,
    RealizedVsIvParams,
    bar_ends_ms,
    compute_study,
    last_final_session,
    realized_vs_iv_identity,
    resolve_window,
)
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms
from app.volatility.basis import convert_iv_act365_to_trading252
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests._helpers.realized_vs_iv import aggregates_as_rows, frame_of, vendor_aggregates

#: The repo's strict-float default for indicators and closed-form calculations.
ATOL = 1e-9
RTOL = 0.0

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01

#: Sixty-odd sessions across the 2024-03-10 spring-forward Sunday and Good Friday (2024-03-29): the daily window.
DAILY_WINDOW = expected_sessions(date(2024, 1, 2), date(2024, 4, 5))
#: Twenty-seven sessions across the same DST change: the 15-minute window.
INTRADAY_WINDOW = expected_sessions(date(2024, 2, 20), date(2024, 4, 1))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))
#: Every session any test here reads, so one pinned tape serves them all.
ALL_SESSIONS = sorted({*DAILY_WINDOW, *INTRADAY_WINDOW, *HALF_DAY_WINDOW})

#: Dense enough that gaps exist but never empty a whole 15-minute bucket of the regular session.
DENSE = {"regular_keep": 0.995, "extended_keep": 0.92}


@pytest.fixture
def lake_vendor(monkeypatch: pytest.MonkeyPatch, tmp_path):
    install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    with respx.mock:
        mock_launcher()
        tape = vendor_history(ALL_SESSIONS, seed=11, start_price=150.0, **DENSE)
        mock_vendor("SPY", tape, adjusted=tape)
        yield tape


@pytest.fixture
def recorder_store():
    new = InMemoryIvSnapshotStore()
    original = get_iv_store()
    set_iv_store(new)
    try:
        yield new
    finally:
        set_iv_store(original)


def window_of(bar_size: str) -> tuple[date, date]:
    sessions = DAILY_WINDOW if bar_size == "1d" else INTRADAY_WINDOW
    return sessions[0], sessions[-1]


def params(bar_size: str = "1d", **overrides: Any) -> RealizedVsIvParams:
    base: dict[str, Any] = {"symbol": "SPY", "bar_size": bar_size, "window": window_of(bar_size)}
    return RealizedVsIvParams(**{**base, **overrides})


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def study_run(p: RealizedVsIvParams) -> StudyRun:
    """The run the shared runner hands the study's compute: the real admission, then the real fold."""
    identity = realized_vs_iv_identity(p, now_ms=NOW_MS)
    admitted = admit_minute_inputs(identity.inputs, requester="test", now_ms=NOW_MS)
    rows = bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes))
    return StudyRun(identity, admitted, rows, StudyHooks(_noop, _noop, _noop, lambda: False))


def result_of(p: RealizedVsIvParams) -> dict[str, Any]:
    return compute_study(study_run(p))


# ---------------------------------------------------------------------------
# The vendor's side of the comparison
# ---------------------------------------------------------------------------


def vendor_bars(tape: dict[date, list[VendorBar]], window: list[date], *, bar_size: str, regular_only: bool) -> list[dict]:
    """The vendor's own aggregates of ``bar_size`` as study rows; see ``tests/_helpers/realized_vs_iv.py`` for the assumptions."""
    return aggregates_as_rows(vendor_aggregates(tape, window, bar_size=bar_size, regular_only=regular_only))


def reference_series(
    frame: pd.DataFrame, *, bar_size: str, tenor_days: int, hf_session: str, windows: list[int], iv: dict[int, float]
) -> dict[str, np.ndarray]:
    """The series the retired route computed, chained from the estimators directly.

    This is the pre-#3525 composition re-stated step by step; it shares no code with ``compute_realized_vs_iv_series``
    beyond the estimators themselves, so a mistake in the composition's plumbing (a window, a bars-per-year, a
    session, a basis conversion) shows here.
    """
    bars_per_year = 252 if bar_size == "1d" else 252 * 26
    out: dict[str, np.ndarray] = {}
    for w in windows:
        out[f"trail_{w}"] = yang_zhang(frame, window=w, annualize=True, bars_per_year=bars_per_year).to_numpy()
        out[f"fwd_{w}"] = forward_rv(frame, estimator="yz", window=w, annualize=True, bars_per_year=bars_per_year).to_numpy()
    if bar_size == "15m":
        utc = frame.copy()
        utc.index = pd.to_datetime(frame.index, unit="ms", utc=True)
        out["hf_trailing"] = hf_realized_vol_trd252(utc, window_trading_days=21, session=hf_session).to_numpy()  # type: ignore[arg-type]
        out["hf_forward"] = hf_forward_rv_trd252(utc, window_trading_days=21, session=hf_session).to_numpy()  # type: ignore[arg-type]
    else:
        out["hf_trailing"] = yang_zhang(frame, window=21, annualize=True, bars_per_year=252).to_numpy()
        out["hf_forward"] = forward_rv(frame, estimator="yz", window=21, annualize=True, bars_per_year=252).to_numpy()
    iv30 = np.array([iv.get(int(t), np.nan) for t in frame.index])
    trd = np.array([convert_iv_act365_to_trading252(float(v), int(t), tenor_days) if not np.isnan(v) else np.nan for v, t in zip(iv30, frame.index, strict=True)])
    out["iv30"], out["iv30_trd252"] = iv30, trd
    out["vrp_forward"] = compute_vrp(pd.Series(trd), pd.Series(out["hf_forward"])).to_numpy()
    out["vrp_z"] = vrp_signal(
        iv=pd.Series(trd).ffill(), rv=pd.Series(out["hf_forward"]), lookback=min(252, len(frame) - 1)
    ).vrp_z.to_numpy()
    return out


def assert_same(ours: list[float | None] | np.ndarray, reference: np.ndarray, where: str) -> None:
    """Equal at the strict-float default, NaN placement included: a warmup region is part of the answer."""
    ours_array = np.array([np.nan if v is None else v for v in ours], dtype=float)
    assert ours_array.shape == reference.shape, f"{where}: {ours_array.shape} != {reference.shape}"
    assert np.isnan(ours_array).tolist() == np.isnan(reference).tolist(), f"{where}: the NaN region moved"
    np.testing.assert_allclose(ours_array, reference, atol=ATOL, rtol=RTOL, equal_nan=True, err_msg=where)


def iv_every_third_bar(frame: pd.DataFrame) -> dict[int, float]:
    return {int(t): 0.18 + 0.01 * (i % 7) for i, t in enumerate(frame.index) if i % 3 == 0}


# ---------------------------------------------------------------------------
# The bars: the lake hands back the vendor's own on-grid bars at both sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_lake_bars_equal_the_vendors_own_bars_for_a_price_grid_the_lake_holds(lake_vendor, bar_size):
    p = params(bar_size)
    run = study_run(p)
    policy = realized_vs_iv_identity(p, now_ms=NOW_MS).inputs.session_policy

    expected = vendor_bars(lake_vendor, expected_sessions(*window_of(bar_size)), bar_size=bar_size, regular_only=policy == "regular")

    assert [r["timestamp"] for r in run.rows] == [r["timestamp"] for r in expected]
    for column in ("open", "high", "low", "close", "volume"):
        np.testing.assert_allclose([r[column] for r in run.rows], [r[column] for r in expected], atol=ATOL, rtol=RTOL, err_msg=column)


def test_a_daily_bar_is_stamped_at_et_midnight_of_its_session_one_per_session_with_no_holiday(lake_vendor):
    run = study_run(params("1d"))

    stamps = [r["timestamp"] for r in run.rows]
    assert stamps == [et_midnight_ms(day) for day in DAILY_WINDOW]
    assert et_midnight_ms(date(2024, 3, 29)) not in stamps, "Good Friday has no bar"
    assert len(stamps) == len(DAILY_WINDOW)


# ---------------------------------------------------------------------------
# Same canonical input -> same series
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bar_size", "hf_session"), [("15m", "ETH"), ("15m", "RTH"), ("1d", "ETH")], ids=["15m-ETH", "15m-RTH", "1d"]
)
def test_the_whole_series_is_the_same_on_lake_bars_as_on_the_vendors_bars(lake_vendor, recorder_store, bar_size, hf_session):
    p = params(bar_size, hf_session=hf_session)
    identity = realized_vs_iv_identity(p, now_ms=NOW_MS)
    vendor = frame_of(vendor_bars(lake_vendor, expected_sessions(*window_of(bar_size)), bar_size=bar_size, regular_only=identity.inputs.session_policy == "regular"))
    iv = iv_every_third_bar(vendor)
    p = params(bar_size, hf_session=hf_session, iv_series=[{"ts": ts, "iv30": value} for ts, value in iv.items()])

    ours = result_of(p)
    reference = reference_series(vendor, bar_size=bar_size, tenor_days=30, hf_session=hf_session, windows=[5, 10, 30], iv=iv)

    assert ours["ts"] == vendor.index.tolist()
    for w in (5, 10, 30):
        assert_same(ours["rv_trailing"][f"yz_{w}"], reference[f"trail_{w}"], f"yz_{w} trailing")
        assert_same(ours["rv_forward"][f"yz_{w}"], reference[f"fwd_{w}"], f"yz_{w} forward")
    assert_same(ours["rv_hf_trailing"], reference["hf_trailing"], "HF trailing")
    assert_same(ours["rv_hf_forward"], reference["hf_forward"], "HF forward")
    assert_same(ours["iv30"], reference["iv30"], "iv30")
    assert_same(ours["iv30_trd252"], reference["iv30_trd252"], "iv30 on the TRD/252 basis")
    assert_same(ours["vrp_forward"], reference["vrp_forward"], "VRP forward")
    assert_same(ours["vrp_z"], reference["vrp_z"], "VRP z-score")
    assert ours["coverage"]["forward_nan_bars"] == int(np.isnan(reference["hf_forward"]).sum())
    assert sum(v is not None for v in ours["iv30"]) == len(iv), "every IV stamped at a bar start reached its bar"


def test_the_first_valid_value_sits_where_the_estimator_windows_say_and_the_region_before_it_is_nan(lake_vendor):
    """Yang-Zhang over a window of w bars needs w overnight returns and the first bar has none, so bar w is the first
    valid one; the 21-bar fallback that drives daily VRP starts at bar 21. No warmup is fetched before the window."""
    ours = result_of(params("1d"))

    for window in (5, 10, 30):
        series = ours["rv_trailing"][f"yz_{window}"]
        assert all(v is None for v in series[:window]) and all(v is not None for v in series[window:]), f"yz_{window}"
    assert all(v is None for v in ours["rv_hf_trailing"][:21]) and all(v is not None for v in ours["rv_hf_trailing"][21:])


def test_the_hf_estimator_on_fifteen_minute_bars_is_valid_from_the_twenty_first_session(lake_vendor):
    ours = result_of(params("15m"))
    starts = ours["ts"]
    first_bar_of_session_21 = next(i for i, t in enumerate(starts) if et_date_at_ms(t) == INTRADAY_WINDOW[20])

    trailing = ours["rv_hf_trailing"]
    assert all(v is None for v in trailing[:first_bar_of_session_21])
    assert all(v is not None for v in trailing[first_bar_of_session_21:])


# ---------------------------------------------------------------------------
# Requested end and the forward-label tail
# ---------------------------------------------------------------------------


def test_no_bar_is_read_past_the_requested_end_so_the_unrealized_forward_tail_is_unavailable_never_filled(lake_vendor):
    p = params("1d")
    ours = result_of(p)

    forward = ours["rv_hf_forward"]
    assert et_date_at_ms(ours["ts"][-1]) == DAILY_WINDOW[-1], "the last bar is the requested end"
    assert all(v is None for v in forward[-21:]), "the last 21 sessions have no realized forward window"
    assert all(v is not None for v in forward[:-21]), "every earlier bar does"
    assert ours["coverage"]["forward_nan_bars"] == 21
    assert all(ours["rv_forward"]["yz_5"][-5:][i] is None for i in range(5)), "the same holds for each estimator's own window"


def test_the_z_score_of_the_unrealized_tail_is_not_a_signal(lake_vendor):
    """On fifteen-minute bars the z-score has a long enough window to exist, so this is not vacuously true."""
    p = params("15m", iv_series=[{"ts": r["timestamp"], "iv30": 0.2 + 0.01 * (i % 5)} for i, r in enumerate(study_run(params("15m")).rows)])

    ours = result_of(p)

    blind = [i for i, v in enumerate(ours["rv_hf_forward"]) if v is None]
    realized = [i for i, v in enumerate(ours["rv_hf_forward"]) if v is not None]
    assert blind and realized and blind[-1] == len(ours["ts"]) - 1
    assert any(z is not None for z in ours["vrp_z"]), "earlier bars keep their z-score"
    assert all(ours["vrp_z"][i] is None for i in blind)
    assert all(ours["vrp_forward"][i] is None for i in blind), "and neither is the premium itself"


def test_a_window_that_ends_on_a_session_still_forming_refuses_instead_of_ending_short(lake_vendor):
    mid_session = int(datetime(2024, 4, 5, 12, 0, tzinfo=ET).timestamp() * 1000)
    identity = realized_vs_iv_identity(params("1d"), now_ms=mid_session)

    with pytest.raises(StudyInputRefusal) as raised:
        admit_minute_inputs(identity.inputs, requester="test", now_ms=mid_session)

    assert raised.value.code == "window_not_complete"
    assert "2024-04-05" in raised.value.reason


# ---------------------------------------------------------------------------
# Early close, DST and gaps come from the canonical calendar and the tape
# ---------------------------------------------------------------------------


def test_the_spring_forward_day_is_twenty_three_hours_long_and_a_daily_bar_ends_at_the_next_et_midnight(lake_vendor):
    ours = result_of(params("1d"))
    starts = ours["ts"]
    ends = bar_ends_ms(starts, DAY_BAR_MINUTES)

    spring_forward = starts.index(et_midnight_ms(date(2024, 3, 8))), starts.index(et_midnight_ms(date(2024, 3, 11)))
    assert ends[spring_forward[0]] == et_midnight_ms(date(2024, 3, 9)), "an ordinary day: the next midnight"
    assert et_day_end_ms(date(2024, 3, 10)) - et_midnight_ms(date(2024, 3, 10)) == 23 * 3_600_000
    assert ends[spring_forward[1]] - starts[spring_forward[1]] == 24 * 3_600_000
    assert all(end > start for start, end in zip(starts, ends, strict=True)) and all(
        ends[i] <= starts[i + 1] for i in range(len(starts) - 1)
    ), "the intervals never overlap, so a snapshot belongs to one bar at most"


def test_fifteen_minute_bars_start_on_the_wall_clock_quarter_hours_across_the_dst_change_and_open_at_the_calendars_pre_market(lake_vendor):
    ours = result_of(params("15m"))
    starts = ours["ts"]

    assert all(datetime.fromtimestamp(t / 1000, ET).minute % 15 == 0 and t % 60_000 == 0 for t in starts)
    for day in INTRADAY_WINDOW:
        first = next(t for t in starts if et_date_at_ms(t) == day)
        assert first == session_open_ms_utc(day) - 330 * 60_000, f"{day}: the extended session opens 330 minutes before the open"
        last = max(t for t in starts if et_date_at_ms(t) == day)
        assert last < session_close_ms_utc(day) + 240 * 60_000


@pytest.mark.parametrize("policy", ["regular", "extended"])
def test_the_early_close_day_is_one_daily_bar_that_ends_with_the_calendars_close(lake_vendor, policy):
    p = params("1d", window=(HALF_DAY_WINDOW[0], HALF_DAY_WINDOW[-1]), session_policy=policy)
    run = study_run(p)
    half_day = date(2024, 7, 3)

    (row,) = [r for r in run.rows if r["timestamp"] == et_midnight_ms(half_day)]
    in_policy = [b for b in lake_vendor[half_day] if policy == "extended" or b["t"] < session_close_ms_utc(half_day)]
    assert row["close"] == in_policy[-1]["c"], "the close is the last minute the policy admits"
    assert row["high"] == max(b["h"] for b in in_policy)
    assert et_midnight_ms(date(2024, 7, 4)) not in [r["timestamp"] for r in run.rows], "the holiday has no bar"
    assert session_close_ms_utc(half_day) == session_open_ms_utc(half_day) + 210 * 60_000, "13:00 ET: the calendar's close"


def test_a_gap_in_the_minute_tape_is_not_filled_so_a_bucket_with_no_minutes_has_no_bar(lake_vendor):
    day = INTRADAY_WINDOW[3]
    gap_start = at_et(day, 11, 0)
    gapped = {d: [b for b in bars if not (d == day and gap_start <= b["t"] < gap_start + 15 * 60_000)] for d, bars in lake_vendor.items()}
    respx.mock.reset()
    mock_launcher()
    mock_vendor("SPY", gapped, adjusted=gapped)

    run = study_run(params("15m", session_policy="extended"))

    stamps = [r["timestamp"] for r in run.rows]
    assert gap_start not in stamps, "the empty 11:00 bucket yields no bar: nothing is filled"
    assert gap_start - 15 * 60_000 in stamps and gap_start + 15 * 60_000 in stamps, "its neighbours are untouched"
    assert len(stamps) == len(set(stamps)) and stamps == sorted(stamps)
    expected = vendor_bars(gapped, INTRADAY_WINDOW, bar_size="15m", regular_only=False)
    assert stamps == [r["timestamp"] for r in expected]


# ---------------------------------------------------------------------------
# The daily fold: an independent pandas reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["regular", "extended"])
def test_the_daily_fold_matches_an_independent_pandas_reference_across_dst_and_an_early_close(lake_vendor, policy):
    window = sorted({*expected_sessions(date(2024, 3, 7), date(2024, 3, 13)), *HALF_DAY_WINDOW})
    bars = []
    for chunk in (expected_sessions(date(2024, 3, 7), date(2024, 3, 13)), HALF_DAY_WINDOW):
        identity = realized_vs_iv_identity(params("1d", window=(chunk[0], chunk[-1]), session_policy=policy), now_ms=NOW_MS)
        bars.extend(admit_minute_inputs(identity.inputs, requester="test", now_ms=NOW_MS).window_bars)

    folded = fold_minute_bars(bars, DAY_BAR_MINUTES)

    frame = pd.DataFrame(
        {
            "t": [b.start_ms for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        }
    )
    frame["date"] = pd.to_datetime(frame["t"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.date
    grouped = frame.groupby("date", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
    )
    assert [b.start_ms for b in folded] == [et_midnight_ms(d) for d in grouped.index]
    for column in ("open", "high", "low", "close"):
        np.testing.assert_allclose([float(getattr(b, column)) for b in folded], grouped[column].to_numpy(), atol=1e-12, rtol=0)
    np.testing.assert_allclose([float(b.volume) for b in folded], grouped["volume"].to_numpy(), atol=ATOL, rtol=RTOL)
    assert len(folded) == len(window), "one bar per session, none for the holiday"


# ---------------------------------------------------------------------------
# The window rule
# ---------------------------------------------------------------------------


def at_et(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).timestamp() * 1000)


@pytest.mark.parametrize(
    ("now", "policy", "expected"),
    [
        # A Monday: its regular tape is final at 16:15 ET, its extended tape at 20:15.
        (at_et(date(2024, 3, 11), 16, 14), "regular", date(2024, 3, 8)),
        (at_et(date(2024, 3, 11), 16, 15), "regular", date(2024, 3, 11)),
        (at_et(date(2024, 3, 11), 20, 14), "extended", date(2024, 3, 8)),
        (at_et(date(2024, 3, 11), 20, 15), "extended", date(2024, 3, 11)),
        # A weekend and a holiday weekend look back to the last session.
        (at_et(date(2024, 3, 10), 12), "regular", date(2024, 3, 8)),
        (at_et(date(2024, 3, 31), 12), "regular", date(2024, 3, 28)),
        # The early close: 13:00 ET plus the vendor delay.
        (at_et(date(2024, 7, 3), 13, 14), "regular", date(2024, 7, 2)),
        (at_et(date(2024, 7, 3), 13, 15), "regular", date(2024, 7, 3)),
        (at_et(date(2024, 7, 3), 17, 15), "extended", date(2024, 7, 3)),
    ],
)
def test_the_window_ends_on_the_last_session_whose_tape_the_vendor_has_finished_delivering(now, policy, expected):
    assert last_final_session(now, policy) == expected


@pytest.mark.parametrize(
    ("bar_size", "end", "start"),
    [
        ("1d", date(2024, 12, 31), date(2023, 12, 31)),
        ("1d", date(2024, 2, 29), date(2023, 2, 28)),  # the leap day lands on the 28th of a common year
        ("15m", date(2024, 12, 31), date(2024, 11, 30)),
        ("15m", date(2024, 3, 29), date(2024, 2, 29)),
        ("15m", date(2024, 3, 31), date(2024, 2, 29)),  # a month back clamps to the month's last day
    ],
)
def test_the_default_window_reaches_back_one_calendar_year_for_daily_bars_and_one_month_for_intraday(bar_size, end, start, monkeypatch):
    monkeypatch.setattr("app.services.realized_vs_iv_study_service.last_final_session", lambda *_: end)

    assert resolve_window(bar_size, "regular", NOW_MS) == (start, end)


def test_each_bar_size_defaults_to_the_session_it_has_always_read_and_the_identity_names_it():
    assert DEFAULT_SESSION_POLICY == {"15m": "extended", "1d": "regular"}
    for bar_size, minutes in BAR_MINUTES.items():
        identity = realized_vs_iv_identity(RealizedVsIvParams("SPY", bar_size, window=window_of(bar_size)), now_ms=NOW_MS)
        assert identity.inputs.session_policy == DEFAULT_SESSION_POLICY[bar_size]
        assert identity.bar_minutes == minutes
        assert identity.as_dict()["session_policy"] == DEFAULT_SESSION_POLICY[bar_size]
        assert identity.as_dict()["aggregation"] == "minute_fold_et_wall_clock_no_fill"
        assert identity.as_dict()["warmup_sessions"] == 0, "the study fetches no warmup"


# ---------------------------------------------------------------------------
# Identity: what changes the cache key and what must not
# ---------------------------------------------------------------------------


def digest(p: RealizedVsIvParams) -> str:
    return realized_vs_iv_identity(p, now_ms=NOW_MS).digest()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"session_policy": "extended"}, id="session_policy"),
        pytest.param({"tenor_days": 60}, id="tenor"),
        pytest.param({"windows": (5, 10)}, id="windows"),
        pytest.param({"estimators": ("yz", "ctc")}, id="estimators"),
        pytest.param({"confidence_floor": 0.2}, id="confidence_floor"),
        pytest.param({"price_adjustment_mode": "raw"}, id="adjustment"),
        pytest.param({"window": (DAILY_WINDOW[0], DAILY_WINDOW[-2])}, id="window_end"),
        pytest.param({"iv_series": [{"ts": et_midnight_ms(DAILY_WINDOW[3]), "iv30": 0.2}]}, id="iv"),
    ],
)
def test_a_changed_policy_or_scientific_input_is_a_new_identity(change):
    assert digest(params("1d", **change)) != digest(params("1d"))


def test_the_hf_session_is_part_of_a_fifteen_minute_identity_and_not_of_a_daily_one_which_has_no_hf_estimator():
    assert digest(params("15m", hf_session="RTH")) != digest(params("15m", hf_session="ETH"))
    assert digest(params("1d", hf_session="RTH")) == digest(params("1d", hf_session="ETH")), "a choice nothing reads is not an input"
    assert realized_vs_iv_identity(params("1d", hf_session="RTH"), now_ms=NOW_MS).as_dict()["hf_session"] is None


def test_the_same_request_is_the_same_identity_and_a_new_snapshot_in_the_recorder_is_not(recorder_store):
    first = digest(params("1d"))
    assert digest(params("1d")) == first
    snapshot = RecordedIvSnapshot(
        ticker="SPY", snapshot_ts_ms=at_et(DAILY_WINDOW[5], 9, 35), slot="09:35", spot=100.0, rate=0.04, dividend_yield=0.01,
        rate_source="t", dividend_source="t", iv30_vix_style=0.2, iv30_parametric=None, iv_provenance={}, raw_chain=[],
    )
    recorder_store.write(snapshot)

    assert digest(params("1d")) != first, "recorded IV is an input: a new snapshot is a new identity, never a cache hit"


@pytest.mark.parametrize("bar_size", ["5m", "1h", "1m", "30m", "1D", "1w", ""])
def test_a_bar_size_the_study_is_not_defined_for_is_refused_with_a_reason_and_no_coverage(bar_size):
    with pytest.raises(StudyInputRefusal) as raised:
        realized_vs_iv_identity(RealizedVsIvParams("SPY", bar_size, window=window_of("1d")), now_ms=NOW_MS)

    assert raised.value.code == "resolution_unsupported"
    assert "15m and 1d" in raised.value.reason
    assert raised.value.coverage is None


# ---------------------------------------------------------------------------
# IV: recorded snapshots reach the bars they fall in, and nothing else
# ---------------------------------------------------------------------------


def snapshot_at(day: date, slot: str, iv: float, *, health: float | None = None) -> RecordedIvSnapshot:
    hour, minute = int(slot[:2]), int(slot[3:])
    return RecordedIvSnapshot(
        ticker="SPY", snapshot_ts_ms=at_et(day, hour, minute) + 1_234, slot=slot, spot=100.0, rate=0.04, dividend_yield=0.01,
        rate_source="t", dividend_source="t", iv30_vix_style=iv, iv30_parametric=None,
        iv_provenance={"variance_contribution_synthetic": 0.05}, raw_chain=[], health_score=health,
    )


def test_recorder_slots_reach_the_daily_bar_of_their_session_and_the_latest_slot_of_a_day_wins(lake_vendor, recorder_store):
    day = DAILY_WINDOW[10]
    recorder_store.write(snapshot_at(day, "09:35", 0.20))
    recorder_store.write(snapshot_at(day, "15:55", 0.22))
    recorder_store.write(snapshot_at(day, "16:00", 0.23))
    other = DAILY_WINDOW[12]
    recorder_store.write(snapshot_at(other, "12:30", 0.31))

    ours = result_of(params("1d"))

    held = {et_date_at_ms(t): v for t, v in zip(ours["ts"], ours["iv30"], strict=True) if v is not None}
    assert held == {day: 0.23, other: 0.31}, "the 16:00 snapshot, not the 09:35 one, represents the day"
    assert ours["iv_source"] == "recorder"
    assert ours["coverage"]["iv_alignment"] == {
        "rule": "snapshot_in_bar_latest", "snapshots_read": 4, "bars_with_iv": 2, "superseded": 2, "outside_bars": 0,
    }
    assert ours["coverage"]["iv_first_ts"] == et_midnight_ms(day) and ours["coverage"]["iv_last_ts"] == et_midnight_ms(other)


def test_recorder_slots_reach_the_fifteen_minute_bar_that_contains_them_and_the_close_slot_needs_an_extended_bar(lake_vendor, recorder_store):
    day = INTRADAY_WINDOW[4]
    for slot, iv in (("09:35", 0.20), ("12:30", 0.21), ("15:55", 0.22), ("16:00", 0.23)):
        recorder_store.write(snapshot_at(day, slot, iv))

    extended = result_of(params("15m", session_policy="extended"))
    regular = result_of(params("15m", session_policy="regular"))

    def held(result: dict) -> dict[str, float]:
        return {
            datetime.fromtimestamp(t / 1000, ET).strftime("%H:%M"): v
            for t, v in zip(result["ts"], result["iv30"], strict=True)
            if v is not None
        }

    assert held(extended) == {"09:30": 0.20, "12:30": 0.21, "15:45": 0.22, "16:00": 0.23}
    assert held(regular) == {"09:30": 0.20, "12:30": 0.21, "15:45": 0.22}, "no regular bar holds 16:00:01"
    assert regular["coverage"]["iv_alignment"]["outside_bars"] == 1 and regular["coverage"]["iv_alignment"]["bars_with_iv"] == 3


def test_iv_stamped_at_bar_starts_is_unchanged_by_the_alignment_so_input_that_already_lined_up_still_does(lake_vendor):
    ts = [et_midnight_ms(day) for day in DAILY_WINDOW[::4]]
    supplied = [{"ts": t, "iv30": 0.2 + i / 100, "health_score": 0.9} for i, t in enumerate(ts)]

    ours = result_of(params("1d", iv_series=supplied))

    assert {t: v for t, v in zip(ours["ts"], ours["iv30"], strict=True) if v is not None} == {p["ts"]: p["iv30"] for p in supplied}
    assert ours["iv_source"] == "caller_supplied"
    assert ours["coverage"]["iv_alignment"]["superseded"] == 0 and ours["coverage"]["iv_alignment"]["outside_bars"] == 0


def test_a_bar_with_no_snapshot_has_no_iv_nothing_is_carried_forward_into_the_iv_the_page_shows(lake_vendor, recorder_store):
    recorder_store.write(snapshot_at(DAILY_WINDOW[10], "16:00", 0.25))

    ours = result_of(params("1d"))

    finite = [i for i, v in enumerate(ours["iv30"]) if v is not None]
    assert finite == [10], "one snapshot, one bar: the bars after it are not given its IV"
    assert [i for i, v in enumerate(ours["iv30_trd252"]) if v is not None] == [10]
    assert [i for i, v in enumerate(ours["vrp_forward"]) if v is not None] == [10], "and the premium exists only where IV does"


def test_snapshots_on_a_weekend_or_outside_the_window_fall_inside_no_bar_and_are_counted(lake_vendor, recorder_store):
    saturday = date(2024, 1, 20)  # a Saturday inside the window
    recorder_store.write(snapshot_at(saturday, "12:30", 0.4))
    recorder_store.write(snapshot_at(DAILY_WINDOW[7], "09:35", 0.2))

    ours = result_of(params("1d"))

    assert ours["coverage"]["iv_alignment"] == {
        "rule": "snapshot_in_bar_latest", "snapshots_read": 2, "bars_with_iv": 1, "superseded": 0, "outside_bars": 1,
    }
    assert 0.4 not in [v for v in ours["iv30"] if v is not None]


def test_a_recorder_with_snapshots_none_of_which_land_on_a_bar_reports_it_rather_than_pretending_there_is_no_recorder(lake_vendor, recorder_store):
    recorder_store.write(snapshot_at(date(2024, 1, 20), "12:30", 0.4))  # a Saturday

    ours = result_of(params("1d"))

    assert ours["iv_source"] == "recorder"
    assert all(v is None for v in ours["iv30"]) and ours["coverage"]["iv_first_ts"] is None
    assert ours["coverage"]["iv_alignment"]["snapshots_read"] == 1 and ours["coverage"]["iv_alignment"]["bars_with_iv"] == 0


def test_the_series_never_reads_a_live_iv_a_current_marker_has_no_way_into_past_dates(lake_vendor, recorder_store):
    """The live IV30 marker is a separate browser call to ``/api/edge/iv30/*``. The study neither calls it nor accepts it."""
    with respx.mock(assert_all_called=False) as vendor_only:
        ours = result_of(params("1d"))
        assert not [call for call in vendor_only.calls if "snapshot" in str(call.request.url) or "iv30" in str(call.request.url)]

    assert all(v is None for v in ours["iv30"] + ours["iv30_trd252"] + ours["vrp_forward"]), "no recorded IV, so no IV on any date"
    assert ours["iv_source"] == "absent"


def test_the_tenor_converts_each_ivs_basis_with_the_nyse_session_count_of_its_own_bar(lake_vendor):
    supplied = [{"ts": et_midnight_ms(day), "iv30": 0.21} for day in DAILY_WINDOW[::10]]

    ours = result_of(params("1d", iv_series=supplied, tenor_days=60))

    for t, iv, trd in zip(ours["ts"], ours["iv30"], ours["iv30_trd252"], strict=True):
        if iv is not None:
            assert trd == pytest.approx(convert_iv_act365_to_trading252(iv, t, 60), abs=ATOL, rel=0)
    assert ours["tenor_days"] == 60 and ours["bar_size"] == "1d"
    assert ours["hf_session"] is None and ours["coverage"]["session"] is None, "a daily run has no HF estimator, so no session"


def test_the_result_carries_the_bars_the_chart_draws_aligned_to_the_series(lake_vendor):
    run = study_run(params("1d"))
    ours = compute_study(run)

    assert set(ours["bars"]) == {"open", "high", "low", "close", "volume"}
    assert all(len(column) == len(ours["ts"]) for column in ours["bars"].values())
    assert ours["bars"]["close"] == [r["close"] for r in run.rows]
    assert all(math.isfinite(v) for v in ours["bars"]["close"] + ours["bars"]["volume"])
