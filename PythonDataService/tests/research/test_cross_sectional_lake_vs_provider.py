"""Cross-sectional stock bars: the old provider path against the admitted lake path, over a MODELED daily payload, and the study's daily bar against the vendor's real one (#3524; R14, T08).

Slice 34 of the UX redesign moves the study's stock bars from ``PolygonClientService.fetch_aggregates(..., "day")`` to
the lake. This module is the receipt for what that move changes, case by case. Both paths read the *same* pinned
minute tape:

* the **provider path** is the real ``fetch_aggregates`` (the code the study used), fed ``Agg`` objects from the
  Polygon SDK's own parser, over a daily payload the test **models** from that tape (``modeled_daily``);
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
  the reader — and the study's daily fold.

Each case records changed timestamps, prices, counts and derived outputs, and names the cause in the reconcile taxonomy
(``timestamp``, ``data-quality``, ``off-by-one``). The derived outputs are the three things a stock bar feeds in this
study: the daily returns behind the N_eff correlation, the forward targets the IC is measured against, and — through the
stock close — the ATM contract pick and the IV spot. A case that moves nothing says so by asserting zero. Unexplained
drift fails here.

The daily bar is the regular session, and the owner chose that (2026-10-24, #3524): the minute stamped 16:00 is not folded
in. ``modeled_daily`` builds the vendor's daily as the regular-session fold of the same minutes, so under the default
``regular`` policy the two paths agree on price and volume by construction. The cases that use it prove the plumbing (the
admitted lake path equals an independent fold; the anchor, grid, gap, early-close and DST behaviour) and what the session
policy moves. They do not show that Polygon's own daily aggregate equals the regular fold. That is measured on a real
payload by ``test_the_regular_fold_of_a_real_spy_week_equals_the_vendors_real_daily_open_high_and_low`` below, against
golden fixture ``SPY-DAILY-001`` (the vendor's daily bars for 2025-01-13 to 17, ``adjusted=false``, recorded 2026-10-24)
and the repo's SPY one-minute capture of the same week: the open, high and low are exact on all five days; the close
differs by up to $0.15 (the vendor's is the official print); the fold holds 71.5 to 84.7 percent of the vendor's volume
(the vendor counts trades its minute aggregates omit). ``test_the_regular_fold_of_a_real_spy_week_stops_at_the_15_59_minute...``
measures what the fold leaves out: the minute stamped 16:00.

The IV and contract-pick comparison runs on the first fortnight of March 2024 (11 sessions). The study's IC, N_eff and
binomial test are not compared end to end here: a window long enough to compute them needs 30 aligned IV days, and the
comparison is about what a stock bar feeds. Returns, targets, the ATM pick, the spot and the IV are compared.

The lake's 1/10,000 price grid is ``data-quality`` (input quantization), never ``precision``: it changes the input data,
and ``precision`` is floating-point accumulation. Each output it moves is pinned two-sided at its measured magnitude, to
full precision, within the float error of that output's arithmetic (``test_prices_finer_than_the_lake_grid...``): a return
carries an absolute error of a couple of ``eps`` whatever its size, a target is a Lipschitz function of returns, and a
solved IV carries the rounding of the option price over its vega.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import respx
from polygon.rest.models import Agg

from app.config import settings
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.batch_runner import _extract_daily_returns
from app.research.cross_sectional_inputs import admit_universe, cross_sectional_identity
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_inputs import RateTally
from app.research.options_runner import _compute_daily_forward_return
from app.services.polygon_client import PolygonClientService
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.option_chain import OptionChain, member_inputs
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history
from tests.fixtures.golden_support.registry import default as registry

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01

#: 2024-03-10 is the spring-forward Sunday.
DST_WINDOW = expected_sessions(date(2024, 3, 1), date(2024, 3, 15))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))
LOW_EXPIRY, HIGH_EXPIRY = date(2024, 3, 29), date(2024, 4, 15)

TARGETS = ("directional", "volatility", "abs_return")
#: The lake writer rounds half-up to 1/10,000 (``lean_writer.to_deci_cent``): a price moves by at most half of it.
HALF_GRID = 5e-5


def tape_for(window: list[date], *, start_price: float = 150.0, **options: object) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=11, start_price=start_price, **{"regular_keep": 0.9, "extended_keep": 0.4, **options})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


def modeled_daily(tape: Mapping[date, list[VendorBar]]) -> list[VendorBar]:
    """A MODEL of the vendor's daily aggregate of the same trades: the regular session, stamped 00:00 ET.

    First/high/low/last/sum over the minutes the calendar puts in the scheduled session (an early close ends it at
    13:00), the volume-weighted average of the minutes' VWAPs, summed trade counts. ``math.fsum`` so the sum is exactly
    rounded, as the lake's decimal sum is. This is the assumption the module docstring names, not Polygon's bar: a case
    that compares the lake with it under ``regular`` agrees by construction.
    """
    bars: list[VendorBar] = []
    for day, minutes in sorted(tape.items()):
        open_ms, close_ms = session_open_ms_utc(day), session_close_ms_utc(day)
        regular = [m for m in minutes if open_ms <= m["t"] < close_ms]
        if not regular:
            continue
        volume = math.fsum(m["v"] for m in regular)
        bars.append(
            {
                "t": et_midnight_ms(day),
                "o": regular[0]["o"],
                "h": max(m["h"] for m in regular),
                "l": min(m["l"] for m in regular),
                "c": regular[-1]["c"],
                "v": volume,
                "vw": math.fsum(m["vw"] * m["v"] for m in regular) / volume,
                "n": sum(m["n"] for m in regular),
            }
        )
    return bars


class _SdkDailyTape:
    """The SDK's ``RESTClient.list_aggs`` over a daily payload: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, daily: list[VendorBar]) -> None:
        self.daily = daily

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        assert (multiplier, timespan) == (1, "day")
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        for bar in self.daily:
            if start <= datetime.fromtimestamp(bar["t"] / 1000, ET).date() <= end:
                yield Agg.from_dict(bar)


def provider_rows(tape: Mapping[date, list[VendorBar]], window: list[date]) -> list[dict]:
    """What the study read before #3524: ``fetch_aggregates`` daily, adjusted, over the modeled daily payload."""
    service = PolygonClientService()
    service.client = _SdkDailyTape(modeled_daily(tape))  # type: ignore[assignment]
    return service.fetch_aggregates(
        ticker="SPY", multiplier=1, timespan="day", from_date=window[0].isoformat(), to_date=window[-1].isoformat(), adjusted=True
    )


def lake_rows(window: list[date], *, session: str = "regular", now_ms: int = NOW_MS) -> list[dict]:
    identity = cross_sectional_identity(
        ["SPY"], window[0], window[-1], timespan="day", multiplier=1, session_policy=session  # type: ignore[arg-type]
    )
    admission = admit_universe(identity, requester="comparison", now_ms=now_ms)
    assert not admission.excluded, admission.excluded
    return admission.members["SPY"].rows


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _session(row: dict) -> date:
    return datetime.fromtimestamp(row["timestamp"] / 1000, ET).date()


def _by_session(rows: list[dict]) -> dict[date, dict]:
    return {_session(r): r for r in rows}


def _targets(rows: list[dict]) -> dict[str, dict[date, float]]:
    """Each path's forward targets on its own full series, as the study computes them, read by session."""
    ordered = sorted(rows, key=lambda r: r["timestamp"])
    return {
        target: {_session(r): float(v) for r, v in zip(ordered, _compute_daily_forward_return(rows, target).to_numpy(), strict=True)}
        for target in TARGETS
    }


@dataclass
class Comparison:
    provider_bars: int
    lake_bars: int
    only_provider: int = 0
    only_lake: int = 0
    anchor_changed: int = 0
    anchor_same_utc_date: bool = True
    prices_changed: int = 0
    max_price_diff: float = 0.0
    volume_changed: int = 0
    max_volume_diff: float = 0.0
    max_return_diff: float = 0.0
    max_target_diff: dict[str, float] = field(default_factory=dict)
    target_validity_moved: int = 0

    @property
    def sessions_changed(self) -> int:
        return self.only_provider + self.only_lake

    @property
    def causes(self) -> list[str]:
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count decides.

        A different instant stamped on the same session is ``timestamp``. Every input that differs between the paths is
        ``data-quality``: a session on one side only, a price, a volume. ``precision`` is floating-point accumulation,
        and nothing here accumulates: a lake price is the exact decimal the writer encoded, converted to the nearest
        float once.
        """
        found: list[str] = []
        if self.anchor_changed:
            found.append("timestamp")
        if self.sessions_changed or self.prices_changed or self.volume_changed:
            found.append("data-quality")
        return found

    @property
    def quantization_only(self) -> bool:
        """Every price that moved moved by at most half of the lake's 1/10,000 grid: input quantization, not a new tape."""
        return self.prices_changed > 0 and self.max_price_diff <= HALF_GRID + 1e-9


def compare(provider: list[dict], lake: list[dict]) -> Comparison:
    by_p, by_l = _by_session(provider), _by_session(lake)
    shared = sorted(by_p.keys() & by_l.keys())
    result = Comparison(provider_bars=len(provider), lake_bars=len(lake))
    result.only_provider, result.only_lake = len(by_p.keys() - by_l.keys()), len(by_l.keys() - by_p.keys())
    for day in shared:
        p, q = by_p[day], by_l[day]
        if p["timestamp"] != q["timestamp"]:
            result.anchor_changed += 1
            utc_date = lambda ms: datetime.fromtimestamp(ms / 1000, ZoneInfo("UTC")).date()  # noqa: E731
            result.anchor_same_utc_date &= utc_date(p["timestamp"]) == utc_date(q["timestamp"])
        price_diff = max(abs(p[c] - q[c]) for c in ("open", "high", "low", "close"))
        if price_diff > 0:
            result.prices_changed += 1
            result.max_price_diff = max(result.max_price_diff, price_diff)
        volume_diff = abs(p["volume"] - q["volume"])
        if volume_diff > 0:
            result.volume_changed += 1
            result.max_volume_diff = max(result.max_volume_diff, volume_diff)
    returns_p, returns_l = _extract_daily_returns(provider), _extract_daily_returns(lake)
    common_days = sorted(set(returns_p.index) & set(returns_l.index))
    if common_days:
        result.max_return_diff = float(np.max(np.abs(returns_p[common_days].to_numpy() - returns_l[common_days].to_numpy())))
    targets_p, targets_l = _targets(provider), _targets(lake)
    for target in TARGETS:
        both = [d for d in shared if not (math.isnan(targets_p[target][d]) or math.isnan(targets_l[target][d]))]
        result.max_target_diff[target] = max((abs(targets_p[target][d] - targets_l[target][d]) for d in both), default=0.0)
        result.target_validity_moved += sum(1 for d in shared if math.isnan(targets_p[target][d]) != math.isnan(targets_l[target][d]))
    return result


def assert_target_edges(rows: list[dict]) -> None:
    """Warmup and the forward-label tail are kept, never filled: the 1-session targets lose their last row, the 5-session
    volatility target its first four (a full window needs five) and last five (they look five sessions ahead)."""
    ordered = [_session(r) for r in sorted(rows, key=lambda r: r["timestamp"])]
    targets = _targets(rows)
    directional = [targets["directional"][d] for d in ordered]
    volatility = [targets["volatility"][d] for d in ordered]
    assert math.isnan(directional[-1]) and not any(math.isnan(v) for v in directional[:-1])
    assert [i for i, v in enumerate(volatility) if not math.isnan(v)] == list(range(4, len(ordered) - 5))


@dataclass
class OptionSide:
    """What the stock close feeds on the option side: the ATM contract picked, the spot, and the IV solved at it."""

    atm_changed_days: int
    spot_changed_days: int
    max_iv_diff: float
    iv_days: int


def option_side(
    provider: list[dict], lake: list[dict], true_closes: Mapping[date, float], window: list[date], *, strike_step: float = 1.0
) -> OptionSide:
    """Run the real contract finder and IV builder over one chain with each path's rows, and compare the days they share."""
    def run(rows: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
        chain = OptionChain(
            "SPY", true_closes, rate=DEFAULT_RISK_FREE_RATE, expiries=(LOW_EXPIRY, HIGH_EXPIRY), strike_step=strike_step
        )
        inputs = member_inputs(chain, rows, window)
        return inputs.bracket, build_iv_history("SPY", inputs, RateTally())

    (contracts_p, iv_p), (contracts_l, iv_l) = run(provider), run(lake)
    atm = lambda frame: frame.set_index("date")["low_dte_atm_contract"]  # noqa: E731
    changed = int((atm(contracts_p) != atm(contracts_l)).sum())
    spot = iv_p.set_index("date")["stock_close"] != iv_l.set_index("date")["stock_close"]
    iv_both = pd.concat([iv_p.set_index("date")["iv_30d_atm"], iv_l.set_index("date")["iv_30d_atm"]], axis=1, keys=["p", "l"]).dropna()
    return OptionSide(
        atm_changed_days=changed,
        spot_changed_days=int(spot.sum()),
        max_iv_diff=float((iv_both["p"] - iv_both["l"]).abs().max()) if len(iv_both) else 0.0,
        iv_days=len(iv_both),
    )


def true_closes_of(tape: Mapping[date, list[VendorBar]]) -> dict[date, float]:
    """The regular-session close the options are priced off: what the chain's market saw at 16:00."""
    return {bar_day: bar["c"] for bar_day, bar in zip(sorted(tape), modeled_daily(tape), strict=True)}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    catalog = install_fake_catalog(monkeypatch)
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)
    with respx.mock:
        mock_launcher()
        yield catalog


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_under_a_modeled_regular_daily_a_dst_window_differs_only_in_the_instant_stamped_on_a_session(world):
    """Cause: ``timestamp``. Polygon stamps 00:00 ET; the study stamps the canonical session open. Same UTC date, so no output moves.

    The price and volume equality asserted here is by construction (the daily is ``modeled_daily``); what is proven is
    that the admitted lake path equals an independent fold, and that the anchor move changes no downstream output.
    """
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW)

    comparison = compare(provider, lake)
    options = option_side(provider, lake, true_closes_of(tape), DST_WINDOW)

    assert (comparison.provider_bars, comparison.lake_bars) == (len(DST_WINDOW),) * 2
    assert comparison.sessions_changed == 0 and comparison.prices_changed == 0 and comparison.volume_changed == 0
    assert comparison.anchor_changed == len(DST_WINDOW) and comparison.anchor_same_utc_date
    assert comparison.causes == ["timestamp"]
    assert comparison.max_return_diff <= 1e-9 and max(comparison.max_target_diff.values()) <= 1e-9
    assert comparison.target_validity_moved == 0, "the unavailable forward-label tail is the same last rows"
    assert_target_edges(lake)
    assert (options.atm_changed_days, options.spot_changed_days) == (0, 0)
    assert options.iv_days == len(DST_WINDOW) and options.max_iv_diff <= 1e-9, "the same spot prices the same IV, to the strict-float default"


def test_an_early_close_week_matches_a_modeled_regular_daily_and_the_holiday_has_no_row(world):
    tape = tape_for(HALF_DAY_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, HALF_DAY_WINDOW), lake_rows(HALF_DAY_WINDOW))

    assert comparison.sessions_changed == 0 and comparison.prices_changed == 0 and comparison.volume_changed == 0
    assert comparison.provider_bars == len(HALF_DAY_WINDOW) and date(2024, 7, 4) not in {_session(r) for r in lake_rows(HALF_DAY_WINDOW)}
    assert datetime.fromtimestamp(session_close_ms_utc(date(2024, 7, 3)) / 1000, ET).hour == 13, "the calendar closes the 3rd at 13:00"


def test_extended_hours_is_a_different_daily_bar_and_moves_the_spot_and_the_contract_pick(world):
    """Cause: ``data-quality`` (session scope). The lake's own daily rollup folds 04:00-20:00 ET; the modeled daily's close is the 16:00 close.

    The stock close sets the IV spot and the ATM strike. The numbers below are measured on the pinned synthetic tape: how much
    the policy moves the inputs, not how much it would move a live name.
    """
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW, session="extended")

    comparison = compare(provider, lake)
    options = option_side(provider, lake, true_closes_of(tape), DST_WINDOW)

    assert comparison.sessions_changed == 0, "the same sessions, folded from more minutes"
    assert comparison.prices_changed == len(DST_WINDOW) and comparison.max_price_diff > 0.1
    assert comparison.volume_changed == len(DST_WINDOW) and all(
        q["volume"] > p["volume"] for p, q in zip(provider, lake, strict=True)
    ), "the after-hours and pre-market tape adds volume"
    assert "data-quality" in comparison.causes
    assert comparison.max_return_diff > 1e-4, "the daily returns behind N_eff move"
    assert comparison.max_target_diff["directional"] > 1e-4, "and so does the target the IC is measured against"
    assert options.spot_changed_days == len(DST_WINDOW), "every day's IV spot is a different price"
    assert options.atm_changed_days >= 1, "and the ATM contract priced is not always the same strike"
    assert options.max_iv_diff > 1e-5, "so the solved IV moves, not only the target"


# ---------------------------------------------------------------------------
# The one real Polygon week in the repo: what the regular fold leaves out, and how it compares with the vendor's own daily
# ---------------------------------------------------------------------------

#: A week of real Polygon one-minute SPY bars (``adjusted=false``, extended hours included), captured 2026-05-20.
SPY_CAPTURE = Path(__file__).resolve().parents[1] / "fixtures" / "polygon_capture" / "spy_minute_2025-01-13_2025-01-17" / "bars.json"
SPY_WEEK = expected_sessions(date(2025, 1, 13), date(2025, 1, 17))
SPY_WEEK_NOW_MS = 1_738_540_800_000  # 2025-02-03: every session of the week is complete

#: The lake's regular-session fold of that week, as (open, high, low, close, volume) per session. The close is the 15:59
#: minute's close. Measured through the lake (vendor HTTP, ``ensure_data``, writer, admission, fold) and equal to a fold of
#: the capture computed independently from ``bars.json``.
SPY_REGULAR_FOLD = {
    date(2025, 1, 13): (575.77, 581.75, 575.35, 581.54, 36986595.0),
    date(2025, 1, 14): (584.36, 585.0, 578.35, 582.19, 40987960.0),
    date(2025, 1, 15): (590.325, 593.94, 589.195, 592.78, 42142340.0),
    date(2025, 1, 16): (594.175, 594.35, 590.93, 591.7, 30959922.0),
    date(2025, 1, 17): (596.96, 599.36, 595.61, 597.57, 46714655.0),
}
#: The minute the regular fold drops, the one stamped 16:00 ET (16:00:00 to 16:00:59), as (open, high, low, close,
#: volume), read from the same capture. It holds whatever printed in those sixty seconds: the closing cross, if it is
#: in Polygon's minute bars, and the first after-hours trades, which this capture does not separate. Its open is the
#: first trade it holds; its close is the last, and is mostly after-hours.
SPY_CLOSING_MINUTE = {
    date(2025, 1, 13): (581.52, 581.55, 581.17, 581.5, 124967.0),
    date(2025, 1, 14): (582.16, 582.61, 581.93, 582.6, 270425.0),
    date(2025, 1, 15): (592.74, 592.84, 592.62, 592.81, 500043.0),
    date(2025, 1, 16): (591.71, 591.74, 591.35, 591.64, 160774.0),
    date(2025, 1, 17): (597.53, 597.62, 597.4, 597.51, 433889.0),
}


def spy_capture_tape() -> dict[date, list[VendorBar]]:
    """The capture as the vendor served it, one list of minute aggregates per New York session."""
    tape: dict[date, list[VendorBar]] = {}
    for bar in json.loads(SPY_CAPTURE.read_text()):
        day = datetime.fromtimestamp(bar["timestamp"] / 1000, ET).date()
        tape.setdefault(day, []).append(
            {"t": bar["timestamp"], "o": bar["open"], "h": bar["high"], "l": bar["low"], "c": bar["close"], "v": bar["volume"], "vw": bar["vwap"], "n": bar["transactions"]}
        )
    return tape


def test_the_regular_fold_of_a_real_spy_week_stops_at_the_15_59_minute_and_leaves_out_the_minute_stamped_16_00(world):
    """Cause: ``data-quality`` (session boundary). This measures what the regular fold drops, on real minutes; the vendor's own
    daily bar for the same week is compared with the fold below.

    The lake reads ``[open, close)``, so a session's last minute is 15:59. The minute stamped 16:00 is not the auction
    print; it is the sixty seconds from 16:00:00, with whatever printed in them (the closing cross, if Polygon's minute
    bars keep it, and the first after-hours trades). Against the fold's close, at SPY about $580-$600:

    * its OPEN, the first trade it holds and the nearest proxy for the auction print this capture offers, sits 0.01 to
      0.04 away, under one basis point;
    * its CLOSE, the last trade it holds, sits 0.03 to 0.41 away (0.41 on 2025-01-14, when its open was 0.03 away), up to
      seven basis points. That is the minute's after-hours drift and not a price the auction paid;
    * its volume is 0.3 to 1.2 percent of the session's regular volume. This is the minute's volume, not the auction's.

    SPY had no split in 2025, so the split-adjusted tape the lake reads is the raw one. The stock close sets the IV spot
    and the ATM strike, so a daily close taken from the minute's close would be a different spot on 2025-01-14, by $0.41.
    """
    tape = spy_capture_tape()
    mock_vendor("SPY", tape, adjusted=tape)

    regular = {_session(r): r for r in lake_rows(SPY_WEEK, now_ms=SPY_WEEK_NOW_MS)}
    extended = {_session(r): r for r in lake_rows(SPY_WEEK, session="extended", now_ms=SPY_WEEK_NOW_MS)}

    assert sorted(regular) == SPY_WEEK == sorted(SPY_REGULAR_FOLD)
    for day in SPY_WEEK:
        row = regular[day]
        open_ms, close_ms = session_open_ms_utc(day), session_close_ms_utc(day)
        by_stamp = {m["t"]: m for m in tape[day]}
        # The pinned fold is the capture's own: first open, extremes, the 15:59 close, summed volume.
        inside = [m for m in tape[day] if open_ms <= m["t"] < close_ms]
        independent = (inside[0]["o"], max(m["h"] for m in inside), min(m["l"] for m in inside), inside[-1]["c"], math.fsum(m["v"] for m in inside))
        np.testing.assert_allclose(independent, SPY_REGULAR_FOLD[day], atol=1e-9, rtol=0, err_msg=f"{day} fold of the capture")
        np.testing.assert_allclose(
            (row["open"], row["high"], row["low"], row["close"], row["volume"]), SPY_REGULAR_FOLD[day], atol=1e-9, rtol=0, err_msg=f"{day} lake"
        )
        assert inside[-1]["t"] == close_ms - 60_000, "the last regular minute is 15:59"
        minute = by_stamp[close_ms]
        np.testing.assert_allclose(
            (minute["o"], minute["h"], minute["l"], minute["c"], minute["v"]), SPY_CLOSING_MINUTE[day], atol=1e-9, rtol=0, err_msg=f"{day} minute stamped 16:00"
        )
        assert row["close"] != minute["c"], "the lake's close is not the minute stamped 16:00's close"
        assert extended[day]["volume"] > row["volume"] + minute["v"], "and the extended fold holds it and the rest of the after-hours tape"
    # Per session: the minute's open and close against the fold's close, and the minute's share of the regular volume.
    open_moves = [SPY_CLOSING_MINUTE[day][0] - SPY_REGULAR_FOLD[day][3] for day in SPY_WEEK]
    close_moves = [SPY_CLOSING_MINUTE[day][3] - SPY_REGULAR_FOLD[day][3] for day in SPY_WEEK]
    volume_share = [SPY_CLOSING_MINUTE[day][4] / SPY_REGULAR_FOLD[day][4] for day in SPY_WEEK]
    np.testing.assert_allclose(open_moves, [-0.02, -0.03, -0.04, 0.01, -0.04], atol=1e-9, rtol=0)
    np.testing.assert_allclose(close_moves, [-0.04, 0.41, 0.03, -0.06, -0.06], atol=1e-9, rtol=0)
    assert max(abs(move) / SPY_REGULAR_FOLD[day][3] for move, day in zip(open_moves, SPY_WEEK, strict=True)) < 1e-4, "the open is under one basis point away"
    assert max(abs(move) / SPY_REGULAR_FOLD[day][3] for move, day in zip(close_moves, SPY_WEEK, strict=True)) < 7.1e-4, "the close up to seven"
    assert min(volume_share) > 0.003 and max(volume_share) < 0.012


#: Golden fixture ``SPY-DAILY-001``: the vendor's daily aggregates for the same week, recorded byte for byte (``input.json``),
#: and the measurement of the fold against them (``output.json``, written by the fixture's ``regenerate.py``).
SPY_DAILY_FIXTURE = "SPY-DAILY-001"
#: The fold's close less the vendor's, per session, as measured: exact on 2 of 5 days, up to $0.15 on the rest.
SPY_CLOSE_DIFFERENCE = [0.15, 0.0, 0.0, 0.06, -0.01]
#: The same difference had the minute stamped 16:00 been folded in (its close is the day's last): exact on 1 of 5, up to $0.41.
SPY_CLOSE_DIFFERENCE_WITH_16_00 = [0.11, 0.41, 0.03, 0.0, -0.07]
#: The fold's volume over the vendor's daily volume, per session, as measured: 71.5 to 84.7 percent.
SPY_VOLUME_SHARE = [0.7742631053844186, 0.8472511440216729, 0.7408318925503926, 0.7151391977143576, 0.8438207031326346]
#: Every top-level key of the recorded answer; a payload with another field (a key, a cursor) is not the one pinned.
VENDOR_DAILY_KEYS = {"ticker", "queryCount", "resultsCount", "adjusted", "results", "status", "request_id", "count"}


def test_the_regular_fold_of_a_real_spy_week_equals_the_vendors_real_daily_open_high_and_low(world):
    """Vendor data, 2025-01-13 to 17, ``adjusted=false``, recorded 2026-10-24 (golden ``SPY-DAILY-001``) against the study's
    daily fold of the repo's SPY minute capture of the same week, through the real lake path.

    * **Open, high, low: exact on all five days.** The regular session is ``[09:30, 16:00)``, so the fold leaves out the
      minute stamped 16:00.
    * **Close: exact on two days, up to $0.15 off on the others.** The vendor's close is the official print. The study's is
      the last regular minute's close, the 15:59 minute: the instant the option minute bars price at, and so the spot the IV
      solve needs. Folding the 16:00 minute in would be worse: exact on one day and up to $0.41 off.
    * **Volume: the fold holds 71.5 to 84.7 percent of the vendor's.** The vendor's volume counts trades its minute aggregates
      omit. The study reads no stock volume, so this changes no output.

    The differences are pinned as measured: the close at ``atol=1e-9`` (a difference of two prices on the 1/10,000 grid is
    exact to float noise), the share at ``atol=1e-12`` (one division of two exact sums).
    """
    vendor_text = registry.resolve(SPY_DAILY_FIXTURE, "input").read_text(encoding="utf-8")
    vendor = json.loads(vendor_text)
    recorded = json.loads(registry.resolve(SPY_DAILY_FIXTURE, "output").read_text(encoding="utf-8"))["sessions"]
    assert set(vendor) == VENDOR_DAILY_KEYS and vendor["adjusted"] is False and vendor["status"] == "OK"
    assert "apikey" not in vendor_text.lower() and "api_key" not in vendor_text.lower(), "a recorded answer holds no key"
    tape = spy_capture_tape()
    mock_vendor("SPY", tape, adjusted=tape)

    study = {_session(r): r for r in lake_rows(SPY_WEEK, now_ms=SPY_WEEK_NOW_MS)}

    sessions = [datetime.fromtimestamp(daily["t"] / 1000, ET).date() for daily in vendor["results"]]
    assert sessions == SPY_WEEK == sorted(study) == sorted(SPY_REGULAR_FOLD)
    for day, daily in zip(sessions, vendor["results"], strict=True):
        row = study[day]
        np.testing.assert_allclose(
            (row["open"], row["high"], row["low"]), (daily["o"], daily["h"], daily["l"]), atol=0.0, rtol=0.0, err_msg=f"{day} open, high, low"
        )
        np.testing.assert_allclose(
            (row["open"], row["high"], row["low"], row["close"], row["volume"]), SPY_REGULAR_FOLD[day], atol=1e-9, rtol=0, err_msg=f"{day} fold"
        )
        assert recorded[day.isoformat()]["vendor"] == {
            "open": daily["o"], "high": daily["h"], "low": daily["l"], "close": daily["c"], "volume": daily["v"]
        }, "the recorded measurement was made from this payload"
    close_gap = [study[day]["close"] - daily["c"] for day, daily in zip(sessions, vendor["results"], strict=True)]
    np.testing.assert_allclose(close_gap, SPY_CLOSE_DIFFERENCE, atol=1e-9, rtol=0, err_msg="the fold's close less the vendor's")
    assert sum(abs(gap) < 1e-9 for gap in close_gap) == 2 and max(abs(gap) for gap in close_gap) < 0.15 + 1e-9
    volume_share = [study[day]["volume"] / daily["v"] for day, daily in zip(sessions, vendor["results"], strict=True)]
    np.testing.assert_allclose(volume_share, SPY_VOLUME_SHARE, atol=1e-12, rtol=0, err_msg="the fold's volume over the vendor's")
    assert min(volume_share) > 0.715 and max(volume_share) < 0.848

    with_16_00 = [
        float(recorded[day.isoformat()]["with_the_minute_stamped_16_00"]["close_difference_fold_minus_vendor"]) for day in sessions
    ]
    np.testing.assert_allclose(with_16_00, SPY_CLOSE_DIFFERENCE_WITH_16_00, atol=1e-9, rtol=0, err_msg="had the 16:00 minute been folded in")
    np.testing.assert_allclose(
        [recorded[day.isoformat()]["with_the_minute_stamped_16_00"]["close"] for day in sessions],
        [SPY_CLOSING_MINUTE[day][3] for day in sessions],
        atol=0.0, rtol=0.0, err_msg="the recorded close is the capture's minute stamped 16:00",
    )
    assert sum(abs(gap) < 1e-9 for gap in with_16_00) < sum(abs(gap) < 1e-9 for gap in close_gap), "fewer days exact"
    assert max(abs(gap) for gap in with_16_00) > max(abs(gap) for gap in close_gap), "and a larger miss"
    assert [recorded[day.isoformat()]["close_difference_fold_minus_vendor"] for day in sessions] == ["0.15", "0.00", "0.00", "0.06", "-0.01"]


#: What the lake's price grid moves, measured on this pinned payload (seed 11, the DST window, 11 sessions) at three
#: price levels, to full precision: the largest absolute difference between the two paths' values at a shared session.
#: Prices carry five or six decimals, finer than the grid. The movement grows as the price falls: a rounding of at most
#: 5e-5 is a larger share of a day's change on a $2 name than on a $150 one. ``iv`` is the largest solved-IV difference
#: over the days the option chain could solve (11, 10 and 3 of 11), pinned only where there are enough of them to mean
#: something; a $2 underlying leaves 3. Re-measure (print the ``Comparison`` and ``OptionSide`` fields per case) only
#: when the pinned tape or the lake's encoding intentionally changes; never to make a failing test pass.
GRID_MEASURED = {
    "price_150": (
        150.0, 5, 1.0,
        {"returns": 4.758706811935909e-07, "directional": 4.799286627728272e-07, "abs_return": 4.799286627728272e-07,
         "volatility": 9.93863396220318e-07, "iv": 1.2319436593077882e-06},
    ),
    "price_12": (
        12.0, 5, 0.5,
        {"returns": 5.405870201924401e-06, "directional": 5.3492904448400325e-06, "abs_return": 5.3492904448400325e-06,
         "volatility": 3.813195684010395e-05, "iv": 5.2392330460726555e-06},
    ),
    "price_2": (
        2.0, 6, None,
        {"returns": 3.386216610090642e-05, "directional": 3.456115803534415e-05, "abs_return": 3.456115803534415e-05,
         "volatility": 4.083402936982117e-04},
    ),
}

EPS = float(np.finfo(np.float64).eps)
#: A daily return is a close over the day before: the ratio rounds once, to within ``eps / 2`` of a value near 1, and the
#: subtraction or the log adds at most as much again. So a return carries an absolute error of about ``eps`` whatever its
#: size (it does not shrink with the return), rounded up here to ``2 * eps``.
RETURN_ABS_ERR = 2 * EPS
#: How far each output can move when every return moves by ``RETURN_ABS_ERR``: the returns and the one-session targets
#: are returns (or their absolute value); the volatility target is ``sqrt(252)`` times the sample standard deviation of
#: five returns, which is 1-Lipschitz in the returns' L2 norm over ``sqrt(5 - 1)``, so ``sqrt(252) * sqrt(5) / 2``.
LIPSCHITZ = {"returns": 1.0, "directional": 1.0, "abs_return": 1.0, "volatility": math.sqrt(252) * math.sqrt(5) / 2}
#: The IV is the root of ``BS(sigma) = premium``. The premium is the difference of two terms of about ``S / 2`` each, so
#: it carries a rounding of about ``4 * eps * S``; at the money the vega is ``S * phi(0) * sqrt(T)``, so the root moves
#: by ``4 * eps / (phi(0) * sqrt(T))`` whatever ``S`` is. ``T`` is the nearest expiry on any pinned day (14 days). The
#: Newton solve stops at a price residual of 1e-7, but its iterates are the same on every platform to within a few ulps, so
#: it can stop on another step only if a residual sits within an ulp of 1e-7: not a case this tolerance covers, and not
#: one that has occurred.
IV_FLOAT_ERR = 4 * EPS / (math.sqrt(14 / 365) / math.sqrt(2 * math.pi))


def float_atol(name: str) -> float:
    """The float accumulation error of a pinned movement, class ``precision``: the difference of two paths' values.

    Measured by re-rounding the same tape (every price times 16 factors that are not powers of two, both paths alike,
    so no mathematical result changes; nine one-ulp nudges for the IV): the pinned movements stayed within 3.5e-15 of
    their pinned values, about 4 times below the volatility bound and 8 times below the IV bound.
    """
    return 2 * (IV_FLOAT_ERR if name == "iv" else LIPSCHITZ[name] * RETURN_ABS_ERR)


def worst_case_volatility_target_move(lowest_close: float) -> float:
    """A close moves by at most ``HALF_GRID``, so a log return by at most 2*HALF_GRID/P. The target is sqrt(252) times the
    sample standard deviation of five returns, which is 1-Lipschitz in the returns' L2 norm over sqrt(5-1), so it moves
    by at most sqrt(252) * sqrt(5) * 2*HALF_GRID / (2 * P) = sqrt(1260) * HALF_GRID / P."""
    return math.sqrt(1260) * HALF_GRID / lowest_close


@pytest.mark.parametrize(("start_price", "decimals", "strike_step", "measured"), GRID_MEASURED.values(), ids=GRID_MEASURED)
def test_prices_finer_than_the_lake_grid_move_each_output_by_its_measured_amount_which_depends_on_the_price(
    world, start_price, decimals, strike_step, measured
):
    """Cause: ``data-quality`` (input quantization). The lake rounds every minute half-up to 1/10,000
    (``lean_writer.to_deci_cent``); the provider path does not. That changes the input data, so it is not ``precision`` and no
    float tolerance covers it.

    Each recorded output is pinned at its measured movement, within ``float_atol`` and with ``rtol=0`` because the quantity
    compared is itself a difference. The table above is what the grid costs: the daily returns move by about 5e-7 on a
    $150 name, 5e-6 on a $12 name and 3e-5 on a $2 name; the five-day volatility target by 1e-6, 4e-5 and 4e-4; the solved IV
    by 1e-6 and 5e-6 where it can be solved. ``worst_case_volatility_target_move`` is the bound the measured movement must sit
    under whatever the tape.
    """
    tape = tape_for(DST_WINDOW, start_price=start_price, decimals=decimals)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW)

    comparison = compare(provider, lake)

    assert comparison.sessions_changed == 0 and comparison.volume_changed == 0
    assert comparison.quantization_only, "every price that moved moved by at most half a ten-thousandth"
    assert comparison.causes == ["timestamp", "data-quality"]
    assert comparison.target_validity_moved == 0
    moved = {"returns": comparison.max_return_diff, **comparison.max_target_diff}
    for name in ("returns", *TARGETS):
        np.testing.assert_allclose(moved[name], measured[name], atol=float_atol(name), rtol=0, err_msg=name)
        assert moved[name] > 0, f"{name}: the divergence is real, so the output does move"
    assert moved["volatility"] <= worst_case_volatility_target_move(min(r["close"] for r in lake))
    if strike_step is not None:
        options = option_side(provider, lake, true_closes_of(tape), DST_WINDOW, strike_step=strike_step)
        assert options.iv_days >= 10 and options.atm_changed_days == 0
        np.testing.assert_allclose(options.max_iv_diff, measured["iv"], atol=float_atol("iv"), rtol=0, err_msg="iv")
        assert options.max_iv_diff > 0


def test_fractional_share_volume_sums_to_the_same_number_on_both_paths(world):
    tape = tape_for(DST_WINDOW, fractional_volume=True)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW)

    comparison = compare(provider, lake)

    np.testing.assert_allclose([r["volume"] for r in lake], [r["volume"] for r in provider], atol=1e-6, rtol=0)
    assert comparison.max_volume_diff <= 1e-6, "an exactly rounded sum against an exact decimal sum"
    assert any(r["volume"] != int(r["volume"]) for r in lake)


def test_a_session_the_provider_path_dropped_in_silence_now_excludes_the_member_with_a_reason(world):
    """Cause: ``data-quality`` (a gap). A shorter series used to run; the member is now excluded and the session named."""
    tape = tape_for(DST_WINDOW)
    gap_day = DST_WINDOW[4]
    holed = {day: bars for day, bars in tape.items() if day != gap_day}
    mock_vendor("SPY", holed, adjusted=holed)

    provider = provider_rows(holed, DST_WINDOW)
    identity = cross_sectional_identity(["SPY"], DST_WINDOW[0], DST_WINDOW[-1], timespan="day", multiplier=1, session_policy="regular")
    admission = admit_universe(identity, requester="comparison", now_ms=NOW_MS)

    assert len(provider) == len(DST_WINDOW) - 1, "before: the study quietly ran on one session fewer"
    assert admission.members == {} and admission.excluded["SPY"].code == "data_unavailable_from_provider"
    assert gap_day.isoformat() in admission.excluded["SPY"].reason, "now: the missing session is named"


def test_a_member_listed_inside_the_window_ran_on_its_shorter_history_before_and_is_now_excluded(world):
    """Cause: ``data-quality`` (coverage). The lake gate does not run a study on a window it cannot fully admit.

    The sessions before the listing date are not in the provider's tape, so the old path simply started later. The lake
    path asks for them, is told there is nothing, and excludes the member with those sessions named. The owner chose this
    (2026-10-24, #3524): a member missing a session, or listed mid-window, is excluded, and the page shows it.
    """
    tape = tape_for(DST_WINDOW)
    listed = DST_WINDOW[3]
    late = {day: bars for day, bars in tape.items() if day >= listed}
    mock_vendor("SPY", late, adjusted=late)
    identity = cross_sectional_identity(["SPY"], DST_WINDOW[0], DST_WINDOW[-1], timespan="day", multiplier=1, session_policy="regular")

    provider = provider_rows(late, DST_WINDOW)
    admission = admit_universe(identity, requester="comparison", now_ms=NOW_MS)

    assert len(provider) == len(DST_WINDOW) - 3, "before: the study ran on the sessions after the listing"
    assert admission.members == {}
    excluded = admission.excluded["SPY"]
    assert excluded.code == "data_unavailable_from_provider" and DST_WINDOW[0].isoformat() in excluded.reason
    assert excluded.refusal["sessions_total"] == 3 and excluded.refusal["coverage"]["window"]["failed"] == 3
    assert compare(provider, lake_rows(DST_WINDOW[3:])).sessions_changed == 0, "ask for the listed span and the paths agree"


def test_the_forming_session_is_in_the_provider_path_and_refused_by_the_lake(world):
    """Cause: ``data-quality`` (a partial session). The provider path ended on today's partial bar; the lake holds completed sessions only."""
    tape = tape_for(DST_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)
    mid_last_session = int(datetime.combine(DST_WINDOW[-1], datetime.min.time().replace(hour=12), tzinfo=ET).timestamp() * 1000)
    identity = cross_sectional_identity(["SPY"], DST_WINDOW[0], DST_WINDOW[-1], timespan="day", multiplier=1, session_policy="regular")

    admission = admit_universe(identity, requester="comparison", now_ms=mid_last_session)
    complete = lake_rows(DST_WINDOW[:-1], now_ms=mid_last_session)

    assert len(provider_rows(tape, DST_WINDOW)) == len(DST_WINDOW), "before: the forming session was a row like any other"
    assert admission.excluded["SPY"].code == "window_not_complete" and DST_WINDOW[-1].isoformat() in admission.excluded["SPY"].reason
    comparison = compare(provider_rows(tape, DST_WINDOW[:-1]), complete)
    assert comparison.sessions_changed == 0 and comparison.prices_changed == 0, "ending one session earlier, the two paths agree"


def test_a_split_announced_after_capture_is_not_in_a_held_window_until_the_lake_next_captures_the_symbol(world):
    """Cause: ``data-quality`` (adjustment as-of). The provider adjusts to *now*; the lake to its latest capture.

    A held window is read as captured: a split that lands afterwards is invisible to it until a later capture for the
    symbol refreshes the adjusted history. The provider path always saw the new basis.
    """
    window = expected_sessions(date(2024, 6, 3), date(2024, 6, 14))
    held, later = window[:6], window[6:]
    split_day = later[0]
    raw = vendor_history(window, seed=11, start_price=1500.0, regular_keep=0.9, extended_keep=0.4)
    mock_vendor("SPY", raw, adjusted=raw)
    lake_rows(held)  # the lake captures the first six sessions before the split exists
    respx.mock.reset()
    mock_launcher()
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    calls = VendorCalls()
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split], calls=calls)
    provider_after_split = provider_rows(adjusted, held)

    stale = compare(provider_after_split, lake_rows(held))
    lake_rows(later)  # any later capture of the symbol rebuilds its stale adjusted days
    refreshed = compare(provider_after_split, lake_rows(held))

    assert "data-quality" in stale.causes and stale.prices_changed == len(held)
    assert stale.max_price_diff > 100, "the held days still carry the pre-split basis"
    assert refreshed.prices_changed == 0 and refreshed.sessions_changed == 0
    assert calls.count > 0
