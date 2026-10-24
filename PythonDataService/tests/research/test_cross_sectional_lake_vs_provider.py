"""Cross-sectional stock bars: the old provider path against the admitted lake path, on one pinned vendor payload (#3524; R14, T08, D02).

Slice 34 of the UX redesign moves the study's stock bars from ``PolygonClientService.fetch_aggregates(..., "day")`` to
the lake. This module is the receipt for what that move changes, case by case. Both paths read the *same* pinned
minute tape:

* the **provider path** is the real ``fetch_aggregates`` (the code the study used), fed ``Agg`` objects from the
  Polygon SDK's own parser, over a daily payload the test builds from that tape (``vendor_daily``);
* the **lake path** is the real admission — vendor HTTP, ``ensure_data``, the lake writer, committed-byte admission,
  the reader — and the study's daily fold.

Each case records changed timestamps, prices, precision, counts and derived outputs, and names the cause in the
reconcile taxonomy (``timestamp``, ``precision``, ``off-by-one``, ``data-quality``). The derived outputs are the three
things a stock bar feeds in this study: the daily returns behind the N_eff correlation, the forward targets the IC is
measured against, and — through the stock close — the ATM contract pick and the IV spot. A case that moves nothing
says so by asserting zero. Unexplained drift fails here; an *intentional* divergence is the owner's decision (D02).

What is not provable offline, and is carried to the owner as an assumption: that Polygon's own daily aggregate equals
the regular-session fold of its minute aggregates. ``vendor_daily`` models the daily bar that way, because the repo
treats the daily close as the regular-session close (``dataset_service.fetch_rth_closes``) and the census marks the rest
unverified. Differences that come from the *vendor's* construction of a daily bar (the official closing cross, odd-lot
rules) are outside this comparison and unverified against live data.

The IV and contract-pick comparison runs on the first fortnight of March 2024, the span over which the monthly expiry
bracket still holds 30 days (see ``tests/research/options/test_iv_builder_provider_inputs.py``). The study's IC itself
cannot be compared end to end for that reason: on master, no chain passes the IV diagnostics over a window long enough
to compute one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
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
from app.research.options.contract_finder import find_bracket_contracts
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_input_ledger import OptionInputLedger
from app.research.options_runner import _compute_daily_forward_return
from app.services.polygon_client import PolygonClientService
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.option_chain import OptionChain
from tests._helpers.pinned_vendor import VendorBar, VendorCalls, mock_vendor, scaled, vendor_history

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01

#: 2024-03-10 is the spring-forward Sunday. The first fortnight keeps the monthly expiry bracket valid for the IV build.
DST_WINDOW = expected_sessions(date(2024, 3, 1), date(2024, 3, 15))
#: 2024-07-03 is an early close (13:00 ET) and 2024-07-04 a holiday.
HALF_DAY_WINDOW = expected_sessions(date(2024, 7, 1), date(2024, 7, 12))
LOW_EXPIRY, HIGH_EXPIRY = date(2024, 3, 29), date(2024, 4, 15)

TARGETS = ("directional", "volatility", "abs_return")
#: A decimal place of precision beyond the lake grid is half of 1/10,000.
HALF_GRID = 5e-5


def tape_for(window: list[date], **options: object) -> dict[date, list[VendorBar]]:
    return vendor_history(window, seed=11, start_price=150.0, **{"regular_keep": 0.9, "extended_keep": 0.4, **options})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


def vendor_daily(tape: Mapping[date, list[VendorBar]]) -> list[VendorBar]:
    """An independent stand-in for the vendor's daily aggregate of the same trades: the regular session, stamped 00:00 ET.

    First/high/low/last/sum over the minutes the calendar puts in the scheduled session (an early close ends it at
    13:00), the volume-weighted average of the minutes' VWAPs, summed trade counts. ``math.fsum`` so the sum is exactly
    rounded, as the lake's decimal sum is.
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
    """The SDK's ``RESTClient.list_aggs`` over a pinned daily payload: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, daily: list[VendorBar]) -> None:
        self.daily = daily

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        assert (multiplier, timespan) == (1, "day")
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        for bar in self.daily:
            if start <= datetime.fromtimestamp(bar["t"] / 1000, ET).date() <= end:
                yield Agg.from_dict(bar)


def provider_rows(tape: Mapping[date, list[VendorBar]], window: list[date]) -> list[dict]:
    """What the study read before #3524: ``fetch_aggregates`` daily, adjusted, for the window."""
    service = PolygonClientService()
    service.client = _SdkDailyTape(vendor_daily(tape))  # type: ignore[assignment]
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
        """The reconcile taxonomy's reading of what moved. Rules, not judgement: a count or a magnitude decides."""
        found: list[str] = []
        if self.anchor_changed:
            found.append("timestamp")  # same session, a different instant stamped on it
        if self.sessions_changed:
            found.append("data-quality")  # a session present on one side only: the input itself differs
        if self.prices_changed and self.max_price_diff <= HALF_GRID + 1e-9:
            found.append("precision")  # at most half of the lake's 1/10,000 grid
        elif self.prices_changed:
            found.append("data-quality")
        if self.volume_changed:
            found.append("data-quality")
        return list(dict.fromkeys(found))


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


def option_side(provider: list[dict], lake: list[dict], true_closes: Mapping[date, float], window: list[date]) -> OptionSide:
    """Run the real contract finder and IV builder over one chain with each path's rows, and compare the days they share."""
    def run(rows: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
        chain = OptionChain("SPY", true_closes, rate=DEFAULT_RISK_FREE_RATE, expiries=(LOW_EXPIRY, HIGH_EXPIRY))
        service = PolygonClientService()
        service.client = chain  # type: ignore[assignment]
        args = ("SPY", window[0].isoformat(), window[-1].isoformat(), service, rows)
        return find_bracket_contracts(*args, OptionInputLedger()), build_iv_history(*args, OptionInputLedger())

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
    return {bar_day: bar["c"] for bar_day, bar in zip(sorted(tape), vendor_daily(tape), strict=True)}


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


def test_regular_hours_over_a_dst_week_differ_only_in_the_instant_stamped_on_a_session(world):
    """Cause: ``timestamp``. Polygon stamps 00:00 ET; the study stamps the canonical session open. Same UTC date, so no output moves."""
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


def test_an_early_close_week_matches_under_regular_hours_and_the_holiday_has_no_row(world):
    tape = tape_for(HALF_DAY_WINDOW)
    mock_vendor("SPY", tape, adjusted=tape)

    comparison = compare(provider_rows(tape, HALF_DAY_WINDOW), lake_rows(HALF_DAY_WINDOW))

    assert comparison.sessions_changed == 0 and comparison.prices_changed == 0 and comparison.volume_changed == 0
    assert comparison.provider_bars == len(HALF_DAY_WINDOW) and date(2024, 7, 4) not in {_session(r) for r in lake_rows(HALF_DAY_WINDOW)}
    assert datetime.fromtimestamp(session_close_ms_utc(date(2024, 7, 3)) / 1000, ET).hour == 13, "the calendar closes the 3rd at 13:00"


def test_extended_hours_is_a_different_daily_bar_and_moves_the_spot_and_the_contract_pick(world):
    """Cause: ``data-quality`` (session scope). The lake's own daily rollup folds 04:00-20:00 ET; Polygon's close is the 16:00 close. D02.

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


def test_prices_finer_than_the_lake_grid_move_by_at_most_half_a_ten_thousandth(world):
    """Cause: ``precision``. The lake rounds every minute half-up to 1/10,000 (``lean_writer.to_deci_cent``); the provider path does not."""
    tape = tape_for(DST_WINDOW, decimals=5)
    mock_vendor("SPY", tape, adjusted=tape)
    provider, lake = provider_rows(tape, DST_WINDOW), lake_rows(DST_WINDOW)

    comparison = compare(provider, lake)
    options = option_side(provider, lake, true_closes_of(tape), DST_WINDOW)

    assert comparison.sessions_changed == 0
    assert comparison.prices_changed > 0 and comparison.max_price_diff <= HALF_GRID + 1e-9
    assert comparison.causes == ["timestamp", "precision"]
    # A $150 price moved by 5e-5 moves a daily return by about 7e-7 and a 5-day annualized volatility by far less than its own noise.
    assert comparison.max_return_diff <= 2e-6
    assert max(comparison.max_target_diff.values()) <= 1e-4
    assert options.max_iv_diff <= 1e-5, "and the solved IV by less than a hundredth of a volatility point"


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
    """Cause: ``data-quality`` (coverage). The lake gate does not run a study on a window it cannot fully admit. D02.

    The sessions before the listing date are not in the provider's tape, so the old path simply started later. The lake
    path asks for them, is told there is nothing, and excludes the member with those sessions named. Whether a member
    should instead be evaluated on the part of the window it was listed for is the owner's decision.
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
