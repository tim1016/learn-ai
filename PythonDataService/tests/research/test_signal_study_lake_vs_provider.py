"""Signal study inputs: the old provider path against the admitted lake path, on one pinned vendor payload (#3522; R14, T08).

Slice 32 of the UX redesign moves the study's bars from ``PolygonClientService.fetch_aggregates`` to the lake. This
module is the receipt for what that move changes, case by case. Both paths read the *same* pinned vendor payload:

* the **provider path** is the real ``fetch_aggregates`` (the code the study used), fed ``Agg`` objects from the
  Polygon SDK's own parser, whose rows go straight into the signal engine as the old job did;
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
  the reader — and the study's fold, whose rows go into the same engine.

Each case records changed timestamps, prices, counts and derived outputs (best threshold and cost, the best net Sharpe,
how often the signal is active, the effective sample, the walk-forward OOS Sharpe, the graduation grade), and names the
cause in the reconcile taxonomy (``timestamp``, ``warmup``, ``precision``, ``off-by-one``, ``data-quality``). A case
that moves nothing says so by asserting zero. Unexplained drift fails here; an *intentional* divergence is named in its
case's docstring, and the owner accepted each of them on #3522.

The lake's 1/10,000 price grid is ``data-quality``, never ``precision``: it changes the input data, and ``precision``
is floating-point accumulation. A tolerance may loosen only for the latter, so each output this divergence moves is
pinned at its *measured* magnitude (``test_prices_finer_than_the_lake_grid...``) at three price levels, because how far a
Sharpe, the active share or the chosen threshold move depends on the price the signal is computed at. The pin is
two-sided, at the measured value to full precision, and its tolerance is the floating-point accumulation error of the
statistic (``accumulation_atol``), the only thing that can differ between two platforms running the same arithmetic.
That tape is synthetic and finer than the grid on purpose: it measures the size of the effect. The vendor's own prices are
not finer, which the last section shows on a real split-adjusted tape.

Cases that fold use an independent reference for the vendor side (``vendor_fold``). The last section holds the vendor's
real answers (``tests/fixtures/golden/lake-studies``, recorded 2026-10-24 with the owner's approval) and is the one place
the fold meets the vendor itself:

* Polygon's own 15-minute SPY aggregates for 2025-01-13..17 equal ``fold_minute_bars`` of the repo's SPY minute capture,
  bar for bar and field for field, for the whole day and for regular hours. That includes 135 buckets with missing minutes,
  where a minute that traded without a minute bar of its own would have shown as a volume difference. It does not cover
  other symbols, half-days, a week with a clock change, split-adjusted aggregates or any other bar length.
* A split-adjusted WMT tape (the week before its 3:1 split) has no price finer than the lake's grid, and every Signal
  output is identical on the two paths. It does not cover other split ratios, other tickers or prices below $1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import respx

from app.data_lake.lean_writer import to_deci_cent
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.signal.engine import SignalEngineReport
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs
from app.services.signal_study_service import SignalStudyParams, serialize_signal_report, signal_study_identity
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import ATOL, DENSE, RTOL, assert_same, provider_rows
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, scaled, vendor_history
from tests._helpers.signal_study import LONG, NOW_MS, SHORT, engine_report, lake_rows, study, ungated_rows
from tests._helpers.vendor_recordings import (
    SPY_15_MINUTE,
    WMT_ADJUSTED,
    by_session,
    recorded_bars,
    spy_minute_capture,
)

ET = ZoneInfo("America/New_York")


def tape_for(window: list[date], *, start_price: float = 150.0, **options: Any) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=11, start_price=start_price, **{**DENSE, **options})


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    catalog = install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    with respx.mock:
        mock_launcher()
        yield catalog


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def outputs(report: SignalEngineReport) -> dict[str, float]:
    """The headline numbers a reader takes from a signal report; NaN where the report has none."""
    best = max(report.backtest_grid, key=lambda r: r.net_sharpe, default=None)
    walk = report.walk_forward
    return {
        "bars_used": float(report.bars_used),
        "best_threshold": report.best_threshold,
        "best_cost_bps": report.best_cost_bps,
        "best_net_sharpe": best.net_sharpe if best else np.nan,
        "pct_time_active": report.signal_diagnostics.pct_time_active if report.signal_diagnostics else np.nan,
        "effective_n": report.effective_sample.effective_n if report.effective_sample else np.nan,
        "oos_windows": float(len(walk.windows)) if walk else np.nan,
        "mean_oos_sharpe": walk.mean_oos_sharpe if walk and walk.windows else np.nan,
    }


def winner_margin(report: SignalEngineReport) -> float:
    """How far the best cell's net Sharpe sits above the runner-up's. The sweep's choice can flip on float noise only if this is near zero."""
    ranked = sorted((r.net_sharpe for r in report.backtest_grid), reverse=True)
    return ranked[0] - ranked[1] if len(ranked) > 1 else float("inf")


#: The lake writer rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``): a price moves by at most half of it.
HALF_GRID = 5e-5
#: Shares of volume: two paths that trade the same minutes agree on the total to float noise.
VOLUME_ATOL = 1e-6
EPS = float(np.finfo(np.float64).eps)


def accumulation_atol(n_terms: int, scale: float) -> float:
    """The floating-point accumulation error of a statistic that two paths each compute once; class ``precision``.

    A sum or recursion over ``n_terms`` terms carries a relative error of at most about ``n_terms * eps`` (recursive
    summation's bound, with unit roundoff ``eps / 2`` doubled), plus a few single roundings (a square root, a division)
    that the ``+ 8`` covers. The movement pinned below is the difference of two such values, hence the ``2 *``, and the
    error is relative to the statistic's own size (``scale``), not to the movement. Measured here by re-rounding the same
    tape (every price times 16 factors that are not powers of two, both paths alike, so no mathematical result changes):
    the pinned movements stayed within 7e-13 absolute, at least 80 times below this bound for every output that moved.
    """
    return 2 * (n_terms + 8) * EPS * scale


@dataclass
class Comparison:
    provider_bars: int
    lake_bars: int
    only_provider: int = 0
    only_lake: int = 0
    prices_changed: int = 0
    max_price_diff: float = 0.0
    volume_changed: int = 0
    max_volume_diff: float = 0.0
    volume_totals: tuple[float, float] = (0.0, 0.0)
    provider_out: dict[str, float] = field(default_factory=dict)
    lake_out: dict[str, float] = field(default_factory=dict)
    grades: tuple[str | None, str | None] = (None, None)
    errors: tuple[str | None, str | None] = (None, None)
    #: Per path, how far the best (threshold, cost) cell's net Sharpe sits above the runner-up's.
    winner_margin: tuple[float, float] = (float("inf"), float("inf"))

    @property
    def timestamps_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def output_diffs(self) -> dict[str, float]:
        """Each headline number's absolute difference between the two paths (NaN against NaN is no difference)."""
        return {
            name: 0.0 if np.isnan(self.provider_out[name]) and np.isnan(self.lake_out[name]) else abs(self.provider_out[name] - self.lake_out[name])
            for name in self.provider_out
        }

    @property
    def output_scales(self) -> dict[str, float]:
        """Each headline number's size on the two paths: what its float accumulation error is relative to."""
        return {
            name: float(np.nanmax(np.abs([self.provider_out[name], self.lake_out[name]]), initial=0.0)) for name in self.provider_out
        }

    @property
    def max_output_diff(self) -> float:
        return max(self.output_diffs.values(), default=0.0)

    @property
    def regrouped_minutes(self) -> bool:
        """Bars sit on both sides only and the same volume trades in both: the same minutes in other buckets.

        That is a moved bar grid, not a different tape.
        """
        return self.only_provider > 0 and self.only_lake > 0 and bool(
            np.isclose(*self.volume_totals, atol=VOLUME_ATOL, rtol=0)
        )

    @property
    def quantization_only(self) -> bool:
        """Every price that moved moved by at most half of the lake's 1/10,000 grid: input quantization, not a new tape."""
        return self.prices_changed > 0 and self.max_price_diff <= HALF_GRID + 1e-9

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count or a magnitude decides.

        A bar on one side only is ``timestamp`` when the other side holds the same minutes in different buckets (the
        grid moved) and ``data-quality`` when the minutes themselves differ. A price that differs is ``data-quality``:
        the lake's 1/10,000 grid changes the input data, and ``precision`` is floating-point accumulation, which no
        price goes through. A volume that differs is ``precision`` only when the sums agree to float noise (a decimal-
        exact fold against a float sum of the same minutes).
        """
        found: list[str] = []
        if self.timestamps_changed:
            found.append("timestamp" if self.regrouped_minutes else "data-quality")
        if self.prices_changed:
            found.append("data-quality")
        if self.volume_changed:
            found.append("precision" if self.max_volume_diff <= 1e-9 else "data-quality")
        return list(dict.fromkeys(found))


def compare(provider: list[dict], lake: list[dict], params: SignalStudyParams) -> Comparison:
    by_provider = {r["timestamp"]: r for r in provider}
    by_lake = {r["timestamp"]: r for r in lake}
    result = Comparison(provider_bars=len(provider), lake_bars=len(lake))
    result.only_provider = len(by_provider.keys() - by_lake.keys())
    result.only_lake = len(by_lake.keys() - by_provider.keys())
    result.volume_totals = (sum(r["volume"] for r in provider), sum(r["volume"] for r in lake))
    for ts in sorted(by_provider.keys() & by_lake.keys()):
        p, q = by_provider[ts], by_lake[ts]
        price_diff = max(abs(p[c] - q[c]) for c in ("open", "high", "low", "close"))
        if price_diff > 0:
            result.prices_changed += 1
            result.max_price_diff = max(result.max_price_diff, price_diff)
        if p["volume"] != q["volume"]:
            result.volume_changed += 1
            result.max_volume_diff = max(result.max_volume_diff, abs(p["volume"] - q["volume"]))
    reports = engine_report(provider, params), engine_report(lake, params)
    result.errors = (reports[0].error, reports[1].error)
    result.provider_out, result.lake_out = outputs(reports[0]), outputs(reports[1])
    result.winner_margin = (winner_margin(reports[0]), winner_margin(reports[1]))
    result.grades = tuple(r.graduation.overall_grade if r.graduation else None for r in reports)  # type: ignore[assignment]
    return result


def paths(tape: dict[date, list[VendorBar]], window: list[date], params: SignalStudyParams, **kwargs: Any):
    return provider_rows(tape, window, bar_minutes=params.multiplier, **kwargs), lake_rows(params)[0]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_prices_on_the_grid_across_a_dst_change_early_closes_and_gaps_move_nothing(world):
    """Four months through the walk-forward folds: DST ends Nov 3, early closes Nov 29 and Dec 24, a 92%-dense tape."""
    tape = tape_for(LONG)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(LONG)

    provider, lake = paths(tape, LONG, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert (comparison.only_provider, comparison.only_lake) == (0, 0), "same bars, timestamp for timestamp"
    assert (comparison.prices_changed, comparison.volume_changed) == (0, 0)
    assert comparison.causes == []
    assert comparison.provider_out["oos_windows"] >= 1, "the comparison reaches the walk-forward"
    assert comparison.max_output_diff <= ATOL, "every headline number is the same at the strict-float default"
    assert comparison.grades[0] == comparison.grades[1]
    assert_same(
        serialize_signal_report(engine_report(provider, params)), serialize_signal_report(engine_report(lake, params))
    )


@pytest.mark.parametrize("bar_minutes", [1, 5, 15])
def test_folded_bars_give_the_same_bars_and_the_same_signal_as_the_vendor_side_aggregate(world, bar_minutes):
    tape = tape_for(SHORT)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=bar_minutes)

    provider, lake = paths(tape, SHORT, params)
    comparison = compare(provider, lake, params)

    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert comparison.errors == (None, None)
    assert comparison.max_output_diff <= ATOL


def test_a_bar_length_off_the_hour_regroups_the_same_minutes_into_other_buckets_which_is_why_the_study_refuses_it(world):
    """Cause: ``timestamp`` (bar alignment). The provider's grid and the lake fold's agree only for a length that divides the hour.

    The vendor-side stand-in floors UTC milliseconds; the fold floors the New York wall clock counted from 1970. For a
    7-minute bar the two sit on different minutes on most sessions, so almost every bar is on one side only while the
    same minutes trade on both. That is a moved grid and not a different tape, and the headline numbers move with it.
    The study refuses these lengths (``test_signal_study.py``) rather than ship numbers no clock can check.
    """
    tape = tape_for(SHORT)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=7)

    comparison = compare(provider_rows(tape, SHORT, bar_minutes=7), ungated_rows(params, 7), params)

    assert comparison.errors == (None, None)
    assert comparison.causes == ["timestamp"]
    assert comparison.regrouped_minutes
    assert comparison.volume_totals[0] == pytest.approx(comparison.volume_totals[1], abs=VOLUME_ATOL, rel=0)
    assert comparison.timestamps_changed > 0.5 * (comparison.provider_bars + comparison.lake_bars) / 2
    assert comparison.prices_changed == 0 and comparison.volume_changed == 0, "a bar both sides hold is the same bar"
    assert comparison.max_output_diff > ATOL, "so the signal the study reports moves with the grid"


#: What a price finer than the lake's grid moves, measured on this pinned synthetic payload (seed 11, the four-month
#: window, 5,352 fifteen-minute bars, the page's default RSI(14) run) at three price levels: the largest absolute difference between
#: the two paths' value of each output, to full precision. Prices carry five or six decimals, finer than the grid. A
#: rounding of at most 5e-5 is a larger share of a bar-to-bar change on a $2 name than on a $150 one, so the outputs move
#: more as the price falls. ``continuous`` outputs are pinned two-sided at the measured value, within the statistic's
#: float accumulation error (``accumulation_atol``); ``discrete`` outputs (the chosen threshold and cost, counts) are
#: pinned exactly, because a tolerance on a choice means nothing: the choice is stable because the sweep's winner leads
#: its runner-up by far more than that error (asserted below; measured 0.047 at the least). Re-measure (print
#: ``output_diffs`` per case) only when the pinned tape or the lake's encoding intentionally changes; never to make a
#: failing test pass.
QUANTIZATION_MEASURED = {
    "price_150": (
        150.0, 5,
        {"best_net_sharpe": 3.549102143018956e-05, "pct_time_active": 0.0, "effective_n": 7.707874244147206e-04, "mean_oos_sharpe": 0.0},
        {"bars_used": 0.0, "best_threshold": 0.0, "best_cost_bps": 0.0, "oos_windows": 0.0},
    ),
    "price_12": (
        12.0, 5,
        {"best_net_sharpe": 0.17226736646039598, "pct_time_active": 1.868809568305041e-04, "effective_n": 5.894916677220863e-03, "mean_oos_sharpe": 0.0},
        {"bars_used": 0.0, "best_threshold": 1.5, "best_cost_bps": 0.0, "oos_windows": 0.0},
    ),
    "price_2": (
        2.0, 6,
        {"best_net_sharpe": 0.49348158966829914, "pct_time_active": 5.606428704914984e-04, "effective_n": 1.8355161329623115e-02, "mean_oos_sharpe": 0.0},
        {"bars_used": 0.0, "best_threshold": 1.5, "best_cost_bps": 0.0, "oos_windows": 0.0},
    ),
}


@pytest.mark.parametrize(
    ("start_price", "decimals", "continuous", "discrete"), QUANTIZATION_MEASURED.values(), ids=QUANTIZATION_MEASURED
)
def test_prices_finer_than_the_lake_grid_move_each_output_by_its_measured_amount_which_depends_on_the_price(
    world, start_price, decimals, continuous, discrete
):
    """Cause: ``data-quality`` (input quantization). The lake rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``);
    the provider path does not. That changes the input data, so it is not ``precision``: the movement is a fact about the
    data and is pinned at its measured value. What ``precision`` (float accumulation) adds on top of it is the tolerance.

    This measures what a tape finer than the grid would do. The tape carries five and six decimals on purpose; real prices
    are already on the grid (``test_a_real_split_adjusted_tape...``), so these movements are the size of the effect and not
    one a real run sees. On a $150 name nothing the reader acts on would move. On a $12 name the best net Sharpe would move
    by 0.17 and the sweep pick another threshold (2.0 on the provider path, 0.5 on the lake); on a $2 name the Sharpe by
    0.49. The graduation grade would not change at any level.
    """
    tape = tape_for(LONG, start_price=start_price, decimals=decimals)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(LONG, feature_name="rsi_14")

    provider, lake = paths(tape, LONG, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert comparison.timestamps_changed == 0
    assert comparison.quantization_only, "every price that moved moved by at most half a ten-thousandth"
    assert comparison.causes == ["data-quality"]
    assert comparison.grades[0] == comparison.grades[1]
    diffs, scales = comparison.output_diffs, comparison.output_scales
    n_terms = int(comparison.provider_out["bars_used"])
    for name, measured in continuous.items():
        np.testing.assert_allclose(diffs[name], measured, atol=accumulation_atol(n_terms, scales[name]), rtol=0, err_msg=name)
        assert bool(diffs[name] > 0) == (measured > 0), f"{name}: the divergence is real where it was measured"
    for name, measured in discrete.items():
        assert diffs[name] == measured, name
    # A choice is exact only if float noise cannot flip it: its runner-up is far behind on both paths.
    assert min(comparison.winner_margin) > 1_000 * accumulation_atol(n_terms, scales["best_net_sharpe"])


def test_fractional_share_volume_is_kept_exactly_by_both_paths(world):
    tape = tape_for(SHORT, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=1, feature_name="volume_zscore")

    provider, lake = paths(tape, SHORT, params)
    comparison = compare(provider, lake, params)

    assert comparison.volume_changed == 0, "six-decimal volume survives the lake's text encoding exactly"
    assert comparison.errors == (None, None)
    assert comparison.max_output_diff <= ATOL


def test_folding_fractional_volume_sums_it_exactly_where_the_vendor_side_stand_in_sums_floats(world):
    """Cause: ``precision``. The fold adds decimals and converts once; a float sum of the same minutes differs in its last bits."""
    tape = tape_for(SHORT, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=5, feature_name="volume_zscore")

    provider, lake = paths(tape, SHORT, params)
    comparison = compare(provider, lake, params)

    assert comparison.volume_changed > 0 and comparison.max_volume_diff <= 1e-9
    assert comparison.causes == ["precision"]
    assert comparison.timestamps_changed == 0 and comparison.prices_changed == 0
    assert comparison.errors == (None, None)
    assert comparison.max_output_diff <= ATOL, "and the sum's last bits move no output at the strict-float default"


def test_regular_hours_is_a_different_sample_from_what_the_study_has_always_read(world):
    """Cause: ``data-quality`` (session scope). The study ran on 04:00-20:00 ET; ``regular`` removes the rest. The page
    lets the person choose between the two (#3522).

    The numbers below are on the pinned synthetic tape, a measure of how much the policy moves the inputs and the
    outputs, not of how much it would move a live name's.
    """
    tape = tape_for(SHORT)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=5, session="regular")

    provider, lake = paths(tape, SHORT, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert comparison.only_lake == 0 and comparison.only_provider > 0
    assert comparison.lake_bars < 0.5 * comparison.provider_bars, "extended hours are most of the minutes in a day"
    assert comparison.prices_changed == 0, "the bars that remain are the vendor's own"
    assert comparison.causes == ["data-quality"]
    assert comparison.lake_out["bars_used"] < comparison.provider_out["bars_used"]
    assert comparison.max_output_diff > 0.5, "the headline numbers move with the sample"


def test_a_split_inside_the_window_matches_the_vendors_adjusted_view_and_raw_is_a_different_study(world):
    """Cause (raw versus adjusted): ``data-quality``. The adjusted lake view equals the vendor's; the raw view is another identity."""
    split_day = SHORT[5]
    raw = vendor_history(SHORT, seed=11, start_price=1500.0, **DENSE)
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split])
    params = study(SHORT, multiplier=5)

    provider = provider_rows(raw, SHORT, bar_minutes=5, adjusted=adjusted)
    adjusted_lake = lake_rows(params)[0]
    raw_lake = lake_rows(study(SHORT, multiplier=5, price_adjustment_mode="raw"))[0]  # type: ignore[arg-type]

    same = compare(provider, adjusted_lake, params)
    moved = compare(provider, raw_lake, params)

    assert same.errors == (None, None)
    assert (same.timestamps_changed, same.volume_changed) == (0, 0)
    assert same.prices_changed == 0, "the vendor's adjusted tape, bar for bar"
    assert same.max_output_diff <= ATOL
    assert moved.max_price_diff > 100, "the raw basis is ten times the adjusted one before the split"
    assert moved.causes == ["data-quality"]
    assert moved.max_output_diff > same.max_output_diff, "so adjustment mode must be part of the study's identity"
    assert signal_study_identity(params).digest() != signal_study_identity(study(SHORT, multiplier=5, price_adjustment_mode="raw")).digest()  # type: ignore[arg-type]


def test_a_forming_session_was_served_by_the_provider_and_is_refused_by_the_lake(world):
    """Cause: ``data-quality``. The provider hands back the in-progress session's partial bars; the lake holds completed sessions."""
    last = SHORT[-1]
    midday = int(datetime(last.year, last.month, last.day, 12, 0, tzinfo=ET).timestamp() * 1000)
    tape = tape_for(SHORT)
    served_so_far = {**tape, last: [b for b in tape[last] if b["t"] < midday]}
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=5)

    provider = provider_rows(served_so_far, SHORT, bar_minutes=5)
    with pytest.raises(StudyInputRefusal) as raised:
        admit_minute_inputs(signal_study_identity(params).inputs, requester="comparison", now_ms=midday)

    assert datetime.fromtimestamp(provider[-1]["timestamp"] / 1000, ET).date() == last, "the old path computed on a partial day"
    assert provider[-1]["timestamp"] < midday
    assert raised.value.code == "window_not_complete"


def test_duplicate_or_out_of_order_vendor_bars_were_tolerated_or_crashed_before_and_are_refused_now(world):
    """Cause: ``data-quality``. The provider path sorted out-of-order bars silently and failed late on a duplicate."""
    tape = tape_for(SHORT)
    bad_day = SHORT[2]
    out_of_order = {**tape, bad_day: [*tape[bad_day][:5], tape[bad_day][6], tape[bad_day][5], *tape[bad_day][7:]]}
    duplicate = {**tape, bad_day: [*tape[bad_day][:5], tape[bad_day][4], *tape[bad_day][5:]]}
    params = study(SHORT, multiplier=1)

    reordered_report = engine_report(provider_rows(out_of_order, SHORT), params)
    duplicate_report = engine_report(provider_rows(duplicate, SHORT), params)

    assert reordered_report.error is None, "before: an out-of-order bar was sorted into place without a word"
    assert duplicate_report.error is not None and "duplicate" in duplicate_report.error, "before: a duplicate failed late"
    for bad in (out_of_order, duplicate):
        respx.mock.reset()
        mock_launcher()
        mock_vendor("SPY", bad)
        with pytest.raises(StudyInputRefusal) as raised:
            admit_minute_inputs(signal_study_identity(params).inputs, requester="comparison", now_ms=NOW_MS)
        assert raised.value.code == "data_unavailable_from_provider", "now: the lake never publishes a corrupt tape"


def test_the_study_reads_no_vwap_or_transaction_count_so_dropping_them_moves_nothing(world):
    """The lake carries neither field. The study's math never reads them, so the omission cannot move an output."""
    tape = tape_for(SHORT)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(SHORT, multiplier=5)
    provider = provider_rows(tape, SHORT, bar_minutes=5)
    assert {"vwap", "transactions"} <= provider[0].keys()
    lake = lake_rows(params)[0]
    assert not {"vwap", "transactions"} & lake[0].keys()

    stripped = [{k: v for k, v in row.items() if k not in ("vwap", "transactions")} for row in provider]
    ours, theirs = engine_report(lake, params), engine_report(stripped, params)

    assert outputs(ours) == pytest.approx(outputs(theirs), abs=ATOL, rel=RTOL, nan_ok=True)


# ---------------------------------------------------------------------------
# The vendor's real answers, recorded 2026-10-24 (tests/fixtures/golden/lake-studies)
# ---------------------------------------------------------------------------

SPY_WEEK = expected_sessions(date(2025, 1, 13), date(2025, 1, 17))
#: SPY's 15-minute bars for the whole day (64 a session, 04:00-19:45 ET) and for regular hours (26 a session).
SPY_15_MINUTE_BARS = {"extended": 320, "regular": 130}
#: Buckets of the minute capture that hold fewer than their 15 minutes, and the minutes missing from them (measured).
SPY_PARTIAL_BUCKETS, SPY_MISSING_MINUTES = 135, 738

WMT_WINDOW = expected_sessions(date(2024, 2, 20), date(2024, 2, 23))
#: The 3:1 split the corporate-action feed is served with, the owner's fact; no request was made for it.
WMT_SPLIT = {"ticker": "WMT", "execution_date": "2024-02-26", "split_from": 1, "split_to": 3}
WMT_BARS, WMT_PRICES = 2226, 8904
#: Every price multiplied by this and rounded to six decimals is off the lake's grid: the positive control.
OFF_THE_GRID = 1.0000337


def vendor_15_minute_rows(session: str) -> list[dict]:
    """Polygon's own 15-minute SPY bars as study rows, for the whole day or for regular hours alone."""
    bars = recorded_bars(SPY_15_MINUTE)
    if session == "regular":
        bars = [
            bar
            for day, day_bars in by_session(bars).items()
            for bar in day_bars
            if session_open_ms_utc(day) <= bar["t"] < session_close_ms_utc(day)
        ]
    return [
        {"timestamp": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": float(b["v"])}
        for b in bars
    ]


@pytest.mark.parametrize("session", ["extended", "regular"])
def test_polygons_own_15_minute_spy_bars_equal_the_lake_fold_of_its_minute_bars_in_every_field(world, session):
    """Cause: none. The fold and the vendor's aggregate agree, on every bar and every field, on exactly equal numbers.

    The repo's SPY minute capture is served at the vendor's HTTP seam and taken through the real lake writer, committed-byte
    admission and reader; the fold is the study's own. One symbol, one winter week, 15-minute bars: the assumption that the
    vendor's N-minute bar is a fold of its minute bars is measured here and nowhere wider (the module docstring says where).
    """
    capture = spy_minute_capture()
    minutes_in_bucket: dict[int, int] = {}
    for bars in capture.values():
        for bar in bars:
            minutes_in_bucket[bar["t"] // 900_000] = minutes_in_bucket.get(bar["t"] // 900_000, 0) + 1
    short = [count for count in minutes_in_bucket.values() if count < 15]
    assert (len(short), sum(15 - count for count in short)) == (SPY_PARTIAL_BUCKETS, SPY_MISSING_MINUTES), (
        "the buckets with missing minutes are in the comparison, which is what makes it more than a count of full ones"
    )
    mock_vendor("SPY", capture)
    params = study(SPY_WEEK, multiplier=15, session=session, price_adjustment_mode="raw")

    ours = ungated_rows(params, 15)  # the study refuses 15-minute regular-hours runs; the fold itself is what is held to the vendor
    vendor = vendor_15_minute_rows(session)

    assert len(vendor) == SPY_15_MINUTE_BARS[session]
    assert ours == vendor, "bar for bar, open, high, low, close and volume, with no bar on one side only"


def wmt_tape() -> dict[date, list[VendorBar]]:
    tape = by_session(recorded_bars(WMT_ADJUSTED))
    assert list(tape) == WMT_WINDOW
    return tape


def test_no_price_of_a_real_split_adjusted_tape_is_finer_than_the_lake_grid():
    """The vendor's adjusted prices are rounded to at most four decimals before they reach us; the lake's encoder moves none."""
    prices = [Decimal(repr(bar[column])) for bars in wmt_tape().values() for bar in bars for column in ("o", "h", "l", "c")]

    assert len(prices) == WMT_PRICES
    assert [price for price in prices if to_deci_cent(price) != price * 10_000] == []
    assert min(prices) == Decimal("56.0186") and max(prices) == Decimal("60.45"), "a name priced in tens of dollars"


@pytest.mark.parametrize("feature", ["rsi_14", "momentum_5m"])
def test_a_real_split_adjusted_tape_gives_every_signal_output_unchanged_through_the_lake(world, feature):
    """Cause: none. The same bars, prices and volumes reach the engine on both paths, so no output moves.

    One-minute bars are the only length the four sessions give the engine an answer on (at five and fifteen minutes it
    refuses on both paths), so those are compared as bars in the next test.
    """
    tape = wmt_tape()
    mock_vendor("WMT", tape, adjusted=tape, splits=[WMT_SPLIT])
    params = study(WMT_WINDOW, multiplier=1, feature_name=feature, symbol="WMT")

    provider, lake = paths(tape, WMT_WINDOW, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert (comparison.provider_bars, comparison.lake_bars) == (WMT_BARS, WMT_BARS)
    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert comparison.causes == []
    assert comparison.max_output_diff <= ATOL, "every headline number is the same at the strict-float default"
    assert comparison.grades[0] == comparison.grades[1]
    assert_same(
        serialize_signal_report(engine_report(provider, params)), serialize_signal_report(engine_report(lake, params))
    )


@pytest.mark.parametrize("multiplier", [5, 15])
def test_a_real_split_adjusted_tape_folds_to_the_same_bars_on_both_paths(world, multiplier):
    tape = wmt_tape()
    mock_vendor("WMT", tape, adjusted=tape, splits=[WMT_SPLIT])
    params = study(WMT_WINDOW, multiplier=multiplier, symbol="WMT")
    columns = ("timestamp", "open", "high", "low", "close", "volume")

    provider = provider_rows(tape, WMT_WINDOW, bar_minutes=multiplier)
    lake = lake_rows(params)[0]

    assert [{column: row[column] for column in columns} for row in provider] == lake


def test_the_same_real_tape_pushed_off_the_grid_does_move_the_outputs_so_the_comparison_can_see_a_grid_effect(world):
    """Cause: ``data-quality`` (input quantization). The positive control for the two cases above.

    Measured: all 2,226 bars change by up to half a ten-thousandth, and the best net Sharpe, the effective sample and the
    active share move by up to 2e-3. Without this a comparison that cannot see a grid effect would pass for the same reason.
    """
    tape = {
        day: [{**bar, **{key: round(bar[key] * OFF_THE_GRID, 6) for key in ("o", "h", "l", "c", "vw")}} for bar in bars]
        for day, bars in wmt_tape().items()
    }
    mock_vendor("WMT", tape, adjusted=tape, splits=[WMT_SPLIT])
    params = study(WMT_WINDOW, multiplier=1, feature_name="rsi_14", symbol="WMT")

    provider, lake = paths(tape, WMT_WINDOW, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert comparison.quantization_only and comparison.causes == ["data-quality"]
    assert comparison.prices_changed == WMT_BARS
    assert comparison.max_output_diff > 1e-4
