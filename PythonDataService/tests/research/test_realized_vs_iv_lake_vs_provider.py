"""Realized vs IV inputs: the old provider path against the admitted lake path, on one pinned vendor payload (#3525; R14, T08, D02).

Slice 35 of the UX redesign moves the study's bars from the browser's ``/api/aggregates/fetch`` call (the provider, then
``DataSanitizer``) to the lake. This module is the receipt for what that move changes, case by case. Both paths read the
*same* pinned vendor payload:

* the **provider path** is the real ``PolygonClientService.fetch_aggregates`` (fed ``Agg`` objects from the Polygon SDK's
  own parser) followed by the real ``DataSanitizer.sanitize_aggregates`` that the aggregates route applied;
* the **lake path** is the real admission (vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission, the
  reader) and the study's fold.

Each case records changed timestamps, prices, volumes and derived outputs (the trailing Yang-Zhang series, the HF
realized vol and the premium), and names the cause in the reconcile taxonomy (``timestamp``, ``warmup``, ``precision``,
``off-by-one``, ``data-quality``). A case that moves nothing says so by asserting zero. Unexplained drift fails here; an
*intentional* divergence is the owner's decision (D02) and is listed in the PR.

The two that matter most are in the first half:

* The sanitizer fills NaN with a column's median and clips every price column to its own 1st to 99th percentile across
  the window, so the provider path computed every estimator on winsorized highs, lows and closes. Lake bars are not
  clipped. Cause: ``data-quality`` (outliers removed from the input), never ``precision``.
* The lake's 1/10,000 price grid. Cause: ``data-quality`` (input quantization). ``precision`` is floating-point
  accumulation, and nothing here accumulates: a lake price is the exact decimal the writer encoded, converted to the
  nearest float once. A float tolerance cannot cover it, so each output it moves is pinned at its *measured*
  magnitude, with a stated margin, at three price levels.

Not provable offline: that Polygon's own 15-minute and day aggregates equal a fold of its minute aggregates, and that
a day bar is the regular session with the first and last minute for the official open and close. The vendor side is an
independent stand-in (``tests/_helpers/realized_vs_iv.py``), and ``test_a_day_bar_whose_official_open_and_close_differ``
measures how much the second assumption could matter. All of it is unverified against live data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import respx
from polygon.rest.models import Agg

from app.engine.edge.realized_vs_iv_series import compute_realized_vs_iv_series
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs, bars_as_rows, fold_minute_bars
from app.services.polygon_client import PolygonClientService
from app.services.realized_vs_iv_study_service import RealizedVsIvParams, realized_vs_iv_identity
from app.services.sanitizer import DataSanitizer
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history
from tests._helpers.realized_vs_iv import frame_of, vendor_aggregates

NOW_MS = 1_735_689_600_000  # 2025-01-01

#: A volume that differs by less than this is the same volume summed in a different order (``precision``): the lake
#: folds exact decimals and the vendor side floats.
VOLUME_PRECISION = 1e-8

#: The lake writer rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``): a price moves by at most half of it.
HALF_GRID = 5e-5

#: 2024-03-10 is the spring-forward Sunday.
DAILY_WINDOW = expected_sessions(date(2024, 1, 2), date(2024, 4, 5))
INTRADAY_WINDOW = expected_sessions(date(2024, 2, 20), date(2024, 4, 1))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))

DENSE = {"regular_keep": 0.995, "extended_keep": 0.92}

#: The outputs a person reads off the page, by the name this module gives them.
OUTPUTS = ("yz_5", "yz_10", "yz_30", "hf_trailing", "hf_forward", "vrp_forward")


def window_of(bar_size: str) -> list[date]:
    return DAILY_WINDOW if bar_size == "1d" else INTRADAY_WINDOW


def tape_for(window: list[date], *, start_price: float = 150.0, seed: int = 11, **options: object) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=seed, start_price=start_price, **{**DENSE, **options})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


class _SdkTape:
    """The SDK's ``RESTClient.list_aggs`` over a pinned minute tape: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, raw: Mapping[date, list[VendorBar]], adjusted: Mapping[date, list[VendorBar]] | None, *, regular_only: bool) -> None:
        self.raw, self.adjusted, self.regular_only = raw, adjusted, regular_only

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        tape = self.adjusted if (adjusted and self.adjusted is not None) else self.raw
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        window = [day for day in sorted(tape) if start <= day <= end]
        bar_size = "1d" if timespan == "day" else "15m"
        for bar in vendor_aggregates(tape, window, bar_size=bar_size, regular_only=self.regular_only):
            yield Agg.from_dict(bar)


def provider_rows(
    raw: Mapping[date, list[VendorBar]],
    window: list[date],
    *,
    bar_size: str,
    sanitized: bool = True,
    adjusted: Mapping[date, list[VendorBar]] | None = None,
    end: date | None = None,
    regular_only: bool = True,
) -> list[dict]:
    """What the browser read before #3525: ``/api/aggregates/fetch`` for the window, the sanitizer included.

    ``sanitized=False`` stops after ``fetch_aggregates``, to separate what the sanitizer does from what the provider
    sends. ``end`` extends the request past the window's last session, as "today" did when it was in progress.
    """
    service = PolygonClientService()
    service.client = _SdkTape(raw, adjusted, regular_only=regular_only)  # type: ignore[assignment]
    fetched = service.fetch_aggregates(
        ticker="SPY",
        multiplier=15 if bar_size == "15m" else 1,
        timespan="minute" if bar_size == "15m" else "day",
        from_date=window[0].isoformat(),
        to_date=(end or window[-1]).isoformat(),
    )
    data = DataSanitizer.sanitize_aggregates(fetched)["data"] if sanitized else fetched
    return [{k: row[k] for k in ("open", "high", "low", "close", "volume")} | {"timestamp": row["timestamp"]} for row in data]


def lake_rows(window: list[date], *, bar_size: str, session_policy: str | None = None) -> list[dict]:
    identity = realized_vs_iv_identity(
        RealizedVsIvParams("SPY", bar_size, window=(window[0], window[-1]), session_policy=session_policy),  # type: ignore[arg-type]
        now_ms=NOW_MS,
    )
    admitted = admit_minute_inputs(identity.inputs, requester="comparison", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes))


def iv_at(rows: list[dict]) -> list[dict]:
    """IV stamped at every third bar's start: the same points on both paths, so the premium is comparable."""
    return [{"ts": r["timestamp"], "iv30": 0.18 + 0.01 * (i % 7)} for i, r in enumerate(rows) if i % 3 == 0]


def outputs(rows: list[dict], bar_size: str, iv: list[dict] | None) -> dict[str, np.ndarray]:
    series = compute_realized_vs_iv_series(
        frame_of(rows), symbol="SPY", bar_size=bar_size, tenor_days=30, hf_session="ETH", estimators=["yz"], windows=[5, 10, 30],
        iv_points=iv or None, iv_source="caller_supplied" if iv else "absent", confidence_floor=0.1,
    )

    def column(values: list[float | None]) -> np.ndarray:
        return np.array([np.nan if v is None else v for v in values], dtype=float)

    return {
        "yz_5": column(series["rv_trailing"]["yz_5"]),
        "yz_10": column(series["rv_trailing"]["yz_10"]),
        "yz_30": column(series["rv_trailing"]["yz_30"]),
        "hf_trailing": column(series["rv_hf_trailing"]),
        "hf_forward": column(series["rv_hf_forward"]),
        "vrp_forward": column(series["vrp_forward"]),
    }


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass
class Comparison:
    provider_bars: int
    lake_bars: int
    only_provider: int = 0
    only_lake: int = 0
    prices_changed: int = 0
    max_price_diff: float = 0.0
    volume_changed: int = 0
    #: The largest volume difference below ``VOLUME_PRECISION``: float summation order, not a different tape.
    volume_precision_diff: float = 0.0
    #: The largest absolute difference between the two paths' value of an output at a shared bar, both finite.
    output_max_diff: dict[str, float] = field(default_factory=dict)
    #: Shared bars where exactly one path has a value (a warmup or forward-tail region that moved).
    output_nan_moved: dict[str, int] = field(default_factory=dict)

    @property
    def timestamps_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count decides.

        Every input that differs between the paths is ``data-quality``: a bar on one side only, a price, a volume.
        A volume that differs only below ``VOLUME_PRECISION`` is ``precision`` and is not counted here.
        """
        return ["data-quality"] if self.timestamps_changed or self.prices_changed or self.volume_changed else []

    @property
    def quantization_only(self) -> bool:
        """Every price that moved moved by at most half of the lake's 1/10,000 grid: input quantization, not a new tape."""
        return self.prices_changed > 0 and self.max_price_diff <= HALF_GRID + 1e-9


def compare(provider: list[dict], lake: list[dict], bar_size: str, *, derived: bool = True) -> Comparison:
    by_provider, by_lake = {r["timestamp"]: r for r in provider}, {r["timestamp"]: r for r in lake}
    shared = sorted(by_provider.keys() & by_lake.keys())
    result = Comparison(provider_bars=len(provider), lake_bars=len(lake))
    result.only_provider = len(by_provider.keys() - by_lake.keys())
    result.only_lake = len(by_lake.keys() - by_provider.keys())
    for ts in shared:
        p, q = by_provider[ts], by_lake[ts]
        price_diff = max(abs(p[c] - q[c]) for c in ("open", "high", "low", "close"))
        if price_diff > 0:
            result.prices_changed += 1
            result.max_price_diff = max(result.max_price_diff, price_diff)
        volume_diff = abs(p["volume"] - q["volume"])
        if volume_diff > VOLUME_PRECISION:
            result.volume_changed += 1
        else:
            result.volume_precision_diff = max(result.volume_precision_diff, volume_diff)
    if not derived:
        return result
    iv = iv_at(lake)  # stamped at lake bar starts; every one of them is also a provider bar start when no bar is lost
    a, b = outputs(provider, bar_size, [point for point in iv if point["ts"] in by_provider]), outputs(lake, bar_size, iv)
    index_provider = {ts: i for i, ts in enumerate(sorted(by_provider))}
    index_lake = {ts: i for i, ts in enumerate(sorted(by_lake))}
    for name in OUTPUTS:
        diffs, moved = [], 0
        for ts in shared:
            x, y = a[name][index_provider[ts]], b[name][index_lake[ts]]
            if np.isnan(x) != np.isnan(y):
                moved += 1
            elif not np.isnan(x):
                diffs.append(abs(x - y))
        result.output_max_diff[name] = float(max(diffs)) if diffs else 0.0
        result.output_nan_moved[name] = moved
    return result


@dataclass
class World:
    """The in-memory catalog and the temporary lake the lake path runs against."""

    catalog: FakeCatalog
    lake: Path


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    with respx.mock:
        mock_launcher()
        yield World(catalog, lake)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_the_lake_adds_nothing_to_the_bars_the_provider_sends_across_a_dst_change_and_with_gaps(world, bar_size):
    window = window_of(bar_size)
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, window, bar_size=bar_size, sanitized=False), lake_rows(window, bar_size=bar_size), bar_size)

    assert (comparison.only_provider, comparison.only_lake) == (0, 0), "same bars, timestamp for timestamp"
    assert (comparison.prices_changed, comparison.volume_changed) == (0, 0)
    assert comparison.causes == []
    assert max(comparison.output_max_diff.values()) <= 1e-9, "every output is the same at the strict-float default"
    assert set(comparison.output_nan_moved.values()) == {0}, "and so is every warmup and forward-tail region"
    assert comparison.lake_bars < len(window) * (64 if bar_size == "15m" else 1) + 1, "gaps are in both; nothing is filled"


#: What the sanitizer moves, measured on this pinned payload (seed 11, 27 sessions of 15-minute bars and 66 daily bars,
#: both ``tape_for`` defaults) as the largest absolute difference between the two paths' value of an output at a shared
#: bar. Re-measure (print ``compare(...)``) only when the pinned tape or the sanitizer intentionally changes; never to
#: make a failing test pass.
SANITIZER_MEASURED = {
    "15m": {
        "bars_with_a_price_changed": 58, "bars_with_a_volume_changed": 38, "max_price_diff": 0.6055,
        "outputs": {"yz_5": 3.810e-2, "yz_10": 1.966e-2, "yz_30": 1.026e-2, "hf_trailing": 2.066e-3, "hf_forward": 2.066e-3, "vrp_forward": 1.169e-3},
    },
    "1d": {
        "bars_with_a_price_changed": 3, "bars_with_a_volume_changed": 2, "max_price_diff": 0.832,
        "outputs": {"yz_5": 2.075e-2, "yz_10": 1.013e-2, "yz_30": 4.450e-3, "hf_trailing": 4.424e-3, "hf_forward": 4.424e-3, "vrp_forward": 2.193e-3},
    },
}
#: The headroom over a measured movement. The tape is seeded and the arithmetic deterministic, so this only absorbs
#: platform differences in float summation order inside pandas and numpy; a change in the sanitizer, the lake's
#: encoding or an estimator moves these by far more.
MEASURED_MARGIN = 1.5


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_the_sanitizer_clips_each_column_to_its_percentile_band_so_the_provider_path_computed_on_winsorized_bars(world, bar_size):
    """Cause: ``data-quality`` (outliers removed from the input). Lake bars are not clipped.

    What it clips is each column's *level*, not its volatility: the bars rewritten are the highest- and lowest-priced of
    the window (a trending name loses the ends of its trend), whatever their ranges. That is why the cause is proved
    here by recomputing the clip independently, then the effect on each output is pinned at its measured movement.
    """
    window = window_of(bar_size)
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)
    raw = provider_rows(tape, window, bar_size=bar_size, sanitized=False)
    clipped = provider_rows(tape, window, bar_size=bar_size)
    lake = lake_rows(window, bar_size=bar_size)
    measured = SANITIZER_MEASURED[bar_size]

    columns = ["open", "high", "low", "close", "volume"]
    frame = pd.DataFrame(raw)[columns]
    expected = frame.clip(lower=frame.quantile(0.01), upper=frame.quantile(0.99), axis=1)
    comparison = compare(clipped, lake, bar_size)

    for column in columns:
        np.testing.assert_allclose([r[column] for r in clipped], expected[column].to_numpy(), atol=1e-12, rtol=0, err_msg=column)
    assert compare(raw, lake, bar_size, derived=False).causes == [], "the lake holds exactly the bars the provider sent"
    assert comparison.timestamps_changed == 0 and comparison.causes == ["data-quality"]
    assert (comparison.prices_changed, comparison.volume_changed) == (measured["bars_with_a_price_changed"], measured["bars_with_a_volume_changed"])
    assert not comparison.quantization_only, "this is far larger than the lake's grid"
    assert comparison.max_price_diff == pytest.approx(measured["max_price_diff"], abs=5e-4, rel=0)
    changed_closes = sorted(r["close"] for r, c in zip(raw, clipped, strict=True) if r["close"] != c["close"])
    ranked = sorted(r["close"] for r in raw)
    tail = int(np.ceil(0.01 * len(raw))) + 1
    assert all(close in ranked[:tail] or close in ranked[-tail:] for close in changed_closes), (
        "the rewritten bars are the window's price extremes: the lowest and highest 1% of closes"
    )
    assert set(comparison.output_nan_moved.values()) == {0}, "no warmup region moves"
    for name, movement in measured["outputs"].items():
        # atol is the measured movement with its margin; rtol is 0 because the quantity compared is itself a difference.
        assert 0 < comparison.output_max_diff[name] <= movement * MEASURED_MARGIN, name


#: What the lake's price grid moves, measured on this pinned payload at three price levels, as the largest absolute
#: difference between the two paths' value of an output at a shared bar (unsanitized provider rows, to isolate the
#: grid from the clip). Prices carry five or six decimals, finer than the grid. The movement grows as the price falls: a
#: rounding of at most 5e-5 is a larger share of a bar's range on a $2 name than on a $150 one.
QUANTIZATION_MEASURED = {
    ("15m", 150.0, 5): {"yz_5": 2.676e-5, "yz_10": 1.767e-5, "yz_30": 1.231e-5, "hf_trailing": 5.478e-7, "hf_forward": 5.478e-7, "vrp_forward": 3.112e-7},
    ("15m", 12.0, 5): {"yz_5": 3.113e-4, "yz_10": 2.658e-4, "yz_30": 1.274e-4, "hf_trailing": 7.452e-6, "hf_forward": 7.452e-6, "vrp_forward": 4.234e-6},
    ("15m", 2.0, 6): {"yz_5": 1.913e-3, "yz_10": 1.475e-3, "yz_30": 6.042e-4, "hf_trailing": 1.30e-4, "hf_forward": 1.30e-4, "vrp_forward": 7.393e-5},
    ("1d", 150.0, 5): {"yz_5": 5.899e-6, "yz_10": 2.473e-6, "yz_30": 8.95e-7, "hf_trailing": 1.004e-6, "hf_forward": 1.004e-6, "vrp_forward": 4.459e-7},
    ("1d", 12.0, 5): {"yz_5": 5.885e-5, "yz_10": 4.195e-5, "yz_30": 2.04e-5, "hf_trailing": 2.175e-5, "hf_forward": 2.175e-5, "vrp_forward": 9.975e-6},
    ("1d", 2.0, 6): {"yz_5": 4.077e-4, "yz_10": 2.405e-4, "yz_30": 6.167e-5, "hf_trailing": 1.007e-4, "hf_forward": 1.007e-4, "vrp_forward": 4.444e-5},
}


@pytest.mark.parametrize(("bar_size", "start_price", "decimals"), list(QUANTIZATION_MEASURED), ids=lambda key: str(key))
def test_prices_finer_than_the_lake_grid_move_each_output_by_its_measured_amount_which_depends_on_the_price(
    world, bar_size, start_price, decimals
):
    """Cause: ``data-quality`` (input quantization). The lake rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``);
    the provider path does not. That changes the input data, so it is not ``precision`` and no float tolerance covers it.
    """
    window = window_of(bar_size)
    tape = tape_for(window, start_price=start_price, decimals=decimals)
    mock_vendor("SPY", tape, adjusted=tape)
    measured = QUANTIZATION_MEASURED[(bar_size, start_price, decimals)]

    comparison = compare(provider_rows(tape, window, bar_size=bar_size, sanitized=False), lake_rows(window, bar_size=bar_size), bar_size)

    assert comparison.timestamps_changed == 0 and comparison.volume_changed == 0
    assert comparison.quantization_only, "every price that moved moved by at most half a ten-thousandth"
    assert comparison.causes == ["data-quality"]
    assert set(comparison.output_nan_moved.values()) == {0}
    for name, movement in measured.items():
        assert 0 < comparison.output_max_diff[name] <= movement * MEASURED_MARGIN, name


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_fractional_share_volume_is_kept_exactly_and_the_estimators_do_not_read_it(world, bar_size):
    window = window_of(bar_size)
    tape = tape_for(window, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, window, bar_size=bar_size, sanitized=False), lake_rows(window, bar_size=bar_size), bar_size)

    assert comparison.volume_changed == 0, "six-decimal volume survives the lake's text encoding"
    assert comparison.volume_precision_diff <= VOLUME_PRECISION, "a fold sums exact decimals, the vendor side floats: summation order only (precision)"
    assert max(comparison.output_max_diff.values()) <= 1e-9


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_an_early_close_week_matches_exactly(world, bar_size):
    tape = tape_for(HALF_DAY_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, HALF_DAY_WINDOW, bar_size=bar_size, sanitized=False), lake_rows(HALF_DAY_WINDOW, bar_size=bar_size), bar_size)

    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert max(comparison.output_max_diff.values()) <= 1e-9


#: What folding the day from extended-hours minutes moves, against the regular-session day bar the vendor stand-in
#: serves, measured on this payload (66 daily bars): the largest difference between the two paths' value of an output.
EXTENDED_DAILY_MEASURED = {"yz_5": 0.1183, "yz_10": 0.09034, "yz_30": 0.04354, "hf_trailing": 0.06005, "hf_forward": 0.06005, "vrp_forward": 0.02899}


def test_the_daily_session_is_regular_by_default_and_folding_it_from_extended_hours_is_a_different_sample(world):
    """Cause: ``data-quality`` (session scope). The census could not confirm Polygon's day bar is regular-session only;
    the default assumes it is, because a day folded from extended minutes opens at the first pre-market print and its
    high and low reach into the after-hours tape. Which one the page should use is an owner decision (D02)."""
    window = window_of("1d")
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)
    provider = provider_rows(tape, window, bar_size="1d", sanitized=False)

    default = compare(provider, lake_rows(window, bar_size="1d"), "1d")
    extended = compare(provider, lake_rows(window, bar_size="1d", session_policy="extended"), "1d")

    assert default.causes == [] and max(default.output_max_diff.values()) <= 1e-9
    assert extended.causes == ["data-quality"] and extended.timestamps_changed == 0
    assert extended.prices_changed == extended.lake_bars, "every day's open, high or low is different"
    assert extended.max_price_diff > 1.0
    for name, movement in EXTENDED_DAILY_MEASURED.items():
        assert 0 < extended.output_max_diff[name] <= movement * MEASURED_MARGIN, name


def test_a_day_bar_whose_official_open_and_close_differ_from_the_first_and_last_minute_moves_the_series_by_a_measured_sensitivity(world):
    """Cause: ``data-quality`` (the day bar's definition), as a *sensitivity*, not a finding. A real day bar opens and
    closes at the official auction prints; the lake folds the first and last regular minute. Neither side of that
    difference is on this payload, so each day's open and close are moved by 3 bps in alternating directions and the
    series is read again. Measured on this payload; it says how much the unverified assumption could matter."""
    window = window_of("1d")
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)
    nudged = []
    for i, row in enumerate(provider_rows(tape, window, bar_size="1d", sanitized=False)):
        sign = 1 if i % 2 == 0 else -1
        official_open, official_close = row["open"] * (1 + sign * 3e-4), row["close"] * (1 - sign * 3e-4)
        nudged.append({
            **row, "open": official_open, "close": official_close,
            "high": max(row["high"], official_open, official_close), "low": min(row["low"], official_open, official_close),
        })

    comparison = compare(nudged, lake_rows(window, bar_size="1d"), "1d")

    assert comparison.causes == ["data-quality"] and comparison.timestamps_changed == 0 and not comparison.quantization_only
    measured = {"yz_5": 1.797e-3, "yz_10": 1.091e-3, "yz_30": 4.356e-4, "hf_trailing": 6.347e-4, "hf_forward": 6.347e-4, "vrp_forward": 3.188e-4}
    for name, movement in measured.items():
        assert 0 < comparison.output_max_diff[name] <= movement * MEASURED_MARGIN, name


@pytest.mark.parametrize("bar_size", ["15m", "1d"])
def test_the_session_still_forming_when_the_old_window_ended_is_no_longer_in_the_sample(world, bar_size):
    """Cause: ``off-by-one`` (the window's end). The browser asked for bars through today, so a session in progress was a
    partial bar, and a trailing-window estimator can read a forward value from it. The lake holds a session only once its
    tape is final, so the window ends one session earlier."""
    window = window_of(bar_size)
    forming = expected_sessions(window[-1] + timedelta(days=1), window[-1] + timedelta(days=7))[0]
    full = tape_for([*window, forming])
    midday = session_open_ms_utc(forming) + 150 * 60_000
    partial = {**full, forming: [b for b in full[forming] if b["t"] < midday]}
    mock_vendor("SPY", full, adjusted=full)

    comparison = compare(
        provider_rows(partial, window, bar_size=bar_size, sanitized=False, end=forming), lake_rows(window, bar_size=bar_size), bar_size
    )

    expected_extra = len([b for b in vendor_aggregates(partial, [forming], bar_size=bar_size, regular_only=bar_size == "1d")])
    assert comparison.only_provider == expected_extra > 0 and comparison.only_lake == 0
    assert comparison.prices_changed == 0 and comparison.causes == ["data-quality"]
    assert max(comparison.output_max_diff.values()) <= 1e-9, "at every shared bar the two paths agree"
    # The partial session lets the provider path report a forward value one session earlier than the data supports.
    assert comparison.output_nan_moved["hf_forward"] == (1 if bar_size == "1d" else 64)
    assert comparison.output_nan_moved["vrp_forward"] == (1 if bar_size == "1d" else 21)
    assert comparison.output_nan_moved["yz_5"] == 0, "trailing outputs never look past their own bar"


def test_a_split_announced_after_capture_leaves_every_unit_free_output_unmoved_until_the_lake_refreshes_and_then_matches(world):
    """Cause: ``data-quality`` (adjustment as-of). The provider adjusts to *now*; the lake to its latest capture. Prices
    on a held pre-split day sit on the old basis, so every price differs by the split factor. Every output here is a
    ratio of prices (log returns, log ranges), so none moves; MACD-style price-unit outputs would (Feature, #3521)."""
    window = DAILY_WINDOW[:30]
    held, later = window[:15], window[15:]
    split_day = later[0]
    raw = tape_for(window, start_price=1500.0)
    mock_vendor("SPY", raw, adjusted=raw)
    lake_rows(held, bar_size="1d")  # the lake captures the first fifteen sessions before the split exists
    respx.mock.reset()
    mock_launcher()
    adjusted = {day: (scaled({day: bars}, 0.1, decimals=4)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    calls = VendorCalls()
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split], calls=calls)
    provider_after_split = provider_rows(raw, held, bar_size="1d", sanitized=False, adjusted=adjusted)

    stale = compare(provider_after_split, lake_rows(held, bar_size="1d"), "1d")
    lake_rows(later, bar_size="1d")  # any later capture of the symbol rebuilds its stale adjusted days
    refreshed = compare(provider_after_split, lake_rows(held, bar_size="1d"), "1d")

    assert stale.causes == ["data-quality"] and stale.prices_changed == stale.provider_bars
    assert stale.max_price_diff > 1000, "the held days still carry the pre-split basis"
    assert max(stale.output_max_diff.values()) <= 1e-9, "a ratio of prices does not see the basis"
    assert refreshed.prices_changed == 0 and refreshed.timestamps_changed == 0
    assert max(refreshed.output_max_diff.values()) <= 1e-9
    assert calls.count > 0


def test_a_window_that_starts_before_the_symbol_traded_was_computed_on_fewer_bars_and_is_refused_now(world):
    """Cause: ``data-quality`` (a session the vendor has no bars for). The provider path computed on the sessions it got; the
    lake path refuses a window with a session the vendor answers empty, because "the symbol had not listed yet" and
    "the vendor failed to serve it" look the same to admission (no "not applicable" outcome, ``known-gaps``)."""
    window = window_of("1d")
    tape = tape_for(window)
    listed = {day: bars for day, bars in tape.items() if day >= window[2]}
    mock_vendor("SPY", listed, adjusted=listed)

    before = provider_rows(listed, window[2:], bar_size="1d")
    with pytest.raises(StudyInputRefusal) as raised:
        lake_rows(window, bar_size="1d")

    assert len(before) == len(window) - 2, "before: the study ran on the sessions the vendor served"
    assert raised.value.code == "data_unavailable_from_provider"
    assert [i.trading_date for i in raised.value.coverage.issues] == list(window[:2])
    assert raised.value.coverage.window.added == len(window) - 2


class _DuplicatingTape(_SdkTape):
    """A vendor that sends one day aggregate twice."""

    def list_aggs(self, **kwargs):
        bars = list(super().list_aggs(**kwargs))
        yield from bars[:5]
        yield bars[4]
        yield from bars[5:]


def test_duplicate_or_out_of_order_vendor_bars_fail_loudly_on_both_paths_and_neither_computes(world):
    """Not a divergence: the sanitizer raised on a duplicate or a non-increasing timestamp, and the lake never publishes one."""
    window = window_of("1d")
    tape = tape_for(window)
    bad_day = window[2]
    duplicate = {**tape, bad_day: [*tape[bad_day][:5], tape[bad_day][4], *tape[bad_day][5:]]}
    mock_vendor("SPY", duplicate, adjusted=duplicate)

    service = PolygonClientService()
    service.client = _DuplicatingTape(tape, None, regular_only=True)  # type: ignore[assignment]
    fetched = service.fetch_aggregates(ticker="SPY", multiplier=1, timespan="day", from_date=window[0].isoformat(), to_date=window[-1].isoformat())
    with pytest.raises(ValueError, match="Duplicate timestamps"):
        DataSanitizer.sanitize_aggregates(fetched)
    with pytest.raises(StudyInputRefusal) as raised:
        lake_rows(window, bar_size="1d")

    assert raised.value.code == "data_unavailable_from_provider", "the lake never publishes a corrupt tape"
