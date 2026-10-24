"""``build_iv_history`` over a window longer than the first session's bracket (#3528).

``find_bracket_contracts`` used to look the two expiries around 30 days up once per month and reuse them for the
whole month, so ``k`` days in both DTEs had fallen by ``k``. ``variance_interpolated_iv`` (#3484) raises when 30 days
is outside ``[dte_low, dte_high]``, so any session more than ``dte_high - 30`` days after its month's first session
raised, and the whole ticker was lost. The bracket is now chosen per date from one wide expiry lookup per month.

The chain is a pinned listed weekly-Friday chain priced with a known volatility, served in place of
``PolygonClientService`` (``list_options_contracts`` and ``fetch_aggregates``; the underlying's own bars are the
``stock_bars`` argument and a request for them fails the test). The real contract finder, IV builder and solver run
over it, so what comes back is checked against the number that generated the prices.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.research.options import iv_builder
from app.research.options.iv_builder import build_iv_history
from app.services.bs_greeks import bs_european_price

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
    """The slice of ``PolygonClientService`` the contract finder and IV builder call, over a pinned chain."""

    expiries: tuple[date, ...]
    spots: dict[date, float]
    sigma: Callable[[date, int], float] = flat_sigma
    expiry_lookup_error: Exception | None = None
    expiry_lookups: list[dict[str, Any]] = field(default_factory=list)

    def list_options_contracts(self, **kw: Any) -> list[dict[str, Any]]:
        is_expiry_lookup = kw.get("expiration_date") is None
        if is_expiry_lookup:
            self.expiry_lookups.append(kw)
            if self.expiry_lookup_error is not None:
                raise self.expiry_lookup_error
        as_of = date.fromisoformat(kw["as_of_date"]) if kw.get("as_of_date") else None
        out: list[dict[str, Any]] = []
        for expiry in sorted(self.expiries):
            iso = expiry.isoformat()
            if kw.get("expiration_date") not in (None, iso):
                continue
            if (kw.get("expiration_date_gte") or "") > iso or iso > (kw.get("expiration_date_lte") or "9999"):
                continue
            if as_of is not None and expiry < as_of:
                continue
            for strike in np.arange(100.0, 201.0, 1.0):
                if not (kw.get("strike_price_gte") or 0) <= strike <= (kw.get("strike_price_lte") or 1e9):
                    continue
                for kind in ("call", "put"):
                    if kw.get("contract_type") in (None, kind):
                        out.append(
                            {
                                "ticker": option_ticker(expiry, kind, strike),
                                "contract_type": kind,
                                "strike_price": float(strike),
                                "expiration_date": iso,
                            }
                        )
        return out[: kw["limit"]]

    def fetch_aggregates(self, *, ticker: str, from_date: str, to_date: str, **_: Any) -> list[dict[str, Any]]:
        if not ticker.startswith("O:"):
            raise AssertionError(f"the underlying's bars are passed in, not fetched: asked for {ticker}")
        expiry, kind, strike = parse_option_ticker(ticker)
        bars = []
        for day, spot in sorted(self.spots.items()):
            if not (from_date <= day.isoformat() <= to_date) or day >= expiry:
                continue
            dte = (expiry - day).days
            price = bs_european_price(spot, strike, dte / 365.0, RATE, self.sigma(day, dte), kind == "call")
            if price < 0.05:
                continue
            price = round(price, 4)
            bars.append({"timestamp": noon_utc_ms(day), "close": price, "vw": price, "volume": 1000})
        return bars


@pytest.fixture(autouse=True)
def fixed_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED is not reachable from a test: every session is solved at the one rate the chain is priced with."""
    monkeypatch.setattr(iv_builder, "get_risk_free_rate", lambda **kwargs: RATE)


def stock_rows(sessions: list[date]) -> list[dict[str, Any]]:
    return [{"timestamp": noon_utc_ms(day), "close": round(150.0 + 0.37 * i, 4)} for i, day in enumerate(sessions)]


def build(sessions: list[date], client: ChainClient) -> Any:
    return build_iv_history("SPY", sessions[0].isoformat(), sessions[-1].isoformat(), client, stock_rows(sessions))


def chain(sessions: list[date], **options: Any) -> ChainClient:
    spots = {row_day: row["close"] for row_day, row in zip(sessions, stock_rows(sessions), strict=True)}
    expiries = options.pop("expiries", fridays(date(2024, 4, 1), date(2024, 9, 1)))
    return ChainClient(expiries=expiries, spots=spots, **options)


def sessions_of(frame: Any, sessions: list[date]) -> Any:
    """The rows for real sessions: ``build_iv_history`` pads the frame to business days, holidays included."""
    return frame[frame["date"].isin([day.isoformat() for day in sessions])].reset_index(drop=True)


# ---------------------------------------------------------------------------
# The regression: a window longer than the first session's bracket
# ---------------------------------------------------------------------------


def test_a_window_past_the_first_sessions_bracket_edge_builds_instead_of_raising():
    """May 1's bracket is (May 31, Jun 7): 30 days leaves it after May 8, the old month-long reuse raised there."""
    client = chain(MAY_SESSIONS)

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    assert list(frame["date"]) == [day.isoformat() for day in MAY_SESSIONS]
    assert (frame["dte_low"] <= 30).all() and (frame["dte_high"] >= 30).all(), "30 days is inside every day's bracket"
    assert (frame["iv_quality"] != "missing").all(), "no session is lost"


def test_every_session_recovers_the_volatility_the_chain_was_priced_with():
    """The chain's volatility is the same at every expiry, so any pair that brackets 30 days must return it."""
    client = chain(MAY_SESSIONS)

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    expected = [flat_sigma(day, 30) for day in MAY_SESSIONS]
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        np.testing.assert_allclose(frame[column].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0, err_msg=column)


def test_the_thirty_day_vol_follows_the_pair_chosen_for_that_date():
    """With vol linear in days to expiry the answer depends on the pair: pin it to the closed form per date."""
    client = chain(MAY_SESSIONS, sigma=sloped_sigma)
    listed = fridays(date(2024, 4, 1), date(2024, 9, 1))

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    for day, row in zip(MAY_SESSIONS, frame.itertuples(), strict=True):
        low = max(e for e in listed if e <= day + timedelta(days=30))
        high = min(e for e in listed if e > day + timedelta(days=30))
        dte_low, dte_high = (low - day).days, (high - day).days
        assert (row.dte_low, row.dte_high) == (dte_low, dte_high), day
        w_low = (dte_high - 30) / (dte_high - dte_low)
        total_variance = (
            w_low * sloped_sigma(day, dte_low) ** 2 * dte_low + (1 - w_low) * sloped_sigma(day, dte_high) ** 2 * dte_high
        )
        assert math.isclose(row.iv_30d_atm, math.sqrt(total_variance / 30), abs_tol=IV_ATOL, rel_tol=0), day


def test_a_month_costs_one_expiry_lookup():
    sessions = MAY_SESSIONS + JUNE_SESSIONS
    client = chain(sessions)

    frame = sessions_of(build(sessions, client), sessions)

    assert len(client.expiry_lookups) == 2, "one wide lookup for May and one for June, not one per session"
    assert (frame["iv_quality"] != "missing").all()
    first, last = client.expiry_lookups[0], client.expiry_lookups[1]
    assert first["as_of_date"] == "2024-05-01" and last["as_of_date"] == "2024-06-03"
    assert first["expiration_date_gte"] == "2024-05-08", "the near end is the first session's 7 days"
    assert first["expiration_date_lte"] == "2024-07-15", "the far end reaches 45 days past the month's last session"


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
    may_15 = frame[frame["date"] == "2024-05-15"].iloc[0]
    assert (may_15["dte_low"], may_15["dte_high"]) == (23, 30), "Jun 14 is 30 days out: it is the high end of (Jun 7, Jun 14)"
    assert math.isclose(may_15["iv_30d_atm"], flat_sigma(date(2024, 5, 15), 30), abs_tol=IV_ATOL, rel_tol=0)


def test_a_failed_expiry_lookup_marks_the_month_missing_with_its_own_reason():
    client = chain(MAY_SESSIONS, expiry_lookup_error=RuntimeError("vendor down"))

    frame = sessions_of(build(MAY_SESSIONS, client), MAY_SESSIONS)

    assert (frame["iv_quality"] == "missing").all()
    assert (frame["missing_reason"] == "expiry_lookup_failed").all(), "a lookup that failed is not an empty chain"
