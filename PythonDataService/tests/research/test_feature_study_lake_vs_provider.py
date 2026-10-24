"""Feature study inputs: the old provider path against the admitted lake path, on one pinned vendor payload (#3521; R14, T08).

Slice 31 of the UX redesign moves the study's bars from ``PolygonClientService.fetch_aggregates`` to the lake. This
module is the receipt for what that move changes, case by case. Both paths read the *same* pinned vendor payload:

* the **provider path** is the real ``fetch_aggregates`` (the code the study used), fed ``Agg`` objects from the
  Polygon SDK's own parser;
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
  the reader — and the study's fold.

Each case records changed timestamps, prices, counts and derived outputs (every feature's values, the mean IC, the
forward-target validity), and names the cause in the reconcile taxonomy (``timestamp``, ``warmup``, ``precision``,
``off-by-one``, ``data-quality``). A case that moves nothing says so by asserting zero. Unexplained drift fails here; an
*intentional* divergence is one the owner decided on 2026-10-24 and is listed in ``docs/known-gaps.md``.

The lake's 1/10,000 price grid is ``data-quality``, never ``precision``: it changes the input data, and ``precision``
is floating-point accumulation. A tolerance may loosen only for the latter, so each output this divergence moves is
pinned at its *recorded* magnitude (``test_prices_finer_than_the_lake_grid...``) at three price levels, because the
movement of an indicator such as RSI depends on the price it is computed at. A recorded magnitude is an observation, and
its own reproducibility on another CPU is a ``precision`` question with a derived answer, not a multiple of the
magnitude: see "What float noise can move" below. The same applies to a rank statistic (the mean IC), which float noise
moves by reordering values it cannot tell apart.

Not provable offline: that Polygon's own N-minute aggregates equal a fold of its minute aggregates. Cases that fold use
an independent reference for the vendor side (``vendor_fold``); the equivalence with the live API is an assumption of
the lake path (the same trades build both) and is unverified against live data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import respx
from polygon.rest.models import Agg
from scipy import stats

from app.data_lake.admission import read_committed_bytes
from app.data_lake.capture_finality import CLAIM_LEASE_MS, VENDOR_DELAY_MS, capture_final_ms
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.config import ResearchConfig
from app.research.features.ta_features import TechnicalFeatures
from app.research.runner import run_feature_research
from app.research.study_inputs import StudyInputRefusal, admit_minute_inputs, bars_as_rows, fold_minute_bars
from app.research.target import compute_forward_log_return
from app.research.validation.ic import compute_information_coefficient
from app.services.feature_study_service import FeatureStudyParams, feature_study_identity
from app.services.polygon_client import PolygonClientService
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day, lake_file
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history

FEATURES = ["momentum_5m", "rsi_14", "realized_vol_30", "volume_zscore", "macd_signal"]
NOW_MS = 1_735_689_600_000  # 2025-01-01

#: The lake writer rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``): a price moves by at most half of it.
HALF_GRID = 5e-5

#: 2024-03-10 is the spring-forward Sunday.
DST_WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))

#: Dense enough that the 15-minute target clears the study's 70% validity gate; the gate is not under test here.
DENSE = {"regular_keep": 0.995, "extended_keep": 0.92}


def tape_for(window: list[date], *, start_price: float = 150.0, **options: object) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=11, start_price=start_price, **{**DENSE, **options})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


def vendor_fold(minutes: list[VendorBar], bar_minutes: int) -> list[VendorBar]:
    """An independent stand-in for the vendor's own N-minute aggregate: first/high/low/last/sum per wall-clock bucket."""
    bucket_ms = bar_minutes * 60_000
    out: dict[int, VendorBar] = {}
    for m in minutes:
        key = m["t"] // bucket_ms * bucket_ms
        if key not in out:
            out[key] = {**m, "t": key}
        else:
            bar = out[key]
            bar["h"], bar["l"], bar["c"], bar["v"] = max(bar["h"], m["h"]), min(bar["l"], m["l"]), m["c"], bar["v"] + m["v"]
    return [out[k] for k in sorted(out)]


class _SdkTape:
    """The SDK's ``RESTClient.list_aggs`` over a pinned tape: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, raw: Mapping[date, list[VendorBar]], adjusted: Mapping[date, list[VendorBar]] | None) -> None:
        self.raw, self.adjusted = raw, adjusted

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        tape = self.adjusted if (adjusted and self.adjusted is not None) else self.raw
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        minutes = [b for day, bars in sorted(tape.items()) if start <= day <= end for b in bars]
        for bar in minutes if multiplier == 1 else vendor_fold(minutes, multiplier):
            yield Agg.from_dict(bar)


def provider_rows(
    raw: Mapping[date, list[VendorBar]],
    window: list[date],
    *,
    bar_minutes: int = 1,
    adjusted: Mapping[date, list[VendorBar]] | None = None,
) -> list[dict]:
    """What the study read before #3521: ``fetch_aggregates`` for the window, adjusted, every session of the day."""
    service = PolygonClientService()
    service.client = _SdkTape(raw, adjusted)  # type: ignore[assignment]
    return service.fetch_aggregates(
        ticker="SPY", multiplier=bar_minutes, timespan="minute", from_date=window[0].isoformat(), to_date=window[-1].isoformat()
    )


def lake_rows(window: list[date], *, bar_minutes: int = 1, session: str = "extended") -> list[dict]:
    identity = feature_study_identity(FeatureStudyParams("SPY", "rsi_14", window[0], window[-1], "minute", bar_minutes, session))  # type: ignore[arg-type]
    admitted = admit_minute_inputs(identity.inputs, requester="comparison", now_ms=NOW_MS)
    return bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes))


def held_rows(world: World, window: list[date]) -> list[dict]:
    """What the lake holds for ``window`` as committed, read with no admission pass and no refresh in front of it."""
    reader = LeanMinuteDataReader(resolve_data_roots(source="polygon", adjusted=True), session="extended")
    bars = [
        bar
        for day in window
        for bar in reader.parse_day_zip(
            read_committed_bytes(lake_file(world.lake, "polygon_split_adjusted", day)), "SPY", day
        )
    ]
    return bars_as_rows(fold_minute_bars(bars, 1))


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
    max_volume_diff: float = 0.0
    feature_max_diff: dict[str, float] = field(default_factory=dict)
    feature_valid_moved: int = 0
    mean_ic: tuple[float | None, float | None] = (None, None)
    target_valid: tuple[int | None, int | None] = (None, None)
    errors: tuple[str | None, str | None] = (None, None)

    @property
    def timestamps_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count decides.

        Every input that differs between the paths is ``data-quality``: a bar on one side only, a price, a volume.
        ``precision`` is floating-point accumulation, and nothing here accumulates: a lake price is the exact decimal
        the writer encoded, converted to the nearest float once.
        """
        return ["data-quality"] if self.timestamps_changed or self.prices_changed or self.volume_changed else []

    @property
    def quantization_only(self) -> bool:
        """Every price that moved moved by at most half of the lake's 1/10,000 grid: input quantization, not a new tape."""
        return self.prices_changed > 0 and self.max_price_diff <= HALF_GRID + 1e-9


def _report(rows: list[dict], window: list[date]):
    return run_feature_research("SPY", "rsi_14", rows, window[0].isoformat(), window[-1].isoformat(), ResearchConfig())


def compare(provider: list[dict], lake: list[dict], window: list[date], *, derived: bool = True) -> Comparison:
    by_ts_provider = {r["timestamp"]: r for r in provider}
    by_ts_lake = {r["timestamp"]: r for r in lake}
    shared = sorted(by_ts_provider.keys() & by_ts_lake.keys())
    result = Comparison(provider_bars=len(provider), lake_bars=len(lake))
    result.only_provider = len(by_ts_provider.keys() - by_ts_lake.keys())
    result.only_lake = len(by_ts_lake.keys() - by_ts_provider.keys())
    for ts in shared:
        p, q = by_ts_provider[ts], by_ts_lake[ts]
        price_diff = max(abs(p[c] - q[c]) for c in ("open", "high", "low", "close"))
        if price_diff > 0:
            result.prices_changed += 1
            result.max_price_diff = max(result.max_price_diff, price_diff)
        volume_diff = abs(p["volume"] - q["volume"])
        if volume_diff > 0:
            result.volume_changed += 1
            result.max_volume_diff = max(result.max_volume_diff, volume_diff)
    if not derived:
        return result
    # Each path's features are computed on its own full series, as the study would, then read at the shared bars:
    # a different sample moves a feature's value (an RSI remembers its history) as well as its warmup region.
    for feature in FEATURES:
        a = dict(zip(sorted(by_ts_provider), TechnicalFeatures.compute_feature(feature, provider).to_numpy(), strict=True))
        b = dict(zip(sorted(by_ts_lake), TechnicalFeatures.compute_feature(feature, lake).to_numpy(), strict=True))
        diffs = [abs(a[ts] - b[ts]) for ts in shared if not (np.isnan(a[ts]) or np.isnan(b[ts]))]
        result.feature_valid_moved += sum(1 for ts in shared if np.isnan(a[ts]) != np.isnan(b[ts]))
        result.feature_max_diff[feature] = float(max(diffs)) if diffs else 0.0
    rp, rq = _report(provider, window), _report(lake, window)
    result.errors = (rp.error, rq.error)
    result.mean_ic = (None if rp.error else rp.mean_ic, None if rq.error else rq.mean_ic)
    result.target_valid = (rp.target.valid_count if rp.target else None, rq.target.valid_count if rq.target else None)
    return result


# ---------------------------------------------------------------------------
# What float noise can move
# ---------------------------------------------------------------------------
#
# A number recorded from one run is reproducible on another CPU only to the float noise of the computation that made it.
# Both kinds of recorded output below are class ``precision`` (floating-point accumulation), and neither tolerance is a
# multiple of the recorded value, because the noise does not scale with it.

#: A feature's largest movement between the paths, a *continuous* output. Each feature is a rolling or exponentially
#: weighted reduction over N <= 9,130 bars, whose float64 error is at most N * eps * scale = 9,130 * 2.2e-16 * 150 = 3e-10
#: at the largest scale here (MACD, in price units; RSI is on 0-100). The movement is a difference of two such
#: evaluations: at most 6e-10 on any platform. Measured between two mathematically identical series (the split case
#: below), 3.1e-12. This is the repo's strict-float default.
FEATURE_ATOL = 1e-9

#: A mean IC is a mean of daily rank correlations, and a rank is not continuous in the values it orders. Two feature
#: values (or two forward returns) that the mathematics makes equal come out of float arithmetic a few ulps apart, in an
#: order that can depend on the platform (instruction set, FMA contraction, vectorised reductions). Reordering such a
#: pair moves a daily Spearman rho by 12 * |rank gap in the other variable| / (n * (n^2 - 1)), up to 1.5e-5 at n = 900.
#: That is not N * eps: it is a count of near-ties times a rank gap, and it is what moved the split case's mean IC from
#: 2.1e-7 (local) to 5.9e-7 (CI). ``tie_envelope`` bounds it from the data. "Equal" means within these noise floors,
#: set well above the float error of the value (RSI, scale 100: 4e-10 worst case by the bound above, 3e-12 measured; a
#: log return: 4 eps ~ 1e-15) and well below the gaps the data otherwise shows (on the split tape the RSI pairs fall in
#: two clusters, 26 under 1e-9 and the rest over 1e-6; the returns, 6 under 1e-12 and the rest over 1e-9).
FEATURE_TIE_NOISE = 1e-9
TARGET_TIE_NOISE = 1e-12

#: The envelope omits two second-order terms: a pair tied in both variables (+-1 on the sum of rank products, 1.6e-8 of
#: rho at n = 900) and the rank variance that average ranks shrink (<= 1e-9). Each is orders below the 5e-6 envelope.
RANK_SECOND_ORDER_ATOL = 1e-7


@dataclass(frozen=True)
class TieEnvelope:
    """The range a window's mean IC spans when float noise reorders values it cannot tell apart."""

    lo: float
    hi: float

    @property
    def width(self) -> float:
        return self.hi - self.lo


def _ranks_with_ties_ordered_by(values: np.ndarray, tie_break: np.ndarray, noise: float) -> np.ndarray:
    """0-based ranks of ``values``, where values within ``noise`` of their neighbour are ordered by ``tie_break``."""
    order = np.argsort(values, kind="stable")
    group = np.concatenate(([0], np.cumsum(np.diff(values[order]) > noise)))
    ranks = np.empty(len(values))
    ranks[order[np.lexsort((tie_break[order], group))]] = np.arange(len(values))
    return ranks


def tie_envelope(rows: list[dict]) -> TieEnvelope:
    """The mean IC (the study's, on ``rows``) with every group of float-indistinguishable values ordered to agree with the
    other variable's ranks (``hi``) and to disagree with them (``lo``).

    Within a group, the sum of rank products is extremal at those two orders (the rearrangement inequality), so the daily
    rho of any other order lies between. The feature's groups are ordered first, then the target's against the result.
    """
    config = ResearchConfig()
    frame = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    frame["feature"] = TechnicalFeatures.compute_feature("rsi_14", rows).to_numpy()
    frame["target"] = compute_forward_log_return(bars=rows, horizon_minutes=config.horizon_minutes).values.to_numpy()
    frame["date"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.date.astype(str)
    counted = compute_information_coefficient(
        frame["feature"], frame["target"], frame["timestamp"], correlation_method=config.ic_correlation_method
    ).daily_ic_dates  # the days the study itself counts
    lows, highs = [], []
    for day in counted:
        clean = frame.loc[frame["date"] == day, ["feature", "target"]].dropna()
        x, y = clean["feature"].to_numpy(), clean["target"].to_numpy()
        rho = []
        for sign in (+1, -1):
            rx = _ranks_with_ties_ordered_by(x, sign * stats.rankdata(y), FEATURE_TIE_NOISE)
            ry = _ranks_with_ties_ordered_by(y, sign * rx, TARGET_TIE_NOISE)
            rho.append(np.corrcoef(rx, ry)[0, 1])
        lows.append(min(rho))
        highs.append(max(rho))
    return TieEnvelope(lo=float(np.mean(lows)), hi=float(np.mean(highs)))


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


def test_prices_on_the_grid_across_a_dst_change_and_with_gaps_move_nothing(world):
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW), DST_WINDOW)

    assert comparison.errors == (None, None)
    assert (comparison.only_provider, comparison.only_lake) == (0, 0), "same bars, timestamp for timestamp"
    assert (comparison.prices_changed, comparison.volume_changed) == (0, 0)
    assert comparison.causes == []
    assert max(comparison.feature_max_diff.values()) <= 1e-9, "every feature value is the same at the strict-float default"
    assert comparison.feature_valid_moved == 0, "and so is every feature's NaN warmup region"
    assert comparison.mean_ic[0] == pytest.approx(comparison.mean_ic[1], abs=1e-9, rel=0)
    assert comparison.target_valid[0] == comparison.target_valid[1]
    assert comparison.lake_bars < len(DST_WINDOW) * 960, "the gaps are in both; nothing is filled"


@pytest.mark.parametrize("bar_minutes", [3, 5, 15])
def test_coarser_bars_fold_to_the_same_bars_as_the_vendor_side_aggregate(world, bar_minutes):
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(
        provider_rows(tape, DST_WINDOW, bar_minutes=bar_minutes),
        lake_rows(DST_WINDOW, bar_minutes=bar_minutes),
        DST_WINDOW,
    )

    assert comparison.errors == (None, None)
    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert max(comparison.feature_max_diff.values()) <= 1e-9


#: What the lake's price grid moves, recorded from this pinned payload (seed 11, the DST window, 9,130 bars) at three
#: price levels, as the largest absolute difference between the two paths' values at a shared bar, and the window's
#: mean IC. Prices carry five or six decimals, finer than the grid. The movement of RSI and momentum grows as the price
#: falls: a rounding of at most 5e-5 is a larger share of a bar-to-bar change on a $2 name than on a $150 one.
#: Re-record (print ``feature_max_diff`` and ``abs(mean_ic[1] - mean_ic[0])`` per case) only when the pinned tape or the
#: lake's encoding intentionally changes; never to make a failing test pass. Digits are kept to 1e-10 or finer, below
#: ``FEATURE_ATOL``, so the recording adds no tolerance of its own. The mean IC was re-recorded once (#3523), when the
#: daily IC moved from UTC to New York days: this tape holds extended hours in standard time, whose 19:00 to 20:00 ET
#: bars changed day, and each path's mean IC equals an independent mean of per-New-York-date Spearman coefficients.
QUANTIZATION_RECORDED = {
    "price_150": (150.0, 5, {"mean_ic": 2.343130e-6, "momentum_5m": 6.01537004e-7, "rsi_14": 1.108764307e-2, "realized_vol_30": 2.034791103e-7, "macd_signal": 8.521013899e-6}),
    "price_12": (12.0, 5, {"mean_ic": 2.674984e-5, "momentum_5m": 7.499870746e-6, "rsi_14": 1.118103039e-1, "realized_vol_30": 2.211433761e-6, "macd_signal": 8.492849363e-6}),
    "price_2": (2.0, 6, {"mean_ic": 1.182750e-4, "momentum_5m": 4.772232477e-5, "rsi_14": 6.354326903e-1, "realized_vol_30": 1.611649952e-5, "macd_signal": 8.984062728e-6}),
}


@pytest.mark.parametrize(("start_price", "decimals", "recorded"), QUANTIZATION_RECORDED.values(), ids=QUANTIZATION_RECORDED)
def test_prices_finer_than_the_lake_grid_move_each_output_by_its_recorded_amount_which_depends_on_the_price(
    world, start_price, decimals, recorded
):
    """Cause: ``data-quality`` (input quantization). The lake rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``);
    the provider path does not. That changes the input data, so it is not ``precision`` and no float tolerance covers it.

    Each recorded output is pinned to the movement measured on the pinned tape, so a lake encoding change that moved the
    mean IC by 1e-3 would fail here. The table above is the cost the owner accepted: RSI moves by about 0.01 point on a $150
    name, 0.1 on a $12 name and 0.6 on a $2 name; the mean IC by 2e-6, 3e-5 and 1e-4. The recording itself is read back
    at the float noise of the computation (``precision``, derived under "What float noise can move"): a feature to
    ``FEATURE_ATOL``, the mean IC to the reach of the near-ties in the two series it is taken from, which is nil when
    the series hold none (the $150 tape) and widest on the $2 tape, where the lake's grid manufactures ties.
    """
    tape = tape_for(DST_WINDOW, start_price=start_price, decimals=decimals)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW)

    comparison = compare(provider, lake, DST_WINDOW)

    assert comparison.errors == (None, None)
    assert comparison.timestamps_changed == 0
    assert comparison.quantization_only, "every price that moved moved by at most half a ten-thousandth"
    assert comparison.causes == ["data-quality"]
    assert comparison.feature_valid_moved == 0 and comparison.target_valid[0] == comparison.target_valid[1]
    assert comparison.feature_max_diff["volume_zscore"] == 0.0, "volume is not a price"
    for name in ("momentum_5m", "rsi_14", "realized_vol_30", "macd_signal"):
        assert comparison.feature_max_diff[name] > 0, name
        np.testing.assert_allclose(comparison.feature_max_diff[name], recorded[name], atol=FEATURE_ATOL, rtol=0, err_msg=name)
    ic_provider, ic_lake = comparison.mean_ic
    assert abs(ic_lake - ic_provider) > 0, "the divergence is real: the IC does move"
    # Each series' mean IC may sit anywhere in its own envelope on another platform, so the movement may differ from the
    # recorded one by at most the two envelopes' widths.
    reach = tie_envelope(provider).width + tie_envelope(lake).width
    np.testing.assert_allclose(abs(ic_lake - ic_provider), recorded["mean_ic"], atol=reach + RANK_SECOND_ORDER_ATOL, rtol=0)


def test_fractional_share_volume_is_kept_exactly_by_both_paths(world):
    tape = tape_for(DST_WINDOW, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW), DST_WINDOW)

    assert comparison.volume_changed == 0, "six-decimal volume survives the lake's text encoding exactly"
    assert comparison.feature_max_diff["volume_zscore"] <= 1e-9


def test_an_early_close_week_matches_under_extended_hours(world):
    tape = tape_for(HALF_DAY_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, HALF_DAY_WINDOW), lake_rows(HALF_DAY_WINDOW), HALF_DAY_WINDOW)

    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert comparison.errors == (None, None)


def test_regular_hours_is_a_different_sample_from_what_the_study_has_always_read(world):
    """Cause: ``data-quality`` (session scope). The study ran on 04:00-20:00 ET; ``regular`` removes the rest, and the page offers both.

    The sample changes, and a feature remembers its sample: an RSI at the same bar differs because its history does.
    The numbers below are on the pinned synthetic tape, a measure of how much the policy moves the inputs, not of how
    much it would move a live name's IC.
    """
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    provider = provider_rows(tape, DST_WINDOW)

    comparison = compare(provider, lake_rows(DST_WINDOW, session="regular"), DST_WINDOW)

    assert comparison.errors == (None, None)
    assert comparison.only_lake == 0 and comparison.only_provider > 0
    assert comparison.lake_bars < 0.5 * comparison.provider_bars, "extended hours are most of the minutes in a day"
    assert comparison.prices_changed == 0, "the bars that remain are the vendor's own"
    assert comparison.causes == ["data-quality"]
    assert comparison.feature_max_diff["rsi_14"] > 1.0, "the same bar's feature value moves with the sample"
    assert comparison.mean_ic[0] != pytest.approx(comparison.mean_ic[1], abs=1e-3)
    assert comparison.target_valid[1] < comparison.target_valid[0]


def test_a_split_announced_after_capture_is_not_in_a_held_window_until_the_lake_next_captures_the_symbol(world):
    """Cause: ``data-quality`` (adjustment as-of). The provider adjusts to *now*; the lake to its latest capture.

    A window the lake already holds is read as it was captured: a split that lands afterwards is invisible to it until
    any later capture for the symbol refreshes the adjusted history. The provider path always saw the new basis.
    """
    window = expected_sessions(date(2024, 6, 3), date(2024, 6, 14))
    held, later = window[:6], window[6:]
    split_day = later[0]
    raw = vendor_history(window, seed=11, start_price=1500.0, **DENSE)
    mock_vendor("SPY", raw, adjusted=raw)
    lake_rows(held)  # the lake captures the first six sessions before the split exists
    respx.mock.reset()
    mock_launcher()
    # Four decimals: a 10-for-1 split of two-decimal prices lands exactly on the lake's grid, so the only difference
    # between the paths is the split basis, not a rounding of the synthetic adjusted tape.
    adjusted = {day: (scaled({day: bars}, 0.1, decimals=4)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    calls = VendorCalls()
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split], calls=calls)
    provider_after_split = provider_rows(raw, held, adjusted=adjusted)

    stale_lake = lake_rows(held)
    stale = compare(provider_after_split, stale_lake, held)
    lake_rows(later)  # any later capture of the symbol rebuilds its stale adjusted days
    refreshed = compare(provider_after_split, lake_rows(held), held)

    assert stale.causes == ["data-quality"] and stale.prices_changed == stale.provider_bars
    assert stale.max_price_diff > 100, "the held days still carry the pre-split basis"
    assert stale.errors == (None, None)
    # The derived outputs of the split the owner accepted: a split rescales the price level, not the shape of the series.
    # Ratios, momentum and volatility are unit-free and do not move; MACD's signal line is in price units and does.
    for unit_free in ("momentum_5m", "rsi_14", "realized_vol_30", "volume_zscore"):
        assert stale.feature_max_diff[unit_free] <= 1e-9, unit_free
    assert stale.feature_max_diff["macd_signal"] > 1.0, "a price-level output moves by the split factor (measured 3.5 here)"
    # The IC is scale-free too, but a rank correlation orders values that are equal to 1e-12 by float noise, and the two
    # bases (0.1x prices, stored to 4 decimals, against the originals) round differently. That is ``precision``: the
    # IC may differ by at most the reach of the near-ties, which ``tie_envelope`` derives from the data. It is a bound,
    # not a measurement: the movement was 2.1e-7 locally and 5.9e-7 on CI, one platform's draw of the same ties.
    provider_envelope, lake_envelope = tie_envelope(provider_after_split), tie_envelope(stale_lake)
    reach = max(lake_envelope.hi - provider_envelope.lo, provider_envelope.hi - lake_envelope.lo)
    assert reach < 1e-5, "the near-ties are few enough that this bound still distinguishes a real movement of the IC"
    assert abs(stale.mean_ic[1] - stale.mean_ic[0]) <= reach + RANK_SECOND_ORDER_ATOL
    assert refreshed.prices_changed == 0 and refreshed.timestamps_changed == 0
    assert max(refreshed.feature_max_diff.values()) <= 1e-9
    assert calls.count > 0


def test_a_day_captured_before_its_after_hours_tape_finished_was_a_shorter_sample_and_is_refused_now(world):
    """Cause: ``data-quality`` (capture time). A regular-hours chart opened at 16:30 ET leaves the day with its
    after-hours tape cut at 16:15, and the lake reads it as complete. The provider path re-fetched the whole day on
    every run, so the lake path would compute on a shorter sample. Admission sees when the day was captured and refuses
    it (``captured_before_final``): it neither computes on the short tape nor rewrites a day other readers share. A held
    early day stays refused, with no re-capture path (``docs/known-gaps.md``), so the divergence is a refusal. A day with
    no recorded capture time cannot be judged: that is the residual.
    """
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    lake_rows(DST_WINDOW)  # every session captured once, after it had finished forming
    day = DST_WINDOW[3]
    cut = session_close_ms_utc(day) + VENDOR_DELAY_MS
    commit_bars(
        world.catalog, world.lake, day,
        corrected_day(day, [b for b in tape[day] if b["t"] < cut], bump="0"), mode="polygon_split_adjusted",
    )
    row = next(r for r in world.catalog.rows.values() if r["trading_date"] == day and r["price_adjustment_mode"] == "polygon_split_adjusted")
    row["completed_at_ms"] = capture_final_ms(day, "regular") + CLAIM_LEASE_MS
    provider = provider_rows(tape, DST_WINDOW)

    held = compare(provider, held_rows(world, DST_WINDOW), DST_WINDOW)
    with pytest.raises(StudyInputRefusal) as raised:
        lake_rows(DST_WINDOW)

    assert held.errors == (None, None)
    assert held.causes == ["data-quality"] and held.only_lake == 0
    assert held.only_provider == sum(1 for b in tape[day] if b["t"] >= cut), "exactly the bars after 16:15 are gone"
    assert held.prices_changed == 0, "what remains is the vendor's own"
    assert held.feature_max_diff["rsi_14"] > 1e-9 or held.feature_valid_moved > 0, "a feature remembers the shorter sample"
    assert held.target_valid[1] < held.target_valid[0], "and the last bars of that day lose their forward label"
    assert raised.value.code == "captured_before_final", "admission refuses that sample instead of computing on it"
    assert [(i.trading_date, i.outcome) for i in raised.value.coverage.issues] == [(day, "captured_before_final")]


def test_a_window_that_starts_before_the_symbol_traded_was_computed_on_fewer_bars_and_is_refused_now(world):
    """Cause: ``data-quality`` (a session the vendor has no bars for). The provider path computed on the sessions it
    got; the lake path refuses a window with a session the vendor answers empty, because "the symbol had not listed
    yet" and "the vendor failed to serve it" look the same to admission (no "not applicable" outcome, ``known-gaps``)."""
    tape = tape_for(DST_WINDOW)
    listed = {day: bars for day, bars in tape.items() if day >= DST_WINDOW[2]}
    mock_vendor("SPY", listed, adjusted=listed)

    before = _report(provider_rows(listed, DST_WINDOW), DST_WINDOW)
    with pytest.raises(StudyInputRefusal) as raised:
        lake_rows(DST_WINDOW)

    assert before.error is None, "before: the study ran on the eight sessions the vendor served"
    assert raised.value.code == "data_unavailable_from_provider"
    assert [i.trading_date for i in raised.value.coverage.issues] == list(DST_WINDOW[:2])
    assert raised.value.coverage.window.added == len(DST_WINDOW) - 2


def test_duplicate_or_out_of_order_vendor_bars_were_tolerated_or_crashed_before_and_are_refused_now(world):
    """Cause: ``data-quality``. The provider path sorted out-of-order bars silently and failed late on a duplicate."""
    tape = tape_for(DST_WINDOW)
    bad_day = DST_WINDOW[2]
    out_of_order = {**tape, bad_day: [*tape[bad_day][:5], tape[bad_day][6], tape[bad_day][5], *tape[bad_day][7:]]}
    duplicate = {**tape, bad_day: [*tape[bad_day][:5], tape[bad_day][4], *tape[bad_day][5:]]}

    reordered_report = _report(provider_rows(out_of_order, DST_WINDOW), DST_WINDOW)
    duplicate_report = _report(provider_rows(duplicate, DST_WINDOW), DST_WINDOW)

    assert reordered_report.error is None, "before: an out-of-order bar was sorted into place without a word"
    assert duplicate_report.error is not None and "duplicate" in duplicate_report.error, "before: a duplicate failed late"
    for bad in (out_of_order, duplicate):
        respx.mock.reset()
        mock_launcher()
        mock_vendor("SPY", bad)
        with pytest.raises(StudyInputRefusal) as raised:
            admit_minute_inputs(
                feature_study_identity(FeatureStudyParams("SPY", "rsi_14", DST_WINDOW[0], DST_WINDOW[-1], "minute", 1)).inputs,
                requester="comparison",
                now_ms=NOW_MS,
            )
        assert raised.value.code == "data_unavailable_from_provider", "now: the lake never publishes a corrupt tape"


def test_the_study_reads_no_vwap_or_transaction_count_so_dropping_them_moves_nothing(world):
    """The lake carries neither field. The study's math never reads them, so the omission cannot move an output."""
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    provider = provider_rows(tape, DST_WINDOW)
    assert {"vwap", "transactions"} <= provider[0].keys()
    lake = lake_rows(DST_WINDOW)
    assert not {"vwap", "transactions"} & lake[0].keys()

    stripped = [{k: v for k, v in row.items() if k not in ("vwap", "transactions")} for row in provider]
    ours, theirs = _report(lake, DST_WINDOW), _report(stripped, DST_WINDOW)

    assert ours.mean_ic == pytest.approx(theirs.mean_ic, abs=1e-9, rel=0)
