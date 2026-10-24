"""Indicator reliability inputs: the old provider path against the admitted lake path, on one pinned vendor payload (#3523; R14, T08, D02).

Slice 33 of the UX redesign moves the study's bars from Polygon aggregates (``fetch_bars_chunked``, the regular-hours
filter, the warmup lead-in) to the lake. This module is the receipt for what that move changes, case by case. Both paths
read the *same* pinned vendor payload:

* the **provider path** is the old router's own sequence, rebuilt from the functions it called
  (``tests/_helpers/reliability_provider_path.py``): the real ``fetch_aggregates`` over the Polygon SDK's ``Agg`` parser,
  the real chunker with its fail-fast stream check, ``filter_session`` called as the router called it, the indicator over
  warmup plus window, the trim at ET midnight of ``from_date``;
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission, the
  reader — and the study's fold, then the same indicator and trim.

Each case records changed timestamps, prices, counts and derived outputs (the indicator's values; the in- and
out-of-sample mean IC of every horizon), and names the cause with the reconcile categories (``timestamp``, ``warmup``,
``precision``, ``off-by-one``, ``data-quality``). A case that moves nothing says so by asserting zero. Unexplained drift
fails here; an *intentional* divergence is the owner's decision (D02) and is listed in the PR.

The lake's 1/10,000 price grid is ``data-quality``, never ``precision``: it changes the input data, and ``precision`` is
floating-point accumulation. A tolerance may loosen only for the latter, so each output the grid moves is pinned at its
*measured* magnitude (``test_prices_finer_than_the_lake_grid...``), with a stated margin, at three price levels, because
the movement of an indicator such as RSI depends on the price it is computed at.

Not provable offline: that Polygon's own N-minute aggregate equals a fold of its minute aggregates. Cases that fold use an
independent reference for the vendor side (``vendor_fold``); that equivalence with the live API is an assumption of the
lake path (the same trades build both) and is unverified against live data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import respx

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.models.indicator_reliability_models import WarmupPolicy
from app.research import indicator_reliability as reliability_math
from app.research.study_inputs import lead_in_sessions
from app.services import dataset_service
from app.services.dataset_service import CanonicalBarsError, calculate_dynamic_indicators, filter_session
from app.services.indicator_reliability_service import (
    SUPPORTED_MINUTE_MULTIPLIERS,
    ReliabilityParams,
    ReliabilityRefusal,
    admit_reliability_inputs,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history
from tests._helpers.reliability_provider_path import ProviderFrame, provider_frame, seeded_baseline

NOW_MS = 1_735_689_600_000  # 2025-01-01

#: 2024-03-10 is the spring-forward Sunday.
DST_WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))

#: Every regular-session minute trades (so bar counts are exact); extended hours are present but thin.
DENSE = {"regular_keep": 1.0, "extended_keep": 0.5}
ENTRY = {"name": "rsi", "params": {"length": 14}}
HORIZONS = (1, 5)


def tape_for(window: list[date], *, lead: int = 3, start_price: float = 150.0, **options) -> dict[date, list[VendorBar]]:
    return vendor_history(
        [*lead_in_sessions(window[0], lead), *window], seed=11, start_price=start_price, **{**DENSE, **options}
    )


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


@dataclass
class LakeFrame:
    frame: pd.DataFrame
    indicator_column: str
    lead_in_bars: int
    warmup: dict


def lake_frame(
    window: list[date],
    *,
    timespan: str = "minute",
    multiplier: int = 1,
    now_ms: int = NOW_MS,
    warmup_policy: WarmupPolicy = "allow_shortened",
    entry: dict | None = None,
) -> LakeFrame:
    """The lake path as the router runs it: admit, run the indicator over warmup plus window, trim the warmup."""
    entry = entry or ENTRY
    study = admit_reliability_inputs(
        ReliabilityParams(
            symbol="SPY",
            indicator_name=entry["name"],
            indicator_params=entry["params"],
            from_date=window[0],
            to_date=window[-1],
            timespan=timespan,
            multiplier=multiplier,
            horizons=HORIZONS,
            warmup_policy=warmup_policy,
        ),
        now_ms=now_ms,
    )
    df, meta = calculate_dynamic_indicators(study.frame, [entry])
    lead_in = int((df["timestamp"] < study.window_start_ms).sum())
    df = df[df["timestamp"] >= study.window_start_ms].reset_index(drop=True)
    return LakeFrame(df, meta[0]["column"], lead_in, study.warmup.model_dump())


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
    indicator_max_diff: float = 0.0
    indicator_valid_moved: int = 0
    is_ic_max_diff: float = 0.0
    oos_ic_max_diff: float = 0.0
    lead_in_bars: tuple[int, int] = (0, 0)
    extras: dict = field(default_factory=dict)

    @property
    def timestamps_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def quantization_only(self) -> bool:
        """Every price that moved moved by at most half of the lake's 1/10,000 grid step."""
        return self.prices_changed > 0 and self.max_price_diff <= 5e-5 + 1e-9

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count or a magnitude decides."""
        found: list[str] = []
        if self.timestamps_changed:
            found.append("data-quality")  # bars present on one side only: the input itself differs
        if self.prices_changed:
            found.append("data-quality")  # a price that moved is a different input, even by half the lake's grid step
        if self.volume_changed:
            found.append("data-quality")
        if self.lead_in_bars[0] != self.lead_in_bars[1]:
            found.append("warmup")
        return list(dict.fromkeys(found))


def _ic_means(frame: pd.DataFrame, column: str) -> dict[int, tuple[float, float | None]]:
    raw, _, _ = reliability_math.compute_indicator_reliability_with_oos(
        df=frame, indicator_column=column, horizons=list(HORIZONS), include_slope=False,
        train_ratio=reliability_math.TRAIN_RATIO, random_simulations=20,
    )
    return {r.horizon: (r.is_mean_ic, r.oos_mean_ic) for r in raw}


def compare(provider: ProviderFrame, lake: LakeFrame, *, derived: bool = True) -> Comparison:
    p, q = provider.frame.set_index("timestamp"), lake.frame.set_index("timestamp")
    shared = p.index.intersection(q.index)
    out = Comparison(len(p), len(q), lead_in_bars=(provider.lead_in_bars, lake.lead_in_bars))
    out.only_provider, out.only_lake = len(p.index.difference(q.index)), len(q.index.difference(p.index))
    price = (p.loc[shared, ["open", "high", "low", "close"]] - q.loc[shared, ["open", "high", "low", "close"]]).abs().max(axis=1)
    out.prices_changed, out.max_price_diff = int((price > 0).sum()), float(price.max()) if len(price) else 0.0
    volume = (p.loc[shared, "volume"] - q.loc[shared, "volume"]).abs()
    out.volume_changed, out.max_volume_diff = int((volume > 0).sum()), float(volume.max()) if len(volume) else 0.0
    a, b = p.loc[shared, provider.indicator_column], q.loc[shared, lake.indicator_column]
    out.indicator_valid_moved = int((a.isna() != b.isna()).sum())
    both = ~(a.isna() | b.isna())
    out.indicator_max_diff = float((a[both] - b[both]).abs().max()) if both.any() else 0.0
    if derived:
        theirs, ours = _ic_means(provider.frame, provider.indicator_column), _ic_means(lake.frame, lake.indicator_column)
        out.is_ic_max_diff = max(abs(theirs[h][0] - ours[h][0]) for h in HORIZONS)
        out.oos_ic_max_diff = max(abs((theirs[h][1] or 0.0) - (ours[h][1] or 0.0)) for h in HORIZONS)
    return out


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: NOW_MS)
    seeded_baseline(monkeypatch)
    with respx.mock:
        mock_launcher()
        yield catalog


def clock(monkeypatch: pytest.MonkeyPatch, today: date) -> int:
    now = et_midnight_ms(today)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: now)
    return now


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", [DST_WINDOW, HALF_DAY_WINDOW], ids=["dst_change", "early_close_and_holiday"])
def test_prices_on_the_grid_across_a_dst_change_an_early_close_and_a_holiday_move_nothing(world, window):
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(
        provider_frame(tape, from_date=window[0], to_date=window[-1]), lake_frame(window)
    )

    assert (comparison.only_provider, comparison.only_lake) == (0, 0), "same bars, timestamp for timestamp"
    assert (comparison.prices_changed, comparison.volume_changed) == (0, 0)
    assert comparison.causes == []
    assert comparison.indicator_max_diff <= 1e-9, "the indicator is the same at the strict-float default"
    assert comparison.indicator_valid_moved == 0
    assert comparison.is_ic_max_diff <= 1e-9 and comparison.oos_ic_max_diff <= 1e-9
    assert comparison.lead_in_bars[0] == comparison.lead_in_bars[1] > 0, "the same warmup lead-in, bar for bar"


#: How many minutes past the hour the canonical calendar opens the regular session (the same on every session; the ET
#: offset is a whole number of hours).
OPEN_MINUTE_OF_HOUR = (session_open_ms_utc(DST_WINDOW[0]) // 60_000) % 60
#: The supported minute lengths whose buckets start on a wall-clock boundary that the open also sits on (they divide
#: the open's minute of the hour), so no bucket straddles the open: the lake's fold and the vendor's aggregate hold the
#: same minutes.
OPEN_ALIGNED_MINUTES = [m for m in SUPPORTED_MINUTE_MULTIPLIERS if m != 1 and OPEN_MINUTE_OF_HOUR % m == 0]
#: The other supported lengths below an hour. The bucket that holds the open starts before it.
OPEN_STRADDLING_MINUTES = [m for m in SUPPORTED_MINUTE_MULTIPLIERS if m < 60 and OPEN_MINUTE_OF_HOUR % m != 0]


@pytest.mark.parametrize("bar_minutes", OPEN_ALIGNED_MINUTES)
def test_coarser_minute_bars_fold_to_the_same_bars_as_the_vendor_side_aggregate(world, bar_minutes):
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(
        provider_frame(tape, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1], multiplier=bar_minutes),
        lake_frame(DST_WINDOW, multiplier=bar_minutes),
    )

    assert (comparison.timestamps_changed, comparison.prices_changed, comparison.volume_changed) == (0, 0, 0)
    assert comparison.indicator_max_diff <= 1e-9
    assert comparison.lead_in_bars[0] == comparison.lead_in_bars[1]


def test_the_bar_that_holds_the_open_is_the_one_difference_in_hourly_bars(world):
    """Cause: ``data-quality`` (session scope of a bar that straddles the open), and the #3527 omission on the old side.

    The vendor's 09:00 hourly aggregate holds 09:00-10:00 trades, pre-open ones included; the lake's holds the regular
    minutes inside it (09:30-10:00) because the session is applied to minutes before the fold. Every other hourly bar is
    the same. The old call also *dropped* the 09:00 bar altogether (``filter_session`` without the bar length).
    """
    window = HALF_DAY_WINDOW
    tape = tape_for(window, lead=12, extended_keep=0.9)
    mock_vendor("SPY", tape, adjusted=tape)
    entry = {"name": "rsi", "params": {"length": 5}}
    sessions = len(window)

    old_call = provider_frame(tape, from_date=window[0], to_date=window[-1], timespan="hour", indicator_params={"length": 5})
    fixed_call = provider_frame(
        tape, from_date=window[0], to_date=window[-1], timespan="hour", indicator_params={"length": 5},
        filter_with_bar_length=True,
    )
    lake = lake_frame(window, timespan="hour", entry=entry)
    versus_fixed = compare(fixed_call, lake, derived=False)
    versus_old = compare(old_call, lake, derived=False)

    assert versus_old.only_lake == sessions and versus_old.only_provider == 0, "the old call lost the open bar every session"
    assert (versus_fixed.timestamps_changed, versus_fixed.volume_changed > 0, versus_fixed.prices_changed > 0) == (0, True, True)
    assert versus_fixed.volume_changed == sessions and versus_fixed.prices_changed <= sessions, "only the open bar differs"
    assert versus_fixed.causes == ["data-quality"]
    assert len(lake.frame) == len(fixed_call.frame) == len(old_call.frame) + sessions


@pytest.mark.parametrize("bar_minutes", OPEN_STRADDLING_MINUTES)
def test_a_minute_length_that_straddles_the_open_differs_from_the_vendor_in_the_one_bar_that_holds_it(world, bar_minutes):
    """Cause: ``data-quality`` (session scope of a bar that straddles the open), as for the hourly bar below.

    A 4-, 12- or 20-minute bucket holding the open starts before it, so the vendor's aggregate holds pre-open trades
    the lake's, which applies the session before the fold, does not. Every other bar is the same. The
    old call dropped that bar altogether (``filter_session`` without the bar length).
    """
    window = DST_WINDOW
    assert bar_minutes in (4, 12, 20), "the supported lengths below an hour that the open's minute of the hour is not a multiple of"
    tape = tape_for(window, lead=12, extended_keep=1.0)
    mock_vendor("SPY", tape, adjusted=tape)
    sessions = len(window)

    old_call = provider_frame(tape, from_date=window[0], to_date=window[-1], multiplier=bar_minutes)
    fixed_call = provider_frame(tape, from_date=window[0], to_date=window[-1], multiplier=bar_minutes, filter_with_bar_length=True)
    lake = lake_frame(window, multiplier=bar_minutes)
    versus_fixed = compare(fixed_call, lake, derived=False)
    versus_old = compare(old_call, lake, derived=False)

    assert versus_old.only_lake == sessions and versus_old.only_provider == 0, "the old call lost the open bar every session"
    assert versus_fixed.timestamps_changed == 0
    assert versus_fixed.volume_changed == sessions and 0 < versus_fixed.prices_changed <= sessions, "only the open bar differs"
    assert versus_fixed.causes == ["data-quality"]


def test_the_old_session_filter_dropped_every_daily_bar_which_is_why_a_daily_request_is_now_refused():
    """#3527. Daily bars start at ET midnight, before the open; the call that omitted the bar length treated them as one minute."""
    days = expected_sessions(date(2024, 3, 4), date(2024, 3, 8))
    daily = pd.DataFrame(
        {"timestamp": [et_midnight_ms(day) for day in days], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    )

    omitted = filter_session(daily, "rth")
    given = filter_session(daily, "rth", timespan="day", multiplier=1)

    assert len(omitted) == 0, "every daily bar was dropped, and the study went on to say there was no data"
    assert len(given) == len(days)
    # The lake holds no vendor-equivalent daily bar to give a correct answer from (its rollup folds extended-hours
    # trades), so the study refuses the size with a reason instead (see test_indicator_reliability_inputs.py).


#: What the lake's price grid moves, measured on this pinned payload (seed 11, the DST window) at three price levels: the
#: largest absolute difference between the two paths' RSI(14) at a shared bar, and the largest difference in the
#: in-sample and out-of-sample mean IC over the horizons. Prices carry five or six decimals, finer than the grid. The
#: movement of RSI grows as the price falls: a rounding of at most 5e-5 is a larger share of a bar-to-bar change on a
#: $2 name than on a $150 one. Re-measure (print the three ``Comparison`` fields per case) only when the pinned tape or
#: the lake's encoding intentionally changes; never to make a failing test pass.
QUANTIZATION_MEASURED = {
    "price_150": (150.0, 5, {"indicator": 7.021e-3, "is_ic": 3.109e-5, "oos_ic": 3.921e-5}),
    "price_12": (12.0, 5, {"indicator": 1.0896e-1, "is_ic": 9.602e-5, "oos_ic": 4.453e-4}),
    "price_2": (2.0, 6, {"indicator": 5.980e-1, "is_ic": 1.235e-4, "oos_ic": 1.2827e-3}),
}
#: The headroom over a measured movement. The tape is seeded and the arithmetic deterministic, so this only absorbs
#: platform differences in float summation order inside pandas and numpy; a real change in the lake's encoding, or in
#: an indicator's math, moves these by orders of magnitude more.
MEASURED_MARGIN = 1.5


@pytest.mark.parametrize(("start_price", "decimals", "measured"), QUANTIZATION_MEASURED.values(), ids=QUANTIZATION_MEASURED)
def test_prices_finer_than_the_lake_grid_move_each_output_by_its_measured_amount_which_depends_on_the_price(
    world, start_price, decimals, measured
):
    """Cause: ``data-quality`` (input quantization). The lake rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``);
    the provider path does not. That changes the input data, so it is not ``precision`` and no float tolerance covers it.

    Each recorded output is pinned at its measured movement times ``MEASURED_MARGIN``, so a lake encoding change that
    moved the IC by 1e-3 would fail here. The table above is what D02 is decided on.
    """
    tape = tape_for(DST_WINDOW, start_price=start_price, decimals=decimals)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_frame(tape, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1]), lake_frame(DST_WINDOW))

    assert comparison.timestamps_changed == 0
    assert comparison.quantization_only, "every price that moved moved by at most half a ten-thousandth"
    assert comparison.causes == ["data-quality"]
    assert comparison.indicator_valid_moved == 0
    # atol is the measured movement with its margin; rtol is 0 because the quantity compared is itself a difference.
    assert 0 < comparison.indicator_max_diff <= measured["indicator"] * MEASURED_MARGIN
    assert 0 < comparison.is_ic_max_diff <= measured["is_ic"] * MEASURED_MARGIN
    assert 0 < comparison.oos_ic_max_diff <= measured["oos_ic"] * MEASURED_MARGIN


def test_fractional_share_volume_is_kept_exactly_by_both_paths(world):
    tape = tape_for(DST_WINDOW, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)
    entry = {"name": "obv", "params": {}}

    comparison = compare(
        provider_frame(tape, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1], indicator_name="obv", indicator_params={}),
        lake_frame(DST_WINDOW, entry=entry),
    )

    assert comparison.volume_changed == 0, "six-decimal volume survives the lake's text encoding exactly"
    assert comparison.indicator_max_diff <= 1e-9, "OBV accumulates volume; the sum of identical terms is identical (measured 0.0)"


def test_the_study_reads_no_vwap_or_transaction_count_so_dropping_them_moves_nothing(world):
    """The lake carries neither field. The study's indicators read named series (never ``vwap`` or ``transactions``)."""
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    provider = provider_frame(tape, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1])
    assert {"vwap", "transactions"} <= set(provider.frame.columns)
    lake = lake_frame(DST_WINDOW)
    assert not {"vwap", "transactions"} & set(lake.frame.columns)

    stripped = provider.frame.drop(columns=["vwap", "transactions"])
    entry = {"name": "obv", "params": {}}
    with_extras, _ = calculate_dynamic_indicators(provider.frame.copy(), [entry])
    without, _ = calculate_dynamic_indicators(stripped.copy(), [entry])

    np.testing.assert_allclose(with_extras["obv"].to_numpy(), without["obv"].to_numpy(), atol=1e-9, rtol=0, equal_nan=True)
    assert _ic_means(provider.frame, provider.indicator_column) == _ic_means(stripped, provider.indicator_column)


def test_a_split_announced_after_capture_is_not_in_a_held_window_until_the_lake_next_captures_the_symbol(world):
    """Cause: ``data-quality`` (adjustment as-of). The provider adjusts to *now*; the lake to its latest capture."""
    window = expected_sessions(date(2024, 6, 3), date(2024, 6, 14))
    held, later = window[:6], window[6:]
    split_day = later[0]
    raw = vendor_history([*lead_in_sessions(window[0], 3), *window], seed=11, start_price=1500.0, **DENSE)
    mock_vendor("SPY", raw, adjusted=raw)
    lake_frame(held)  # the lake captures the first six sessions before the split exists
    respx.mock.reset()
    mock_launcher()
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    calls = VendorCalls()
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split], calls=calls)
    provider_after_split = provider_frame(raw, from_date=held[0], to_date=held[-1], adjusted=adjusted)

    stale = compare(provider_after_split, lake_frame(held), derived=False)
    lake_frame(later)  # any later capture of the symbol rebuilds its stale adjusted days
    refreshed = compare(provider_after_split, lake_frame(held), derived=False)

    assert stale.causes == ["data-quality"] and stale.prices_changed == stale.provider_bars
    assert stale.max_price_diff > 100, "the held days still carry the pre-split basis"
    assert refreshed.prices_changed == 0 and refreshed.timestamps_changed == 0
    assert calls.count > 0


# --- warmup: silent before, typed now --------------------------------------------------------------------------------


def test_a_lead_in_the_providers_floor_cut_short_was_silent_and_is_reported_with_the_same_numbers(world, monkeypatch):
    """Cause: ``warmup``. Nothing a statistic reads changes; what changes is that the shortfall is no longer unreported."""
    today = date(2029, 3, 6)  # the provider's history begins on Friday 2024-03-08
    now = clock(monkeypatch, today)
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 15))
    tape = vendor_history(expected_sessions(date(2024, 3, 8), window[-1]), seed=11, start_price=150.0, **DENSE)
    mock_vendor("SPY", tape, adjusted=tape)
    entry = {"name": "ema", "params": {"length": 200}}

    old = provider_frame(tape, from_date=window[0], to_date=window[-1], indicator_name="ema", indicator_params={"length": 200})
    lake = lake_frame(window, now_ms=now, entry=entry)
    comparison = compare(old, lake)

    assert old.lead_in_bars == 390 < 1000, "the old path computed on a 390-bar lead-in where the policy asks for 1,000"
    assert lake.warmup["shortened"] and lake.warmup["cause"] == "provider_history_floor"
    assert lake.warmup["admitted_bars"] == old.lead_in_bars, "the report states exactly what the old path silently used"
    assert comparison.causes == [] and comparison.indicator_max_diff <= 1e-9
    assert comparison.is_ic_max_diff <= 1e-9 and comparison.oos_ic_max_diff <= 1e-9


def test_a_symbol_listed_inside_the_lead_in_was_silent_and_is_reported_and_require_full_refuses_it(world):
    """Cause: ``warmup``. The oldest lead-in session has no bars; both paths compute on the two that do."""
    window = DST_WINDOW[4:]
    lead = lead_in_sessions(window[0], 3)
    tape = vendor_history([*lead, *window], seed=11, start_price=150.0, **DENSE)
    del tape[lead[0]]
    mock_vendor("SPY", tape, adjusted=tape)
    entry = {"name": "ema", "params": {"length": 200}}

    old = provider_frame(tape, from_date=window[0], to_date=window[-1], indicator_name="ema", indicator_params={"length": 200})
    lake = lake_frame(window, entry=entry)
    comparison = compare(old, lake)
    with pytest.raises(ReliabilityRefusal) as refused:
        lake_frame(window, entry=entry, warmup_policy="require_full")

    assert old.lead_in_bars == 2 * 390 and lake.warmup["admitted_bars"] == old.lead_in_bars
    assert lake.warmup["cause"] == "symbol_history_starts" and lake.warmup["requested_sessions"] == 3
    assert comparison.causes == [] and comparison.indicator_max_diff <= 1e-9 and comparison.is_ic_max_diff <= 1e-9
    assert refused.value.code == "warmup_shortened"


# --- gaps, forming sessions, bad tapes --------------------------------------------------------------------------------


def test_a_window_session_the_vendor_lacks_was_computed_through_silently_and_is_refused_now(world):
    """Cause: ``data-quality``. The old path ran the indicator across the hole and returned a result; the lake refuses."""
    complete = tape_for(DST_WINDOW)
    holed = {day: bars for day, bars in complete.items() if day != DST_WINDOW[5]}
    mock_vendor("SPY", holed, adjusted=holed)

    old = provider_frame(holed, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1])
    full = provider_frame(complete, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1])
    with pytest.raises(ReliabilityRefusal) as refused:
        lake_frame(DST_WINDOW)

    assert len(old.frame) == len(full.frame) - 390, "the old result silently lacked a whole session"
    assert refused.value.code == "data_unavailable_from_provider"
    assert [(i.group, i.trading_date) for i in refused.value.coverage.issues] == [("window", DST_WINDOW[5])]
    silent = _ic_means(old.frame, old.indicator_column)
    honest = _ic_means(full.frame, full.indicator_column)
    assert max(abs(silent[h][0] - honest[h][0]) for h in HORIZONS) > 1e-9, "and its statistics were not the complete series'"


def test_a_session_still_forming_was_computed_on_and_is_refused_now(world):
    """Cause: ``data-quality`` (the forward-label tail). The provider serves the day's bars so far; the lake only completed days."""
    window = DST_WINDOW
    complete = tape_for(window)
    cut = session_open_ms_utc(window[-1]) + 120 * 60_000
    forming = {**complete, window[-1]: [b for b in complete[window[-1]] if b["t"] < cut]}
    mock_vendor("SPY", forming, adjusted=forming)
    mid_session = cut + 60_000

    old = provider_frame(forming, from_date=window[0], to_date=window[-1])
    with pytest.raises(ReliabilityRefusal) as refused:
        lake_frame(window, now_ms=mid_session)

    assert old.frame["timestamp"].max() < cut, "the old result ended mid-session, two hours in"
    assert refused.value.code == "window_not_complete" and window[-1].isoformat() in refused.value.reason


@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_duplicate_or_out_of_order_vendor_bars_failed_fast_before_and_are_refused_now_with_a_typed_code(world, fault):
    """Cause: ``data-quality``. Both paths refuse the tape; the old one as an untyped 500, the lake as a typed refusal."""
    tape = tape_for(DST_WINDOW)
    bad_day = DST_WINDOW[2]
    bars = tape[bad_day]
    tape[bad_day] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", tape, adjusted=tape)

    with pytest.raises(CanonicalBarsError, match="polygon_corrupt_timestamps"):
        provider_frame(tape, from_date=DST_WINDOW[0], to_date=DST_WINDOW[-1])
    with pytest.raises(ReliabilityRefusal) as refused:
        lake_frame(DST_WINDOW)

    assert refused.value.code == "data_unavailable_from_provider"
    assert "validation_failed" in refused.value.coverage.issues[0].reason


def test_the_insufficient_data_guard_counted_extended_hours_bars_and_now_counts_the_bars_the_study_uses(world):
    """Cause: ``off-by-one`` in what the guard counts, not in any statistic: the provider path's count includes every bar served."""
    window = DST_WINDOW
    tape = tape_for(window, extended_keep=0.9)
    mock_vendor("SPY", tape, adjusted=tape)

    old = provider_frame(tape, from_date=window[0], to_date=window[-1])
    lake = lake_frame(window)

    assert old.fetched_bars > old.lead_in_bars + len(old.frame), "the old guard's count included pre-market and after-hours bars"
    assert lake.lead_in_bars + len(lake.frame) == old.lead_in_bars + len(old.frame), "the bars the statistics run on are equal"


def test_the_trim_boundary_is_et_midnight_of_the_first_date_across_the_dst_change(world):
    window = DST_WINDOW[4:]  # starts Monday Mar 11, the first session after the spring-forward Sunday
    tape = tape_for(window)
    mock_vendor("SPY", tape, adjusted=tape)

    old = provider_frame(tape, from_date=window[0], to_date=window[-1])
    lake = lake_frame(window)

    assert old.window_start_ms == et_midnight_ms(window[0])
    assert lake.frame["timestamp"].min() == old.frame["timestamp"].min() == session_open_ms_utc(window[0])
