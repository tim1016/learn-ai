"""``build_iv_history`` over a window longer than the first session's bracket (#3528, #3540).

``find_bracket_contracts`` used to look the two expiries around 30 days up once per month and reuse them for the
whole month, so ``k`` days in both DTEs had fallen by ``k``. ``variance_interpolated_iv`` (#3484) raises when 30 days
is outside ``[dte_low, dte_high]``, so any session more than ``dte_high - 30`` days after its month's first session
raised, and the whole ticker was lost. A month still costs one expiry list, read as of its first session; a date
keeps the previous date's pair while it still brackets 30 days and its near expiry is 7 or more days out, picks afresh
when it does not, and reads the listing as of that date when the fresh pick finds no near or no far expiry in a list
taken earlier (an expiry listed after the first session is invisible to it).

The chain is a pinned listed weekly-Friday chain priced with a known volatility, held in memory as the contract finder's
``ChainSource`` and as each contract's price on each session (the underlying's own bars are the ``stock_bars`` argument).
The real contract finder, IV builder and solver run over it, so what comes back is checked against the number that
generated the prices. The lake side of the same selection is tested in ``test_option_inputs_lake.py``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.research.options import contract_finder, iv_builder
from app.research.options.contract_finder import find_bracket_contracts
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_inputs import ListedOption, RateTally
from app.services.bs_greeks import bs_european_price
from app.services.fred_service import RATE_SOURCE_FRED, RateResolution
from tests._helpers.option_chain import member_inputs

RATE = 0.043

#: Option prices are rounded to 1e-4, so a price is off by at most 5e-5; the thinnest vega among this chain's 7-45 day
#: contracts is 4.6 per unit of volatility (a 25-delta wing late in the month; ATM about 12). The implied volatility is
#: therefore recovered to within 5e-5 / 4.6 = 1.09e-5, which is the tolerance: the derived worst case, not a round
#: number. The largest error measured on this payload is 4.8e-6 (put wing); the solver's own tolerance adds nothing.
IV_ATOL = 1.1e-5

FRIDAY = 4
MAY_SESSIONS = [
    date(2024, 5, d) for d in (1, 2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 28, 29, 30, 31)
]  # Memorial Day (May 27) is closed
JUNE_SESSIONS = [
    date(2024, 6, d) for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 20, 21, 24, 25, 26, 27, 28)
]  # Juneteenth (June 19) is closed


def fridays(first: date, last: date) -> tuple[date, ...]:
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return tuple(day for day in days if day.weekday() == FRIDAY)


def option_ticker(expiry: date, kind: str, strike: float) -> str:
    return f"O:SPY{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}{round(strike * 1000):08d}"


def parse_option_ticker(ticker: str) -> tuple[date, str, float]:
    tail = ticker[-15:]
    return (
        date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6])),
        "call" if tail[6] == "C" else "put",
        int(tail[7:]) / 1000.0,
    )


def noon_utc_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)


def flat_sigma(day: date, dte: int) -> float:
    """One volatility per session, the same at every expiry: any pair that brackets 30 days recovers it."""
    return 0.20 + 0.002 * day.day


def sloped_sigma(day: date, dte: int) -> float:
    """A volatility that rises 0.1 vol point per day to expiry: the 30-day number depends on which pair is used."""
    return 0.20 + 0.001 * dte


@dataclass
class ChainClient:
    """A pinned chain: the contract finder's ``ChainSource`` and each contract's price on each session."""

    expiries: tuple[date, ...]
    spots: dict[date, float]
    sigma: Callable[[date, int], float] = flat_sigma
    skew: float = 0.0  # volatility lost per unit of ln(strike / spot): lower strikes carry the higher volatility
    listed_on: dict[date, date] = field(default_factory=dict)  # an expiry is visible as of its listing date onward
    listing_requests: list[date] = field(default_factory=list)
    underlying: str = "SPY"

    def listing_as_of(self, day: date) -> tuple[ListedOption, ...]:
        self.listing_requests.append(day)
        listed = [
            ListedOption(option_ticker(expiry, kind, strike), kind, float(strike), expiry)
            for expiry in sorted(self.expiries)
            if expiry >= day and self.listed_on.get(expiry, date.min) <= day
            for strike in np.arange(100.0, 201.0, 1.0)
            for kind in ("call", "put")
        ]
        return tuple(sorted(listed, key=lambda option: option.ticker))

    def daily_price(self, ticker: str, day: date) -> float | None:
        expiry, kind, strike = parse_option_ticker(ticker)
        if day not in self.spots or day >= expiry:
            return None
        spot = self.spots[day]
        dte = (expiry - day).days
        vol = self.sigma(day, dte) - self.skew * np.log(strike / spot)
        price = bs_european_price(spot, strike, dte / 365.0, RATE, vol, kind == "call")
        return None if price < 0.05 else round(price, 4)


@pytest.fixture(autouse=True)
def fixed_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED is not reachable from a test: every session is solved at the one rate the chain is priced with."""
    monkeypatch.setattr(iv_builder, "resolve_risk_free_rate", lambda **kwargs: RateResolution(RATE, RATE_SOURCE_FRED))


def stock_rows(sessions: list[date]) -> list[dict[str, Any]]:
    return [{"timestamp": noon_utc_ms(day), "close": round(150.0 + 0.37 * i, 4)} for i, day in enumerate(sessions)]


def build(sessions: list[date], client: ChainClient) -> Any:
    return build_iv_history("SPY", member_inputs(client, stock_rows(sessions), sessions, volume=1000), RateTally())


def chain(sessions: list[date], **options: Any) -> ChainClient:
    spots = {row_day: row["close"] for row_day, row in zip(sessions, stock_rows(sessions), strict=True)}
    expiries = options.pop("expiries", fridays(date(2024, 4, 1), date(2024, 9, 1)))
    return ChainClient(expiries=expiries, spots=spots, **options)


def sessions_of(frame: Any, sessions: list[date]) -> Any:
    """The rows for real sessions: ``build_iv_history`` pads the frame to business days, holidays included."""
    return frame[frame["date"].isin([day.isoformat() for day in sessions])].reset_index(drop=True)


def first_uses(bracket: pd.DataFrame) -> list[date]:
    """The session each expiry is first in a pair: when its contracts are read, once, and kept for the run."""
    seen: dict[date, date] = {}
    for row in bracket.itertuples():
        if pd.isna(row.low_dte):
            continue
        day = date.fromisoformat(row.date)
        for dte in (row.low_dte, row.high_dte):
            seen.setdefault(day + timedelta(days=int(dte)), day)
    return sorted(seen.values())


def list_reads(client: ChainClient, bracket: pd.DataFrame) -> list[date]:
    """The sessions a month's expiry list was read as of: every read of the chain, less the one read each expiry's contracts take."""
    left = Counter(client.listing_requests)
    left.subtract(Counter(first_uses(bracket)))
    assert min(left.values()) >= 0, "every expiry's contracts were read as of its first use"
    return sorted(day for day, count in left.items() for _ in range(count))


# ---------------------------------------------------------------------------
# The regression: a window longer than the first session's bracket
# ---------------------------------------------------------------------------


def test_a_window_past_the_first_sessions_bracket_edge_builds_instead_of_raising():
    """May 1's bracket is (May 31, Jun 7): 30 days leaves it after May 8, the old month-long reuse raised there."""
    client = chain(MAY_SESSIONS)

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    assert list(frame["date"]) == [day.isoformat() for day in MAY_SESSIONS]
    assert (frame["dte_low"] <= 30).all() and (frame["dte_high"] >= 30).all(), "30 days is inside every day's bracket"
    assert (frame["dte_low"] >= 7).all(), "no near expiry under 7 days out"
    assert (frame["iv_quality"] != "missing").all(), "no session is lost"


def test_every_session_recovers_the_volatility_the_chain_was_priced_with():
    """The chain's volatility is the same at every expiry, so any pair that brackets 30 days must return it."""
    client = chain(MAY_SESSIONS)

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    expected = [flat_sigma(day, 30) for day in MAY_SESSIONS]
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        np.testing.assert_allclose(frame[column].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0, err_msg=column)


#: The pair each May session uses, worked out by hand from the Friday chain: a date keeps the previous date's pair
#: while its far expiry is still 30 or more days out, and picks afresh (latest expiry 7 to 30 days out, earliest
#: 31 to 45 days out) the day after. (First session, last session, near expiry, far expiry.)
MAY_PAIRS = [
    (date(2024, 5, 1), date(2024, 5, 8), date(2024, 5, 31), date(2024, 6, 7)),
    (date(2024, 5, 9), date(2024, 5, 15), date(2024, 6, 7), date(2024, 6, 14)),
    (date(2024, 5, 16), date(2024, 5, 22), date(2024, 6, 14), date(2024, 6, 21)),
    (date(2024, 5, 23), date(2024, 5, 29), date(2024, 6, 21), date(2024, 6, 28)),
    (date(2024, 5, 30), date(2024, 5, 31), date(2024, 6, 28), date(2024, 7, 5)),
]


def test_the_thirty_day_vol_follows_the_pair_chosen_for_that_date():
    """With vol linear in days to expiry the answer depends on the pair: pin it to the closed form per date."""
    client = chain(MAY_SESSIONS, sigma=sloped_sigma)

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    for day, row in zip(MAY_SESSIONS, frame.itertuples(), strict=True):
        low, high = next((low, high) for first, last, low, high in MAY_PAIRS if first <= day <= last)
        dte_low, dte_high = (low - day).days, (high - day).days
        assert (row.dte_low, row.dte_high) == (dte_low, dte_high), day
        w_low = (dte_high - 30) / (dte_high - dte_low)
        total_variance = (
            w_low * sloped_sigma(day, dte_low) ** 2 * dte_low + (1 - w_low) * sloped_sigma(day, dte_high) ** 2 * dte_high
        )
        assert math.isclose(row.iv_30d_atm, math.sqrt(total_variance / 30), abs_tol=IV_ATOL, rel_tol=0), day


def test_a_month_costs_one_listing():
    sessions = MAY_SESSIONS + JUNE_SESSIONS
    client = chain(sessions)

    bracket = find_bracket_contracts("SPY", sessions[0].isoformat(), sessions[-1].isoformat(), client, stock_rows(sessions))

    assert bracket["bracket_gap"].isna().all()
    assert list_reads(client, bracket) == [date(2024, 5, 1), date(2024, 6, 3)], (
        "one list for May and one for June, each as of the month's first session, not one per session"
    )


def test_a_month_list_reaches_from_7_days_after_the_first_session_to_45_days_after_the_last():
    """A call within 5% of the money expiring 7 days after the first session is listed; 6 days after is not. 45 and 46 days past the last."""
    first, last = date(2024, 5, 1), date(2024, 5, 31)
    expiries = (first + timedelta(days=6), first + timedelta(days=7), last + timedelta(days=45), last + timedelta(days=46))
    client = chain(MAY_SESSIONS, expiries=expiries)

    month = contract_finder._list_month_expiries(client, first, last, 150.0)

    assert month == frozenset(e.isoformat() for e in (first + timedelta(days=7), last + timedelta(days=45)))


def test_a_month_list_holds_calls_within_five_percent_of_the_close_only():
    """The strike band is the provider's old query, float for float: ``close - close * 0.05`` to ``close + close * 0.05``."""
    near, far = date(2024, 5, 17), date(2024, 5, 24)
    client = chain(MAY_SESSIONS, expiries=(near, far))  # strikes 100 to 200
    month = contract_finder._list_month_expiries(client, date(2024, 5, 1), date(2024, 5, 1), 100.0)
    in_band = contract_finder._list_month_expiries(client, date(2024, 5, 1), date(2024, 5, 1), 150.0)

    assert month == frozenset({"2024-05-17", "2024-05-24"}), "a 100 strike is listed, so a close of 100 finds both expiries"
    assert in_band == frozenset({"2024-05-17", "2024-05-24"})
    assert contract_finder._list_month_expiries(client, date(2024, 5, 1), date(2024, 5, 1), 250.0) == frozenset(), (
        "no listed call is within 5% of a close of 250"
    )


# ---------------------------------------------------------------------------
# Expiries listed after the month's first session; dates master computed keep master's numbers
# ---------------------------------------------------------------------------


def test_an_expiry_listed_after_the_months_first_session_still_pairs_the_late_dates():
    """Weeklies listed six weeks ahead: on May 1 nothing past Jun 12 exists, so Jun 14 and later are listed in May."""
    expiries = fridays(date(2024, 4, 1), date(2024, 9, 1))
    client = chain(MAY_SESSIONS, listed_on={expiry: expiry - timedelta(weeks=6) for expiry in expiries})
    inputs = member_inputs(client, stock_rows(MAY_SESSIONS), MAY_SESSIONS, volume=1000)

    frame = sessions_of(build_iv_history("SPY", inputs, RateTally()), MAY_SESSIONS)

    assert frame["missing_reason"].isna().all(), "every session has a pair: the late ones list again as of their own date"
    assert (frame["dte_low"] <= 30).all() and (frame["dte_high"] >= 30).all()
    expected = [flat_sigma(day, 30) for day in MAY_SESSIONS]
    np.testing.assert_allclose(frame["iv_30d_atm"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    assert list_reads(client, inputs.bracket) == [
        date(2024, 5, 1),  # the month's first session
        date(2024, 5, 9),  # Jun 14 is the far expiry from here and was not yet listed on May 1
        date(2024, 5, 16),  # Jun 21
        date(2024, 5, 23),  # Jun 28
        date(2024, 5, 30),  # Jul 5
    ], "a re-list is taken only on a date whose pick found no far expiry in the earlier list"


#: ``build_iv_history`` on origin/master (e02a461e) for the window and chain below, whose volatility depends on strike
#: (0.20 - 0.5 * ln(K / S)), so a different strike choice shows up in the number. Generated 2026-10-24 by running this
#: module's ``ChainClient`` through ``build_iv_history`` on an unmodified copy of master's ``PythonDataService/app``
#: (``find_bracket_contracts`` there lists once per month and reuses the first session's pair). Master computes every
#: one of these dates; the window crosses the July/August edge, where the month's pair changes. Never hand-edited;
#: regenerate only by re-running master's code. (atm, put, call, dte_low, dte_high) per date.
MASTER_ACROSS_THE_JULY_AUGUST_EDGE = {
    "2024-07-24": (0.20000140547117956, 0.22040875987225939, 0.17402424773128553, 30, 37),
    "2024-07-25": (0.20299263522586003, 0.2240047841501548, 0.17647786980641675, 29, 36),
    "2024-07-26": (0.20596568328904982, 0.22754055155438413, 0.17893671017014737, 28, 35),
    "2024-07-29": (0.20891997439676052, 0.23198582642958318, 0.18054275097310046, 25, 32),
    "2024-07-30": (0.21185727353649061, 0.23535900426421186, 0.1830854313265825, 24, 31),
    "2024-07-31": (0.2147788301066133, 0.2386699776699856, 0.18564307073938274, 23, 30),
    "2024-08-01": (0.21496353552989944, 0.23872584691418738, 0.18550501916011197, 29, 36),
    "2024-08-02": (0.21524488438736192, 0.23887672924695394, 0.18547006580662898, 28, 35),
    "2024-08-05": (0.21106586255736723, 0.23435649860368524, 0.18036706455531037, 25, 32),
    "2024-08-06": (0.2118398867607058, 0.2350355074503289, 0.18085456350720455, 24, 31),
    "2024-08-07": (0.21273840507102496, 0.23584124443631196, 0.181477480557116, 23, 30),
}


def test_ivs_on_dates_master_computed_are_unchanged_across_a_month_edge():
    """Master computed all of these dates, so the per-date bracket must return its numbers, strikes included.

    A month's strikes are chosen the first session an expiry is used. Choosing the next month's pair one session early
    (on Jul 31, from Jul 31's close) would put August's strikes at the wrong close and move August's IVs by up to 3e-3.
    """
    sessions = [first + timedelta(days=n) for first in [date(2024, 7, 24)] for n in range(15)]
    sessions = [day for day in sessions if day.weekday() < 5]
    spots = {day: round(150.0 + 0.9 * i, 4) for i, day in enumerate(sessions)}
    client = ChainClient(
        expiries=fridays(date(2024, 4, 1), date(2024, 12, 31)),
        spots=spots,
        sigma=lambda day, dte: 0.20,
        skew=0.5,
    )
    stock_bars = [{"timestamp": noon_utc_ms(day), "close": spots[day]} for day in sessions]

    frame = build_iv_history("SPY", member_inputs(client, stock_bars, sessions, volume=1000), RateTally())

    assert list(frame["date"]) == list(MASTER_ACROSS_THE_JULY_AUGUST_EDGE)
    expected = pd.DataFrame.from_dict(
        MASTER_ACROSS_THE_JULY_AUGUST_EDGE,
        orient="index",
        columns=["iv_30d_atm", "iv_30d_put", "iv_30d_call", "dte_low", "dte_high"],
    )
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        np.testing.assert_allclose(
            frame[column].to_numpy(dtype=float), expected[column].to_numpy(dtype=float), atol=1e-9, rtol=0, err_msg=column
        )
    assert list(zip(frame["dte_low"], frame["dte_high"], strict=True)) == list(
        zip(expected["dte_low"], expected["dte_high"], strict=True)
    )


def test_a_carried_pair_is_dropped_once_its_near_expiry_is_under_seven_days_out_though_it_still_brackets_30_days():
    """Monthlies only: (Jun 21, Jul 19) brackets 30 days until Jun 19, but Jun 21 is under 7 days out from Jun 15."""
    monthlies = (date(2024, 4, 19), date(2024, 5, 17), date(2024, 6, 21), date(2024, 7, 19), date(2024, 8, 16))
    client = chain(JUNE_SESSIONS, expiries=monthlies)

    frame = find_bracket_contracts("SPY", "2024-06-03", "2024-06-28", client, stock_rows(JUNE_SESSIONS))

    rows = {row.date: (row.low_dte, row.high_dte, row.bracket_gap) for row in frame.itertuples()}
    assert rows["2024-06-03"][2] == "no_far_expiry", "Jul 19 is 46 days out"
    assert rows["2024-06-04"][:2] == (17, 45) and pd.isna(rows["2024-06-04"][2]), "Jul 19 enters at 45 days"
    assert rows["2024-06-14"][:2] == (7, 35) and pd.isna(rows["2024-06-14"][2]), "Jun 21 is 7 days out: its last day"
    assert rows["2024-06-17"][2] == rows["2024-06-18"][2] == "no_near_expiry", "Jun 21 is under 7 days out; Jul 19 over 30"
    assert rows["2024-06-20"][2] == rows["2024-06-28"][2] == "no_far_expiry", "Jul 19 is under 30 days out: nothing beyond"
    for day in ("2024-06-17", "2024-06-18", "2024-06-20", "2024-06-21", "2024-06-28"):
        assert pd.isna(rows[day][0]) and pd.isna(rows[day][1]), day


def test_a_date_that_finds_no_near_expiry_lists_again_and_picks_up_one_listed_since():
    """Jun 28 is listed on Jun 12: the June 3 list has no expiry 7 to 30 days out on Jun 17, when Jun 21 is too near."""
    monthlies = (date(2024, 4, 19), date(2024, 5, 17), date(2024, 6, 21), date(2024, 7, 19), date(2024, 8, 16))
    expiries = (*monthlies, date(2024, 6, 28))
    sessions = [day for day in JUNE_SESSIONS if day <= date(2024, 6, 18)]
    client = chain(sessions, expiries=expiries, listed_on={date(2024, 6, 28): date(2024, 6, 12)})

    frame = find_bracket_contracts("SPY", "2024-06-03", "2024-06-18", client, stock_rows(sessions))

    rows = {row.date: (row.low_dte, row.high_dte, row.bracket_gap) for row in frame.itertuples()}
    assert rows["2024-06-14"][:2] == (7, 35), "Jun 21 is 7 days out: its last day"
    assert rows["2024-06-17"][:2] == (11, 32) and pd.isna(rows["2024-06-17"][2]), "Jun 28 and Jul 19 bracket Jun 30"
    assert rows["2024-06-18"][:2] == (10, 31) and pd.isna(rows["2024-06-18"][2]), "carried: still brackets 30 days"
    assert list_reads(client, frame) == [
        date(2024, 6, 3),
        date(2024, 6, 17),  # the first date whose fresh pick found no near expiry in the June 3 list
    ]


# ---------------------------------------------------------------------------
# A date no listed pair brackets is a typed missing day, not a raise for the ticker
# ---------------------------------------------------------------------------


def test_sessions_no_listed_pair_brackets_are_typed_missing_days_not_a_raise_for_the_ticker():
    """Nothing is listed past Jun 14: from May 16 no expiry is 31-45 days out (May 15 is answered by Jun 14 itself)."""
    client = chain(MAY_SESSIONS, expiries=fridays(date(2024, 4, 1), date(2024, 6, 14)))

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    reasons = dict(zip(frame["date"], frame["missing_reason"], strict=True))
    covered = [d for d in MAY_SESSIONS if d <= date(2024, 5, 15)]
    uncovered = [d for d in MAY_SESSIONS if d > date(2024, 5, 15)]
    assert all(pd.isna(reasons[d.isoformat()]) for d in covered), "a date with a pair has no reason"
    assert {reasons[d.isoformat()] for d in uncovered} == {"no_far_expiry"}
    assert len(uncovered) == 11
    quality = dict(zip(frame["date"], frame["iv_quality"], strict=True))
    assert {quality[d.isoformat()] for d in covered} <= {"high", "medium"}
    assert {quality[d.isoformat()] for d in uncovered} == {"missing"}
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        assert frame.loc[frame["missing_reason"].notna(), column].isna().all(), f"{column} is not filled into a typed gap"
    may_15 = frame[frame["date"] == "2024-05-15"].iloc[0]
    assert (may_15["dte_low"], may_15["dte_high"]) == (23, 30), "Jun 14 is 30 days out: it is the high end of (Jun 7, Jun 14)"
    assert math.isclose(may_15["iv_30d_atm"], flat_sigma(date(2024, 5, 15), 30), abs_tol=IV_ATOL, rel_tol=0)
