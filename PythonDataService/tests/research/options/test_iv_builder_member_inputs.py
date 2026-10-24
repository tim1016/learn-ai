"""The IV builder over a member's admitted option inputs: the volatility a priced chain was generated with comes back out (#3524, #3540; R25).

The Cross-sectional study's option inputs are admitted from the data lake (``option_inputs.admit_member_option_inputs``,
tested through the lake in ``test_option_inputs_lake.py``). These tests run the real contract finder, IV builder and solver
over ``tests/_helpers/option_chain.OptionChain`` held in memory (``member_inputs``): the chain is priced with a known
volatility, so what the builder returns can be checked against the number that generated it, with no lake and no vendor.

The window is two months, as a real run's is. ``contract_finder`` reads a month's expiries from one listing and picks each
date's pair around 30 days from it (#3528), so the run crosses a month edge and changes pair under it.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.config import settings
from app.research.options import iv_builder
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_inputs import RateTally, combined_rate_block
from app.services import fred_service
from app.services.fred_service import RATE_SOURCE_FRED, RateResolution
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from tests._helpers.option_chain import OptionChain, member_inputs, option_ticker, parse_option_ticker, stock_rows

#: October and November 2024 up to Thanksgiving: every weekday is a session, so the frame the builder pads to business
#: days holds exactly these rows. One expiry list per month (Oct 1, Nov 1) serves the whole window on a chain that lists
#: a Friday every week.
WINDOW = [day for day in (date(2024, 10, 1) + timedelta(days=n) for n in range(58)) if day.weekday() < 5]
#: A slow walk, so a strike chosen at an expiry's first use is still within a few percent of the money when it leaves the pair.
STEP = 0.1

#: Option prices are rounded to 1e-4, so a price is off by at most 5e-5; vega is at least 8.5 per unit of volatility for
#: the 23-36 day options this window prices (ATM 15 to 17, a 25-delta wing 8.5 to 12), so the implied volatility is
#: recovered to within 5e-5 / 8.5 = 5.9e-6. The tolerance stays at 1e-5, the value the short window pinned; it was not
#: loosened to take the longer window. Measured on this payload the largest error is 3.5e-6 (put wing); the solver's own
#: tolerance adds nothing visible.
IV_ATOL = 1e-5


@pytest.fixture(autouse=True)
def no_fred(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED is not reachable from a test: the rate is the one default, and the tally says so."""
    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)


def chain_for(rows: list[dict], days: list[date] = WINDOW, **options) -> OptionChain:
    spots = {day: row["close"] for day, row in zip(days, rows, strict=True)}
    return OptionChain("SPY", spots, rate=DEFAULT_RISK_FREE_RATE, **options)


def build(chain: OptionChain, rows: list[dict], rates: RateTally, *, days: list[date] = WINDOW):
    return build_iv_history("SPY", member_inputs(chain, rows, days), rates)


# ---------------------------------------------------------------------------
# The numbers: the builder recovers the volatility the chain was priced with
# ---------------------------------------------------------------------------


def test_the_iv_history_recovers_the_volatility_the_chain_was_priced_with():
    """Both brackets are priced at the day's volatility, so the 30-day interpolation must return it."""
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)

    frame = build(chain, rows, RateTally())

    assert list(frame["date"]) == [day.isoformat() for day in WINDOW]
    expected = [chain.sigma(day) for day in WINDOW]
    np.testing.assert_allclose(frame["iv_30d_atm"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    np.testing.assert_allclose(frame["iv_30d_put"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    np.testing.assert_allclose(frame["iv_30d_call"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    assert frame["stock_close"].tolist() == [row["close"] for row in rows], "the spot is the stock bar the study passed in"
    assert set(frame["price_source"]) == {"close_filtered"}, "a lake bar has a close and a volume: the price is its close"


def test_a_different_stock_close_picks_a_different_atm_contract():
    """The stock close sets the ATM strike, so the session the close comes from can change the contracts priced."""
    low_rows, high_rows = stock_rows(WINDOW[:1], start=150.0), stock_rows(WINDOW[:1], start=150.9)

    low = member_inputs(chain_for(low_rows, WINDOW[:1]), low_rows, WINDOW[:1])
    high = member_inputs(chain_for(high_rows, WINDOW[:1]), high_rows, WINDOW[:1])

    strikes = lambda inputs: {parse_option_ticker(contract)[3] for contract, _ in inputs.days}  # noqa: E731
    # 150.0 and 150.9 sit on either side of the 150.5 midpoint between the $150 and $151 strikes.
    assert 150.0 in strikes(low) and 150.0 not in strikes(high)
    assert 151.0 in strikes(high) and 151.0 not in strikes(low)


# ---------------------------------------------------------------------------
# The builder reads the admitted inputs and nothing else
# ---------------------------------------------------------------------------


def test_without_stock_bars_there_is_nothing_to_pick_from_and_the_chain_is_never_asked():
    chain = chain_for(stock_rows(WINDOW))

    inputs = member_inputs(chain, [], WINDOW)
    frame = build_iv_history("SPY", inputs, RateTally())

    assert inputs.bracket.empty and frame.empty
    assert chain.listing_requests == [], "no stock close, no strike to compare, no listing read"


def test_a_month_is_listed_once_and_a_late_listed_expiry_makes_a_later_session_list_again():
    """Weeklies listed six weeks ahead: the first list of a month lacks the late ones, so some picks list again."""
    rows = stock_rows(WINDOW)
    weekly = chain_for(rows)._expiries()
    chain = chain_for(rows, listed_on={expiry: expiry - timedelta(weeks=6) for expiry in weekly})

    frame = build(chain, rows, RateTally())

    lists = chain.listing_requests
    assert date(2024, 10, 1) in lists and date(2024, 11, 1) in lists, "each month is listed as of its first session"
    relists = [day for day in lists if day.day != 1]
    assert relists and set(relists) <= set(WINDOW), "and a session whose pick found no near or far expiry listed as of itself"
    assert frame["missing_reason"].isna().all(), "every session has a pair"
    np.testing.assert_allclose(
        frame["iv_30d_atm"].to_numpy(dtype=float), [chain.sigma(day) for day in WINDOW], atol=IV_ATOL, rtol=0
    )


# ---------------------------------------------------------------------------
# The risk-free rate: the one live input, counted apart from the lake's
# ---------------------------------------------------------------------------


def test_the_risk_free_rate_source_is_counted_per_day(monkeypatch):
    rows = stock_rows(WINDOW)
    monkeypatch.setattr(iv_builder, "resolve_risk_free_rate", lambda **kwargs: RateResolution(DEFAULT_RISK_FREE_RATE, RATE_SOURCE_FRED))
    rates = RateTally()

    build(chain_for(rows), rows, rates)

    assert (rates.days_from_fred, rates.days_from_default) == (len(WINDOW), 0)
    assert rates.degraded is False
    assert rates.as_dict() == {
        "kind": "provider", "provider": "fred", "days_from_fred": len(WINDOW), "days_from_default": 0, "days_fetch_failed": 0,
        "values_digest": rates.values_digest(),
    }
    assert len(rates.values_digest()) == 64


def test_a_default_rate_with_no_fred_key_is_not_an_outage_but_one_with_a_key_is(monkeypatch):
    """#3524: no key is the same answer on every attempt; a configured key that gets nothing could answer next time."""
    rows = stock_rows(WINDOW)
    monkeypatch.setattr(fred_service, "_fetch_series", lambda *args, **kwargs: [])
    fred_service.clear_cache()
    no_key, key_but_down = RateTally(), RateTally()

    build(chain_for(rows), rows, no_key)  # the autouse fixture configures no key
    monkeypatch.setattr(settings, "FRED_API_KEY", "a-configured-key", raising=False)
    build(chain_for(rows), rows, key_but_down)

    assert no_key.days_from_default == len(WINDOW) and no_key.days_fetch_failed == 0 and no_key.degraded is False
    assert key_but_down.days_from_default == len(WINDOW) and key_but_down.days_fetch_failed == len(WINDOW)
    assert key_but_down.degraded is True
    assert combined_rate_block({"SPY": no_key, "QQQ": key_but_down})["days_fetch_failed"] == len(WINDOW)


def test_the_rate_values_digest_is_the_digest_of_the_rates_used():
    rows = stock_rows(WINDOW)
    first, again, other = RateTally(), RateTally(), RateTally()

    build(chain_for(rows), rows, first)
    build(chain_for(rows), rows, again)
    for day in WINDOW:
        other.record(day.isoformat(), RateResolution(0.05, RATE_SOURCE_FRED))

    assert first.values_digest() == again.values_digest(), "the same rates, the same identity"
    assert first.values_digest() != other.values_digest()
    assert RateTally().values_digest() is None


def test_combined_rate_block_sums_the_members_and_digests_their_digests():
    rows = stock_rows(WINDOW)
    spy, qqq = RateTally(), RateTally()
    build(chain_for(rows), rows, spy)
    build(chain_for(rows), rows, qqq)

    run = combined_rate_block({"SPY": spy, "QQQ": qqq})

    assert run["days_from_default"] == 2 * len(WINDOW) and run["days_from_fred"] == 0
    assert run["kind"] == "provider" and run["provider"] == "fred" and len(run["values_digest"]) == 64
    assert combined_rate_block({})["values_digest"] is None


def test_option_ticker_round_trips_through_its_parser():
    ticker = option_ticker("SPY", date(2024, 3, 29), "put", 150.5)

    assert ticker == "O:SPY240329P00150500"
    assert parse_option_ticker(ticker) == ("SPY", date(2024, 3, 29), "put", 150.5)
