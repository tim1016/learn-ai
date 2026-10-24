"""Signal study inputs: the old provider path against the admitted lake path, on one pinned vendor payload (#3522; R14, T08, D02).

Slice 32 of the UX redesign moves the study's bars from ``PolygonClientService.fetch_aggregates`` to the lake. This
module is the receipt for what that move changes, case by case. Both paths read the *same* pinned vendor payload:

* the **provider path** is the real ``fetch_aggregates`` (the code the study used), fed ``Agg`` objects from the
  Polygon SDK's own parser, whose rows go straight into the signal engine as the old job did;
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
  the reader — and the study's fold, whose rows go into the same engine.

Each case records changed timestamps, prices, precision, counts and derived outputs (best threshold and cost, the best
net Sharpe, how often the signal is active, the effective sample, the walk-forward OOS Sharpe, the graduation grade), and
names the cause in the reconcile taxonomy (``timestamp``, ``warmup``, ``precision``, ``off-by-one``, ``data-quality``).
A case that moves nothing says so by asserting zero. Unexplained drift fails here; an *intentional* divergence is the
owner's decision (D02) and is listed in the PR.

Not provable offline: that Polygon's own N-minute aggregates equal a fold of its minute aggregates. Cases that fold use
an independent reference for the vendor side (``vendor_fold``); the equivalence with the live API is an assumption of
the lake path (the same trades build both) and is unverified against live data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import respx

from app.research.signal.engine import SignalEngineReport
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs
from app.services.signal_study_service import SignalStudyParams, serialize_signal_report, signal_study_identity
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, scaled, vendor_history
from tests.research.test_feature_study import ATOL, RTOL, assert_same
from tests.research.test_feature_study_lake_vs_provider import DENSE, provider_rows
from tests.research.test_signal_study import LONG, NOW_MS, SHORT, engine_report, lake_rows, study

ET = ZoneInfo("America/New_York")


def tape_for(window: list[date], **options: Any) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=11, start_price=150.0, **{**DENSE, **options})


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
    provider_out: dict[str, float] = field(default_factory=dict)
    lake_out: dict[str, float] = field(default_factory=dict)
    grades: tuple[str | None, str | None] = (None, None)
    errors: tuple[str | None, str | None] = (None, None)

    @property
    def timestamps_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def max_output_diff(self) -> float:
        """The largest absolute difference between the two paths' headline numbers (NaN against NaN is no difference)."""
        diffs = [
            abs(self.provider_out[k] - self.lake_out[k])
            for k in self.provider_out
            if not (np.isnan(self.provider_out[k]) and np.isnan(self.lake_out[k]))
        ]
        return float(max(diffs)) if diffs else 0.0

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count or a magnitude decides."""
        found: list[str] = []
        if self.timestamps_changed:
            found.append("data-quality")  # bars present on one side only: the input itself differs
        if self.prices_changed and self.max_price_diff <= 5e-5 + 1e-9:
            found.append("precision")  # at most half of the lake's 1/10,000 grid
        elif self.prices_changed:
            found.append("data-quality")
        if self.volume_changed and self.max_volume_diff <= 1e-9:
            found.append("precision")  # a decimal-exact sum against a float sum: the last bits of the same total
        elif self.volume_changed:
            found.append("data-quality")
        return list(dict.fromkeys(found))


def compare(provider: list[dict], lake: list[dict], params: SignalStudyParams) -> Comparison:
    by_provider = {r["timestamp"]: r for r in provider}
    by_lake = {r["timestamp"]: r for r in lake}
    result = Comparison(provider_bars=len(provider), lake_bars=len(lake))
    result.only_provider = len(by_provider.keys() - by_lake.keys())
    result.only_lake = len(by_lake.keys() - by_provider.keys())
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


def test_prices_finer_than_the_lake_grid_move_by_at_most_half_a_ten_thousandth(world):
    """Cause: ``precision``. The lake rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``); the provider path does not.

    The bound below is measured on this tape, not assumed: it is the size of the divergence the owner is shown. No
    tolerance is loosened to hide it — the strict-float comparison above still holds for on-grid prices.
    """
    tape = tape_for(LONG, decimals=5)
    mock_vendor("SPY", tape, adjusted=tape)
    params = study(LONG)

    provider, lake = paths(tape, LONG, params)
    comparison = compare(provider, lake, params)

    assert comparison.errors == (None, None)
    assert comparison.timestamps_changed == 0
    assert comparison.prices_changed > 0
    assert comparison.max_price_diff <= 5e-5 + 1e-9
    assert comparison.causes == ["precision"]
    for count in ("bars_used", "oos_windows", "best_threshold", "best_cost_bps"):
        assert comparison.provider_out[count] == comparison.lake_out[count], f"{count} does not move with the rounding"
    assert comparison.grades[0] == comparison.grades[1]
    # Measured on this tape: the largest headline difference is the effective sample size (about 8e-4 of 300); the
    # best net Sharpe moves by about 5e-5.
    assert comparison.max_output_diff < 2e-3, f"measured headline drift {comparison.max_output_diff:.4g}"


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
    """Cause: ``data-quality`` (session scope). The study ran on 04:00-20:00 ET; ``regular`` removes the rest. D02.

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
