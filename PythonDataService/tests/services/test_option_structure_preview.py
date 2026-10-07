"""The option structure preview: what the owner's rules would pick right now, and each way it has nothing to show.

Every preview here is asked of a real supervisor on a stand-in IBKR
connection: the chain is listed, lines are opened and quotes arrive through
its callbacks, as they would from the Gateway.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import numpy as np
import pytest

from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.option_quotes import CEILING_PROBE_MS, OptionQuoteLines, WatchRequest
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.options.american import CashDividend, american_daily_decay, american_implied_volatility, years_from_ms
from app.options.decay import DecayNotEstimated
from app.options.expiry import option_expiry_anchor_ms
from app.options.lean import ABOUT_FLAT_BELOW_DELTA
from app.options.named_structures import NamedStructure
from app.options.resolver import DividendOn
from app.options.structure_catalogue import starting_plan
from app.options.watch import LineClass
from app.schemas.option_preview import OptionStructurePreview, OptionStructurePreviewRequest
from app.services import option_structure_preview
from app.services.broker_v2_panel import option_structure_panel
from app.services.option_model_inputs import ModelInputsBeingRead, ModelInputsRead
from app.services.option_preview_views import usd
from app.services.option_search import PREVIEW_SEARCH_WAIT_S, OptionSearchLane, PreviewSearches
from app.services.option_structure_preview import (
    ARRIVAL_GRACE_MS,
    FIGURES_GOOD_FOR_MS,
    MAX_PREVIEW_WINDOWS,
    PREVIEW_LEASE_MS,
    preview_option_structure,
)
from app.services.option_watch import OptionWatchList

# The stand-in Gateway, and the resolver's own scenes: SPY at 585 on a dollar-wide ladder, a Tuesday at 10:31:02.
from tests.broker.ibkr import test_option_quotes as feed
from tests.options import test_resolver as scenes

# The stand-in coordinator a fleet Clerk asks for the rate and the dividends.
from tests.services import test_option_model_inputs_client as fleet

# A preview lane that searches in this process, through the pickling a worker process's pipe does; and
# stand-in workers whose searches a test holds back, or loses.
from tests.services.test_option_search import Workers, _Dies, searches_in_this_process

NOW = feed.NOW
FRIDAY = scenes._FRIDAY
_ON_FRIDAY = {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(FRIDAY)}
_AT_THE_MONEY = {"selector": "atm"}
_KNOWN = ModelInputsRead(
    rate=0.045,
    rate_source="test",
    rate_is_default=False,
    dividends=(),
    dividends_source="test: none declared",
    dividends_read_ms=NOW,
)
_UNKNOWN = replace(_KNOWN, dividends=None, dividends_source="test: the lookup failed")


def _leg(leg_id: str, position: str, right: str, strike: dict, expiry: dict = _ON_FRIDAY, *, ratio: int = 1) -> dict:
    return {"leg_id": leg_id, "position": position, "qty_ratio": ratio, "right": right, "strike": strike, "expiry": expiry}


def _from(leg_id: str, dollars: float) -> dict:
    return {"selector": "leg_distance", "leg_id": leg_id, "dollars": dollars}


def _asking(*legs: dict, **request: Any) -> OptionStructurePreviewRequest:
    return OptionStructurePreviewRequest.model_validate(
        {"preview_key": "page-0001", "underlying": "SPY", "legs": list(legs), **request}
    )


_BULL_CALL_SPREAD = _asking(
    _leg("long_call", "long", "call", _AT_THE_MONEY),
    _leg("short_call", "short", "call", _from("long_call", 5.0)),
    structure="bull_call_spread",
)


class Desk:
    """One Clerk's option quotes on a stand-in Gateway, the list it watches from, and a page asking for previews."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *bots: WatchRequest, budget: int = 60, reach: int = 2) -> None:
        self.ibkr = feed.Gateway(monkeypatch)
        self.reach = reach
        # What the lookup of the rate and the dividends answers, or the exception it raises.
        self.inputs: ModelInputsRead | Exception = _KNOWN
        self.watch = OptionWatchList(clock=lambda: self.ibkr.now)
        for number, bot in enumerate(bots):
            self.watch.watch(f"bot-{number}", bot)
        self.lines = OptionQuoteLines(
            requests=self.watch.requests, budget=budget, strikes_each_side=reach, clock=lambda: self.ibkr.now
        )
        self.status = IbkrMarketStatusSource(
            symbols=lambda: (), client=lambda: self.ibkr.client, clock=lambda: self.ibkr.now, options=self.lines
        )
        # Where the search for the contracts runs. A test of the real worker process puts its own here.
        self.searches = searches_in_this_process()

    def _model_inputs(self, _underlying: str, _at_ms: int, _latest_expiry: date) -> ModelInputsRead:
        if isinstance(self.inputs, Exception):
            raise self.inputs
        return self.inputs

    async def preview(self, request: OptionStructurePreviewRequest) -> OptionStructurePreview:
        return await preview_option_structure(
            request,
            quotes=self.status,
            watch=self.watch,
            model_inputs=self._model_inputs,
            searches=self.searches,
            strikes_each_side=self.reach,
            clock=lambda: self.ibkr.now,
        )

    async def settle(self, passes: int = 4) -> None:
        await feed._settle(self.status, passes)

    def spy(self) -> None:
        self.ibkr.tick("SPY", bid=585.10, ask=585.12)

    def quote(self, right: str, strike: int, bid: str | None, ask: str | None, expiry: date = FRIDAY) -> None:
        """A tick of each side given. One side alone leaves the other as it was received."""
        self.ibkr.tick(
            scenes._contract(right, strike, expiry).occ_symbol,
            bid=None if bid is None else float(bid),
            ask=None if ask is None else float(ask),
        )

    def quote_every_line(self) -> None:
        """A two-cent market around the model's own price for every open line, as the resolver's scenes have it."""
        held = scenes._scene().chain.watched
        for name in sorted(self.ibkr.open_option_lines()):
            quote = held[name].quote
            self.ibkr.tick(name, bid=float(quote.bid.price), ask=float(quote.ask.price))

    async def watching(self, request: OptionStructurePreviewRequest) -> None:
        """Ask once, and let the supervisor list the chain, quote SPY and open the window's lines."""
        await self.preview(request)
        await self.settle()
        self.spy()
        await self.settle()

    async def quoted(self, request: OptionStructurePreviewRequest, **markets: tuple[str, str]) -> OptionStructurePreview:
        """The preview of ``request`` once every line has a quote, 400 ms old. ``markets`` sets a call's bid and
        ask by its strike: ``c585=("4.08", "4.12")``."""
        await self.watching(request)
        self.quote_every_line()
        for named, (bid, ask) in markets.items():
            self.quote("call" if named[0] == "c" else "put", int(named[1:]), bid, ask)
        self.spy()
        self.ibkr.now += 400
        return await self.preview(request)


def _as_the_builder_starts(name: NamedStructure, **request: Any) -> OptionStructurePreviewRequest:
    """A named structure with the legs the catalogue gives it, as the builder posts them back."""
    legs = [
        _leg(leg.leg_id, leg.position, leg.right, leg.strike.model_dump(), leg.expiry.model_dump(), ratio=leg.qty_ratio)
        for leg in starting_plan(name, "SPY").on_enter
    ]
    return _asking(*legs, structure=name.value, **request)


def _our_delta(
    right: str, strike: int, bid: str, ask: str, *, at_ms: int, dividends: tuple[DividendOn, ...] = ()
) -> float:
    """The model's delta for a Friday contract's mid quote against SPY's mid of 585.11, worked out apart."""
    solved = american_implied_volatility(
        price=float((Decimal(bid) + Decimal(ask)) / 2),
        spot=585.11,
        strike=float(strike),
        years_to_expiry=years_from_ms(session_close_ms_utc(FRIDAY) - at_ms),
        rate=0.045,
        right=right,
        dividends=tuple(CashDividend(years_to_ex=years_from_ms(paid.ex_ms - at_ms), cash=paid.cash) for paid in dividends),
    )
    assert solved.delta is not None
    return solved.delta


def _close(actual: float, expected: float) -> None:
    np.testing.assert_allclose(actual, expected, atol=1e-9, rtol=0, equal_nan=False)


# ---- A structure that resolves --------------------------------------------------


async def test_a_bull_call_spread_previews_with_its_contracts_its_money_and_what_it_leans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 585 call at 4.08 by 4.12 and the 590 at 2.18 by 2.22: bought for $1.94, the PRD's own case."""
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (seen.state, seen.reason_code, seen.notice) == ("resolved", None, None)
    assert (seen.quotes_read_ms, seen.feed_generation) == (NOW + 400, desk.lines.generation)
    assert seen.oldest_quote_age_ms == 400
    # A preview has no decision bar: the money is SPY's live mid.
    assert seen.the_money == Decimal("585.11")
    assert (seen.underlying_quote.bid, seen.underlying_quote.ask, seen.underlying_quote.bid_age_ms) == (
        Decimal("585.1"),
        Decimal("585.12"),
        400,
    )
    bought, sold = seen.legs
    assert (bought.leg_id, bought.position, bought.ratio, bought.contract.occ_symbol) == (
        "long_call", "long", 1, "SPY261009C00585000",
    )
    assert (bought.contract.underlying, bought.contract.right, bought.contract.strike, bought.contract.expiry_ms) == (
        "SPY", "call", Decimal(585), option_expiry_anchor_ms(FRIDAY),
    )
    assert (bought.quote.bid, bought.quote.ask, bought.quote.bid_age_ms, bought.quote.ask_age_ms) == (
        Decimal("4.08"), Decimal("4.12"), 400, 400,
    )
    assert (sold.position, sold.contract.occ_symbol, sold.quote.bid, sold.quote.ask) == (
        "short", "SPY261009C00590000", Decimal("2.18"), Decimal("2.22"),
    )
    # Four cents on a mid of 4.10 is 0.9756%; on a mid of 2.20, 1.8182%.
    assert (bought.gap_percent_of_mid, sold.gap_percent_of_mid) == (Decimal("0.98"), Decimal("1.82"))

    money = seen.money
    assert (money.structures, money.multiplier, money.net_price, money.premium_is) == (1, 100, Decimal("1.94"), "paid")
    assert (money.net_entry_premium_cents, money.net_entry_premium_per_structure_cents) == (19_400, 19_400)
    assert money.broker_requirement_cents == 0
    # On 2026-10-06: the SEC fee on the $218.00 sold, 0.45 of a cent, rounds up to 1; CAT on 200 shares to 1;
    # ORF is 2 x 1.5 = 3; OCC is 2 x 2.5 = 5; FINRA's TAF is paused. Ten cents.
    assert (money.fee_estimate_cents, money.entry_commitment_cents) == (10, 19_410)
    # Four cents a share on each leg, a hundred shares: $8.00, which is 4.12% of $194.10.
    assert (money.round_trip_cost_cents, money.round_trip_cost_percent) == (800, Decimal("4.12"))
    assert (money.cost_limit_percent, money.over_cost_limit) == (Decimal(10), False)

    at_expiry = seen.at_expiry
    assert (at_expiry.estimated, at_expiry.loss_cents, at_expiry.gain_cents, at_expiry.gain_has_no_limit) == (
        True, 19_400, 30_600, False,
    )
    assert [(held.from_price, held.to_price) for held in at_expiry.loss_holds] == [(Decimal(0), Decimal(585))]
    assert [(held.from_price, held.to_price) for held in at_expiry.gain_holds] == [(Decimal(590), None)]
    assert at_expiry.break_even_prices == [Decimal("586.94")]
    # Each sentence states its own figure, and where it holds: the loss at 585 or below, the gain at 590 or above.
    assert usd(19_400) in at_expiry.loss_sentence and usd(30_600) not in at_expiry.loss_sentence
    assert "585" in at_expiry.loss_sentence and "below" in at_expiry.loss_sentence and "above" not in at_expiry.loss_sentence
    assert usd(30_600) in at_expiry.gain_sentence and usd(19_400) not in at_expiry.gain_sentence
    assert "590" in at_expiry.gain_sentence and "above" in at_expiry.gain_sentence and "below" not in at_expiry.gain_sentence
    assert "586.94" in at_expiry.break_even_sentence
    # The cost is said as a part of what is set aside, which is not the premium, against the owner's limit.
    assert all(said in money.cost_sentence for said in (usd(800), "4.12%", usd(19_410), "10%"))
    assert usd(19_400) not in money.cost_sentence

    ours = [_our_delta("call", 585, "4.08", "4.12", at_ms=NOW + 400), _our_delta("call", 590, "2.18", "2.22", at_ms=NOW + 400)]
    assert [leg.delta for leg in seen.legs] == ours
    assert all(leg.delta_status == "solved" and leg.implied_volatility > 0 for leg in seen.legs)
    _close(seen.lean.net_delta, ours[0] - ours[1])
    _close(seen.lean.shares_per_structure, 100 * (ours[0] - ours[1]))
    _close(seen.lean.shares, 100 * (ours[0] - ours[1]))
    assert (seen.lean.direction, seen.lean.gains_when_underlying_falls, seen.lean.warning_sentence) == ("up", False, None)
    assert "up" in seen.lean.sentence.split(".")[0] and "down" not in seen.lean.sentence
    assert f"{seen.lean.shares:,.0f} SPY shares" in seen.lean.sentence and "short" not in seen.lean.sentence
    _close(seen.lean.about_flat_below_shares_per_structure, 100 * ABOUT_FLAT_BELOW_DELTA)

    verdict = seen.verdict
    assert (verdict.can_be_deployed, verdict.resolves_as_built, verdict.red, verdict.leg_ids) == (True, True, False, [])
    assert "a bull call spread" in verdict.resolves_sentence
    # Two strikes either side of 585 for the bought call and of 590 for the sold one: 583 to 592, ten lines.
    (calls,) = seen.watched.ranges
    assert (calls.right, calls.expiry_ms, calls.lowest_strike, calls.highest_strike, calls.contracts) == (
        "call", option_expiry_anchor_ms(FRIDAY), Decimal(583), Decimal(592), 10,
    )
    assert (seen.watched.lines_wanted, seen.watched.lines_held, seen.watched.lines_refused) == (10, 10, 0)
    assert (seen.watched.line_budget, seen.watched.lines_open) == (60, 10)
    assert (seen.pricing_inputs.rate, seen.pricing_inputs.dividends_known, seen.pricing_inputs.dividends) == (0.045, True, [])


async def test_a_spread_that_pays_shows_the_requirement_and_what_is_set_aside_as_two_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 585 call sold at its bid of 5.00 and the 590 bought at its ask of 1.93: $3.07 received on a spread
    $5 wide. Alpaca's requirement is the $500.00; what is set aside is $500.00 - $307.00, and the fees."""
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread",
    )

    seen = await desk.quoted(received, c585=("5.00", "5.04"), c590=("1.89", "1.93"))

    money = seen.money
    assert (money.net_price, money.premium_is, money.net_entry_premium_cents) == (Decimal("-3.07"), "received", -30_700)
    # The SEC fee on the $500.00 sold is 1.03 cents, which rounds up to 2: eleven cents of fees.
    assert (money.broker_requirement_cents, money.fee_estimate_cents, money.entry_commitment_cents) == (50_000, 11, 19_311)
    assert (seen.at_expiry.loss_cents, seen.at_expiry.gain_cents) == (19_300, 30_700)
    assert seen.at_expiry.break_even_prices == [Decimal("588.07")]
    # The other way round from a spread that is bought: it keeps what it received at 585 or below.
    assert usd(19_300) in seen.at_expiry.loss_sentence and "590" in seen.at_expiry.loss_sentence
    assert "above" in seen.at_expiry.loss_sentence and "below" not in seen.at_expiry.loss_sentence
    assert usd(30_700) in seen.at_expiry.gain_sentence and "585" in seen.at_expiry.gain_sentence
    assert "below" in seen.at_expiry.gain_sentence and "above" not in seen.at_expiry.gain_sentence
    # What is set aside is the $193.11, and the sentence says the cost as a part of that.
    assert usd(19_311) in money.cost_sentence and usd(30_700) not in money.cost_sentence
    assert (seen.verdict.resolves_as_built, seen.verdict.red) == (True, False)
    # It sells the call nearer the money, so it gains as SPY falls.
    assert (seen.lean.direction, seen.lean.gains_when_underlying_falls) == ("down", True)
    assert "down" in seen.lean.sentence.split(".")[0] and "short" in seen.lean.sentence
    assert f"{-seen.lean.shares:,.0f} SPY shares" in seen.lean.sentence


async def test_the_fee_estimate_is_worked_out_on_the_price_each_leg_is_entered_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 585 call is sold at its bid of 4.85. The SEC fee on $485.00 is 0.9991 of a cent, which rounds up to
    1; on its ask of 4.89 it would be 1.0073, and round up to 2. With CAT 1, ORF 3 and OCC 5: ten cents."""
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread",
    )

    seen = await desk.quoted(received, c585=("4.85", "4.89"), c590=("1.89", "1.93"))

    assert (seen.money.net_price, seen.money.fee_estimate_cents) == (Decimal("-2.92"), 10)
    assert seen.money.entry_commitment_cents == 50_000 - 29_200 + 10


async def test_more_than_one_structure_multiplies_the_money_and_the_lean(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)

    seen = await desk.quoted(
        _BULL_CALL_SPREAD.model_copy(update={"structures": 3}), c585=("4.08", "4.12"), c590=("2.18", "2.22")
    )

    assert (seen.money.structures, seen.money.net_entry_premium_cents) == (3, 58_200)
    assert seen.money.net_entry_premium_per_structure_cents == 19_400
    # Six contracts: the SEC fee on the $654.00 sold is 1.35 cents, 2 rounded up; CAT on 600 shares is 0.18
    # of a cent, 1; ORF is 6 x 1.5 = 9; OCC is 6 x 2.5 = 15. Twenty-seven cents.
    assert (seen.money.fee_estimate_cents, seen.money.entry_commitment_cents) == (27, 58_227)
    assert (seen.money.round_trip_cost_cents, seen.at_expiry.loss_cents, seen.at_expiry.gain_cents) == (2_400, 58_200, 91_800)
    _close(seen.lean.shares, 3 * seen.lean.shares_per_structure)


async def test_a_structure_that_costs_most_of_itself_to_trade_is_shown_with_its_figures_and_said_to_be_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mids of $5.00 and $4.90, each four cents wide: $0.14 to open, $0.08 of it lost closing at once."""
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.98", "5.02"), c590=("4.88", "4.92"))

    assert (seen.state, seen.money.net_entry_premium_cents, seen.money.round_trip_cost_cents) == ("resolved", 1_400, 800)
    assert seen.money.over_cost_limit is True
    # Over the limit is not a structure that cannot be deployed: an entry is skipped, and the verdict holds.
    assert seen.verdict.red is False


async def test_the_cost_is_held_to_the_limit_the_owner_set_and_not_to_the_one_they_start_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """$8.00 of the $194.10 set aside is 4.12%: inside the 10% an owner starts from, and over a limit of 4%."""
    desk = Desk(monkeypatch)
    strict = _BULL_CALL_SPREAD.model_copy(update={"cost_limit_percent": Decimal(4)})

    seen = await desk.quoted(strict, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (seen.money.cost_limit_percent, seen.money.over_cost_limit) == (Decimal(4), True)
    assert "4%" in seen.money.cost_sentence and "10%" not in seen.money.cost_sentence


async def test_each_side_of_each_quote_is_shown_with_its_own_age_and_the_oldest_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six sides received at six moments, read 2,000 ms after the first of them."""
    desk = Desk(monkeypatch)
    await desk.watching(_BULL_CALL_SPREAD)
    desk.quote_every_line()
    received = iter(range(0, 1_800, 300))

    def after(tick) -> None:
        desk.ibkr.now = NOW + next(received)
        tick()

    after(lambda: desk.quote("call", 585, "4.08", None))
    after(lambda: desk.quote("call", 585, None, "4.12"))
    after(lambda: desk.quote("call", 590, "2.18", None))
    after(lambda: desk.quote("call", 590, None, "2.22"))
    after(lambda: desk.ibkr.tick("SPY", bid=585.10))
    after(lambda: desk.ibkr.tick("SPY", ask=585.12))
    desk.ibkr.now = NOW + 2_000

    seen = await desk.preview(_BULL_CALL_SPREAD)

    bought, sold = seen.legs
    assert (bought.quote.bid_age_ms, bought.quote.ask_age_ms) == (2_000, 1_700)
    assert (sold.quote.bid_age_ms, sold.quote.ask_age_ms) == (1_400, 1_100)
    assert (seen.underlying_quote.bid_age_ms, seen.underlying_quote.ask_age_ms) == (800, 500)
    assert (seen.oldest_quote_age_ms, seen.quotes_read_ms) == (2_000, NOW + 2_000)


async def test_a_structure_that_gains_when_the_underlying_falls_carries_the_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    bought_put = _asking(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put", listens_to="QQQ")

    seen = await desk.quoted(bought_put)

    assert seen.state == "resolved"
    assert (seen.lean.direction, seen.lean.gains_when_underlying_falls) == ("down", True)
    assert seen.lean.shares < 0
    # The warning names what falls, and whose buy signal it is against.
    assert "SPY" in seen.lean.warning_sentence and "QQQ" in seen.lean.warning_sentence
    # A bought put gains most with the stock at zero, and its loss holds from its strike up.
    assert [(held.from_price, held.to_price) for held in seen.at_expiry.gain_holds] == [(Decimal(0), Decimal(0))]
    assert [(held.from_price, held.to_price) for held in seen.at_expiry.loss_holds] == [(Decimal(585), None)]


_CALL_NEAR_30_DELTA = _asking(
    _leg("long_call", "long", "call", {"selector": "delta", "target": 0.30}), structure="long_call"
)


async def test_a_rule_by_delta_is_previewed_from_this_systems_own_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch, reach=10)

    seen = await desk.quoted(_CALL_NEAR_30_DELTA)

    (leg,) = seen.legs
    assert seen.state == "resolved"
    assert abs(leg.delta - 0.30) <= 0.05
    assert (seen.at_expiry.gain_cents, seen.at_expiry.gain_has_no_limit) == (None, True)


async def test_a_rule_that_points_past_the_watched_strikes_is_an_incomplete_search_that_says_how_far_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six strikes either side of 585 are watched. The 591 call's delta is still over 0.30, so the nearest
    could be the 592, which nobody watches: not a pick, and not "nothing listed is close enough"."""
    desk = Desk(monkeypatch, reach=6)

    seen = await desk.quoted(_CALL_NEAR_30_DELTA)

    assert (seen.state, seen.reason_code) == ("not_resolved", "OPTION_CONTRACT_SEARCH_INCOMPLETE")
    assert seen.contract.strike == Decimal(592)
    (calls,) = seen.watched.ranges
    assert (calls.lowest_strike, calls.highest_strike) == (Decimal(579), Decimal(591))
    assert _shows_no_figure(seen)


async def test_legs_that_do_not_share_an_expiry_have_nothing_estimated_at_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    next_friday = {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(scenes._NEXT_FRIDAY)}
    two_expiries = _asking(
        _leg("near", "long", "call", _AT_THE_MONEY), _leg("far", "long", "call", _AT_THE_MONEY, next_friday)
    )

    seen = await desk.quoted(two_expiries)

    assert (seen.state, seen.money.broker_requirement_cents) == ("resolved", 0)
    assert (seen.at_expiry.estimated, seen.at_expiry.loss_cents, seen.at_expiry.gain_cents) == (False, None, None)
    assert (seen.at_expiry.loss_holds, seen.at_expiry.break_even_prices) == ([], [])
    assert len(seen.watched.ranges) == 2


# ---- The entry's own limits, judged as an entry judges them -------------------------


_GAP_OVER, _COST_OVER = "OPTION_STRUCTURE_LEG_GAP_OVER_LIMIT", "OPTION_STRUCTURE_COST_OVER_LIMIT"


async def test_a_leg_quoted_wider_than_the_owners_gap_limit_is_named_and_the_verdict_is_not_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 585 call at 3.90 by 4.30: forty cents on a mid of 4.10 is 9.76%, over the 8% an owner starts from.
    The 590 at 2.18 by 2.22 is 1.82%. Bought for $2.12, crossing both markets costs $44.00 of the $212.10 set
    aside, which is over 10% too."""
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("3.90", "4.30"), c590=("2.18", "2.22"))

    bought, sold = seen.legs
    assert (bought.number, sold.number) == (1, 2)
    assert (bought.gap_percent_of_mid, bought.over_gap_limit, sold.over_gap_limit) == (Decimal("9.76"), True, False)
    assert seen.gap_limit_percent == Decimal(8)
    assert "9.76%" in bought.gap_sentence and "8%" in bought.gap_sentence
    assert "1.82%" in sold.gap_sentence and bought.gap_sentence != sold.gap_sentence
    # Every reason an entry would be refused, in the order an entry asks: the wide leg, then the cost.
    assert [(refused.reason_code, refused.leg_ids) for refused in seen.entry_refusals] == [
        (_GAP_OVER, ["long_call"]), (_COST_OVER, []),
    ]
    by_gap, by_cost = seen.entry_refusals
    assert "Leg 1" in by_gap.sentence and "9.76%" in by_gap.sentence and "long_call" not in by_gap.sentence
    assert by_cost.sentence == seen.money.cost_sentence
    # A wide market passes. It is not a structure that cannot be deployed, and its figures are all shown.
    assert (seen.verdict.resolves_as_built, seen.verdict.red, seen.money.over_cost_limit) == (True, False, True)
    assert seen.money.net_entry_premium_cents == 21_200


async def test_the_gap_is_held_to_the_limit_the_owner_set_and_not_to_the_one_they_start_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four cents on a mid of 2.20 is 1.82%: inside 8%, and over a limit of 1.5%. On 4.10 it is 0.98%: inside both."""
    desk = Desk(monkeypatch)
    strict = _BULL_CALL_SPREAD.model_copy(update={"gap_limit_percent": Decimal("1.5")})

    seen = await desk.quoted(strict, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert [leg.over_gap_limit for leg in seen.legs] == [False, True]
    assert (seen.gap_limit_percent, seen.order_terms.gap_limit_percent) == (Decimal("1.5"), Decimal("1.5"))
    assert [(refused.reason_code, refused.leg_ids) for refused in seen.entry_refusals] == [(_GAP_OVER, ["short_call"])]
    assert "1.5%" in seen.legs[1].gap_sentence and "8%" not in seen.legs[1].gap_sentence


async def test_an_entry_that_nothing_here_would_refuse_is_said_apart_from_one_that_would_be_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)

    fine = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))
    costly = await Desk(monkeypatch).quoted(_BULL_CALL_SPREAD, c585=("4.98", "5.02"), c590=("4.88", "4.92"))

    assert fine.entry_refusals == [] and [refused.reason_code for refused in costly.entry_refusals] == [_COST_OVER]
    assert fine.entry_sentence and costly.entry_sentence and fine.entry_sentence != costly.entry_sentence
    # The budget, the account and the quotes of an entry's own moment are not asked here, and that is said.
    assert "budget" in fine.entry_sentence


async def test_picks_that_are_not_the_structure_built_are_an_entrys_refusal_too_under_the_verdicts_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    upside_down = _asking(
        _leg("long_call", "long", "call", _AT_THE_MONEY),
        _leg("short_call", "short", "call", _from("long_call", -5.0)),
        structure="bull_call_spread",
    )

    seen = await desk.quoted(upside_down)

    assert seen.verdict.red and seen.entry_refusals[0].reason_code == seen.verdict.resolves_reason_code
    assert sorted(seen.entry_refusals[0].leg_ids) == ["long_call", "short_call"]


async def test_an_answer_with_no_figures_names_no_entry_refusal_and_says_nothing_of_an_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await Desk(monkeypatch).preview(_BULL_CALL_SPREAD)

    assert seen.state != "resolved"
    assert (seen.entry_refusals, seen.entry_sentence, seen.decay) == ([], None, None)


# ---- What a day takes ---------------------------------------------------------------


def _decay_apart(leg: Any, *, at_ms: int, dividends: tuple[DividendOn, ...] = ()) -> float:
    """The pricer's own daily decay for one served leg, with every input written out again here: SPY's mid of
    585.11, the leg's own solved volatility, and the time from the read to the expiry day's close."""
    worked = american_daily_decay(
        spot=585.11,
        strike=float(leg.contract.strike),
        years_to_expiry=years_from_ms(session_close_ms_utc(FRIDAY) - at_ms),
        rate=0.045,
        volatility=leg.implied_volatility,
        right=leg.contract.right,
        dividends=tuple(CashDividend(years_from_ms(paid.ex_ms - at_ms), paid.cash) for paid in dividends),
    )
    assert worked is not None
    return worked.decay


def _to_the_cent(dollars: float) -> int:
    return int((Decimal(dollars) * 100).quantize(Decimal(1), rounding="ROUND_HALF_EVEN"))


async def test_a_bought_spread_shows_what_24_hours_take_from_it_bought_leg_less_sold_for_every_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    three = _BULL_CALL_SPREAD.model_copy(update={"structures": 3})

    seen = await desk.quoted(three, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    bought, sold = seen.legs
    # Each leg's figure is its own contract's, whichever side the leg takes: both calls lose value with a day.
    _close(bought.daily_decay, _decay_apart(bought, at_ms=NOW + 400))
    _close(sold.daily_decay, _decay_apart(sold, at_ms=NOW + 400))
    assert bought.daily_decay < sold.daily_decay < 0
    # Bought one and sold the other, a hundred shares, three structures.
    cents = _to_the_cent(3 * 100 * (bought.daily_decay - sold.daily_decay))
    decay = seen.decay
    assert (decay.estimated, decay.reason_code, decay.leg_ids) == (True, None, [])
    assert (decay.per_24h_cents, decay.direction, decay.per_24h_usd) == (cents, "loses", usd(-cents))
    assert cents < 0 and usd(-cents) in decay.sentence and "3 structures" in decay.sentence
    # It is per 24 hours by the clock, and a weekend is three of them. Both are said.
    assert "24 hours" in decay.sentence and "24 hours" in decay.assumptions_sentence
    assert "weekend" in decay.assumptions_sentence and "three" in decay.assumptions_sentence
    assert (decay.warning_sentence, decay.near_ex_date_sentence) == (None, None)


async def test_a_spread_that_was_sold_gains_with_a_day_and_is_said_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 585 call sold and the 590 bought: the sold call, nearer the money, loses more in a day than the bought."""
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread",
    )

    seen = await desk.quoted(received, c585=("5.00", "5.04"), c590=("1.89", "1.93"))

    sold, bought = seen.legs
    cents = _to_the_cent(100 * (bought.daily_decay - sold.daily_decay))
    assert cents > 0
    assert (seen.decay.per_24h_cents, seen.decay.direction, seen.decay.per_24h_usd) == (cents, "gains", usd(cents))
    assert usd(cents) in seen.decay.sentence and "-" not in seen.decay.per_24h_usd


async def test_with_dividends_unknown_no_decay_is_estimated_for_the_structure_or_shown_for_any_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    desk.inputs = _UNKNOWN

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    decay = seen.decay
    assert (seen.state, decay.estimated, decay.reason_code) == ("resolved", False, DecayNotEstimated.INPUTS_NOT_KNOWN)
    assert (decay.per_24h_cents, decay.per_24h_usd, decay.direction) == (None, None, None)
    assert decay.leg_ids == ["long_call", "short_call"] and [leg.daily_decay for leg in seen.legs] == [None, None]
    assert "SPY" in decay.sentence


async def test_one_leg_the_model_has_no_volatility_for_leaves_the_whole_decay_not_estimated_and_is_named_by_its_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 590 call quoted far above anything a volatility explains. A rule by strike still picks it."""
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("400.00", "400.04"))

    decay = seen.decay
    assert (decay.estimated, decay.reason_code, decay.leg_ids) == (False, DecayNotEstimated.NO_VOLATILITY, ["short_call"])
    assert decay.per_24h_cents is None
    assert "leg 2" in decay.sentence and "short_call" not in decay.sentence
    # The other leg's own figure is still its own. No sum of one leg is shown as the structure's.
    assert seen.legs[0].daily_decay < 0 and seen.legs[1].daily_decay is None
    assert seen.legs[1].delta_status_sentence != seen.legs[0].delta_status_sentence


_GOING_EX_TOMORROW = DividendOn(ex_ms=session_open_ms_utc(date(2026, 10, 7)), cash=1.75)


async def test_a_sold_call_whose_figure_counts_tomorrows_dividend_carries_a_warning_that_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPY goes ex $1.75 at tomorrow's open, inside the 24 hours. The sold 590 call falls by about that, which
    the figure counts as a gain. A sold call in the money is often assigned first."""
    desk = Desk(monkeypatch)
    desk.inputs = replace(_KNOWN, dividends=(_GOING_EX_TOMORROW,))

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert seen.decay.estimated
    _close(seen.legs[1].daily_decay, _decay_apart(seen.legs[1], at_ms=NOW + 400, dividends=(_GOING_EX_TOMORROW,)))
    assert "Leg 2" in seen.decay.warning_sentence and "short_call" not in seen.decay.warning_sentence
    assert "Leg 1" not in seen.decay.warning_sentence
    assert seen.decay.near_ex_date_sentence and "SPY" in seen.decay.near_ex_date_sentence


async def test_a_bought_call_ahead_of_that_dividend_is_told_the_figure_is_rough_and_has_no_sold_call_to_warn_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    desk.inputs = replace(_KNOWN, dividends=(_GOING_EX_TOMORROW,))
    bought_call = _asking(_leg("long_call", "long", "call", _AT_THE_MONEY), structure="long_call")

    seen = await desk.quoted(bought_call)

    assert (seen.decay.estimated, seen.decay.warning_sentence) == (True, None)
    assert seen.decay.near_ex_date_sentence is not None


async def test_a_dividend_more_than_three_days_off_brings_neither_note(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    in_ten_days = DividendOn(ex_ms=session_open_ms_utc(date(2026, 10, 16)), cash=1.75)
    desk.inputs = replace(_KNOWN, dividends=(in_ten_days,))

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (seen.decay.estimated, seen.decay.warning_sentence, seen.decay.near_ex_date_sentence) == (True, None, None)


# ---- Money a screen shows as it is, and the line to draw ---------------------------------


async def test_every_amount_of_cents_comes_with_the_same_amount_written_out_in_dollars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    money = seen.money
    assert (money.net_price_usd, money.premium_usd, money.premium_per_structure_usd) == ("$1.94", "$194.00", "$194.00")
    assert (money.premium_is, money.premium_is_label) == ("paid", "paid")
    assert money.premium_usd in money.premium_sentence and money.net_price_usd in money.premium_sentence
    assert (money.broker_requirement_usd, money.fee_estimate_usd) == (usd(0), usd(money.fee_estimate_cents))
    assert (money.entry_commitment_usd, money.round_trip_cost_usd) == (usd(19_410), usd(800))
    assert (seen.at_expiry.loss_usd, seen.at_expiry.gain_usd) == (usd(19_400), usd(30_600))
    # Level at the $194.00 lost up to 585, up to the $306.00 gained at 590, and level again: drawn from 583 to 592.
    assert [(point.price, point.result_cents) for point in seen.at_expiry.sketch] == [
        (Decimal(583), -19_400), (Decimal(585), -19_400), (Decimal(590), 30_600), (Decimal(592), 30_600),
    ]


async def test_a_premium_received_is_written_without_a_sign_and_said_to_be_received(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread",
    )

    seen = await desk.quoted(received.model_copy(update={"structures": 2}), c585=("5.00", "5.04"), c590=("1.89", "1.93"))

    money = seen.money
    assert (money.net_entry_premium_cents, money.premium_usd, money.premium_per_structure_usd) == (-61_400, "$614.00", "$307.00")
    assert (money.net_price_usd, money.premium_is, money.premium_is_label) == ("$3.07", "received", "received")
    assert "received" in money.premium_sentence and "paid" not in money.premium_sentence
    assert all(said in money.premium_sentence for said in ("$614.00", "$307.00", "$3.07", "2 structures"))
    assert money.broker_requirement_usd == "$1,000.00"


async def test_legs_with_two_expiries_have_no_line_to_draw_and_no_amounts_at_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    next_friday = {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(scenes._NEXT_FRIDAY)}
    two_expiries = _asking(
        _leg("near", "long", "call", _AT_THE_MONEY), _leg("far", "long", "call", _AT_THE_MONEY, next_friday)
    )

    seen = await desk.quoted(two_expiries)

    assert (seen.at_expiry.estimated, seen.at_expiry.sketch) == (False, [])
    assert (seen.at_expiry.loss_usd, seen.at_expiry.gain_usd) == (None, None)
    # Each leg still has a day's decay of its own: nothing about that needs one expiry.
    assert seen.decay.estimated


# ---- What was asked, said back; and how long the figures stand -----------------------------


async def test_every_answer_says_each_leg_back_by_its_place_with_its_rules_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)

    waiting = await desk.preview(_BULL_CALL_SPREAD)
    resolved = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))
    refused = await desk.preview(_asking(_leg("short_call", "short", "call", _AT_THE_MONEY)))

    for seen in (waiting, resolved):
        first, second = seen.legs_asked
        assert (first.leg_id, first.number, first.side_label) == ("long_call", 1, "Buy")
        assert (second.leg_id, second.number, second.side_label) == ("short_call", 2, "Sell")
        assert "$5" in second.sentence and "leg 1" in second.sentence and "long_call" not in second.sentence
        assert "Oct 9 '26" in first.sentence
    assert waiting.state not in {"resolved", "plan_refused"}
    assert (resolved.state, refused.state) == ("resolved", "plan_refused")
    assert [(leg.number, leg.side_label) for leg in refused.legs_asked] == [(1, "Sell")]


async def test_a_structure_is_named_as_a_list_writes_it_and_as_a_sentence_takes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    custom = _asking(_leg("only", "long", "call", _AT_THE_MONEY))
    condor = _as_the_builder_starts(NamedStructure.IRON_CONDOR)

    named, unnamed, with_an = [await desk.preview(asked) for asked in (_BULL_CALL_SPREAD, custom, condor)]

    assert (named.structure_label, named.structure_in_a_sentence) == ("Bull call spread", "a bull call spread on")
    assert (unnamed.structure, unnamed.structure_label) == (None, "Custom")
    assert unnamed.structure_in_a_sentence.endswith(" on") and unnamed.structure_in_a_sentence != named.structure_in_a_sentence
    assert with_an.structure_in_a_sentence == "an iron condor on"


async def test_figures_are_good_for_as_long_as_a_pages_request_for_quotes_stands_and_only_figures_carry_that(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)

    waiting = await desk.preview(_BULL_CALL_SPREAD)
    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert FIGURES_GOOD_FOR_MS == PREVIEW_LEASE_MS == 60_000
    assert (seen.figures_good_for_ms, seen.figures_good_until_ms) == (60_000, seen.quotes_read_ms + 60_000)
    assert seen.figures_stale_sentence and seen.quotes_read_sentence
    assert (waiting.figures_good_for_ms, waiting.figures_good_until_ms, waiting.figures_stale_sentence) == (None, None, None)
    assert waiting.quotes_read_sentence is None


async def test_what_is_read_together_is_served_together_or_not_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The feed's generation, the underlying's quote, the money and how far the search reached come from one
    reading of the supervisor. A page that keys on one of them finds them all."""
    desk = Desk(monkeypatch)

    before = await desk.preview(_BULL_CALL_SPREAD)
    after = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (before.feed_generation, before.underlying_quote, before.the_money, before.watched) == (None, None, None, None)
    assert None not in (after.feed_generation, after.underlying_quote, after.the_money, after.watched)
    assert after.watched.sentence and all(reached.sentence for reached in after.watched.ranges)
    assert "583" in after.watched.ranges[0].sentence and "592" in after.watched.ranges[0].sentence


# ---- The owner's own order terms ------------------------------------------------------------


_EXPIRING_TODAY = _asking(
    _leg("long_call", "long", "call", _AT_THE_MONEY, {"selector": "same_day"}), structure="long_call"
)


async def test_a_contract_is_picked_under_the_entry_cutoff_the_owners_own_close_lead_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call expiring today, asked about at 10:31:02. Closed 45 minutes before the 16:00 close, an entry on it
    can still be seen through. Closed 330 minutes before, at 10:30, the entry cutoff passed at 10:28:40."""
    late_enough = await Desk(monkeypatch).quoted(_EXPIRING_TODAY)
    too_late = await Desk(monkeypatch).quoted(_EXPIRING_TODAY.model_copy(update={"expiry_close_lead_minutes": 330}))

    assert (late_enough.state, late_enough.order_terms.expiry_close_lead_minutes) == ("resolved", 45)
    assert (too_late.state, too_late.reason_code) == ("not_resolved", "OPTION_CONTRACT_PAST_ENTRY_CUTOFF")
    assert too_late.order_terms.expiry_close_lead_minutes == 330


async def test_the_entry_wait_counts_toward_that_cutoff_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed 328 minutes before the close, at 10:32. An entry needs its wait, 30 seconds to prove a cancel and
    30 more to spare before that. With a wait of 1 second the cutoff is 10:30:59, already past at 10:31:02.
    The longest wait there is, 300 seconds, is later still. So one more minute of lead is what it turns on:
    327 minutes with a wait of 1 second is cut off at 10:31:59, not yet reached."""
    passed = _EXPIRING_TODAY.model_copy(update={"expiry_close_lead_minutes": 328, "entry_wait_seconds": 1})
    not_yet = _EXPIRING_TODAY.model_copy(update={"expiry_close_lead_minutes": 327, "entry_wait_seconds": 1})
    passed_by_the_wait = _EXPIRING_TODAY.model_copy(update={"expiry_close_lead_minutes": 327, "entry_wait_seconds": 60})

    seen = [await Desk(monkeypatch).quoted(asked) for asked in (passed, not_yet, passed_by_the_wait)]

    assert [answer.state for answer in seen] == ["not_resolved", "resolved", "not_resolved"]
    assert seen[2].reason_code == "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    assert (seen[2].order_terms.entry_wait_seconds, seen[1].order_terms.entry_wait_seconds) == (60, 1)


async def test_where_the_owners_close_falls_against_alpacas_hour_is_said_three_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)

    said = []
    for minutes in (45, 60, 90):
        seen = await desk.preview(_BULL_CALL_SPREAD.model_copy(update={"expiry_close_lead_minutes": minutes}))
        said.append(seen.order_terms.expiry_close_sentence)

    assert all(str(minutes) in words for minutes, words in zip((45, 60, 90), said, strict=True))
    # Inside the hour, as it begins, and before it: three things said, whatever the number in each.
    assert len({words.replace(str(minutes), "N") for minutes, words in zip((45, 60, 90), said, strict=True)}) == 3


async def test_how_the_orders_go_is_said_in_one_line_with_the_owners_own_gap_and_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _BULL_CALL_SPREAD.model_copy(update={"gap_limit_percent": Decimal("6.5"), "entry_wait_seconds": 45})

    seen = await Desk(monkeypatch).preview(asked)

    assert "6.5%" in seen.order_terms.orders_sentence and "45 s" in seen.order_terms.orders_sentence
    assert "8%" not in seen.order_terms.orders_sentence and "20 s" not in seen.order_terms.orders_sentence


# ---- A leg is named by its place, never by its id --------------------------------------------


def _every_sentence(seen: OptionStructurePreview) -> list[str]:
    """Every sentence the answer carries, wherever it sits."""
    found: list[str] = []

    def walk(part: Any, name: str = "") -> None:
        if isinstance(part, dict):
            for key, value in part.items():
                walk(value, key)
        elif isinstance(part, list):
            for value in part:
                walk(value, name)
        elif isinstance(part, str) and (name.endswith("sentence") or name == "notice"):
            found.append(part)

    walk(seen.model_dump(mode="json"))
    return found


def _names_no_leg_by_its_id(seen: OptionStructurePreview, *leg_ids: str) -> bool:
    return not any(f"'{leg_id}'" in said or f" {leg_id} " in said for said in _every_sentence(seen) for leg_id in leg_ids)


# Written second and named first in the alphabet: a place is not an order of names.
_SECOND, _FIRST = "alpha", "zulu"


async def test_a_plans_own_refusal_names_legs_by_their_places(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each leg measures its strike from the other."""
    in_a_loop = _asking(
        _leg(_FIRST, "long", "call", _from(_SECOND, -5.0)), _leg(_SECOND, "short", "call", _from(_FIRST, 5.0))
    )

    seen = await Desk(monkeypatch).preview(in_a_loop)

    assert (seen.state, seen.reason_code) == ("plan_refused", "OPTION_PREVIEW_PLAN_REFUSED")
    assert "leg 1" in seen.notice and "leg 2" in seen.notice
    assert _names_no_leg_by_its_id(seen, _FIRST, _SECOND)


async def test_a_resolvers_refusal_names_the_leg_by_its_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second leg's contract never gets a quote."""
    desk = Desk(monkeypatch)
    spread = _asking(_leg(_FIRST, "long", "call", _AT_THE_MONEY), _leg(_SECOND, "short", "call", _from(_FIRST, 5.0)))
    await desk.watching(spread)
    desk.quote("call", 585, "4.08", "4.12")
    desk.ibkr.now += ARRIVAL_GRACE_MS + 1
    desk.spy()
    desk.quote("call", 585, "4.08", "4.12")

    seen = await desk.preview(spread)

    assert (seen.state, seen.reason_code, seen.leg_ids) == ("not_resolved", "OPTION_CONTRACT_SEARCH_INCOMPLETE", [_SECOND])
    assert "Leg 2" in seen.notice or "leg 2" in seen.notice
    assert _names_no_leg_by_its_id(seen, _FIRST, _SECOND)


async def test_a_shape_that_is_not_the_one_named_names_its_legs_by_their_places(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    upside_down = _asking(
        _leg(_FIRST, "long", "call", _AT_THE_MONEY),
        _leg(_SECOND, "short", "call", _from(_FIRST, -5.0)),
        structure="bull_call_spread",
    )
    one_strike = _asking(
        _leg(_FIRST, "long", "call", _AT_THE_MONEY), _leg(_SECOND, "short", "call", _from(_FIRST, 0.0))
    )

    reversed_, same = await desk.quoted(upside_down), await Desk(monkeypatch).quoted(one_strike)

    assert reversed_.verdict.red and same.verdict.red
    assert "leg 1" in reversed_.verdict.resolves_sentence and "leg 2" in reversed_.verdict.resolves_sentence
    # The check names the pair by their ids' order, zulu after alpha. Said by place, a pair is in its places' order.
    assert "legs 1 and 2" in same.verdict.resolves_sentence
    assert sorted(reversed_.verdict.leg_ids) == [_SECOND, _FIRST]
    assert _names_no_leg_by_its_id(reversed_, _FIRST, _SECOND) and _names_no_leg_by_its_id(same, _FIRST, _SECOND)


async def test_a_leg_with_no_delta_is_named_by_its_place_where_the_lean_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    spread = _asking(_leg(_FIRST, "long", "call", _AT_THE_MONEY), _leg(_SECOND, "short", "call", _from(_FIRST, 5.0)))

    seen = await desk.quoted(spread, c585=("4.08", "4.12"), c590=("400.00", "400.04"))

    assert seen.lean is None and "leg 2" in seen.lean_unknown_sentence
    assert _names_no_leg_by_its_id(seen, _FIRST, _SECOND)


@pytest.mark.parametrize("name", list(NamedStructure), ids=lambda name: name.value)
async def test_no_sentence_of_a_starting_structures_preview_names_a_leg_by_its_id(
    monkeypatch: pytest.MonkeyPatch, name: NamedStructure
) -> None:
    desk = Desk(monkeypatch, reach=6)
    asked = _as_the_builder_starts(name)

    seen = await desk.quoted(asked)

    assert seen.state in {"resolved", "not_resolved"}
    assert _names_no_leg_by_its_id(seen, *(leg.leg_id for leg in asked.legs))


async def test_two_legs_given_one_id_are_refused_by_their_places_and_ask_for_no_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    twice = _asking(
        _leg("same", "long", "call", _AT_THE_MONEY), _leg("other", "long", "put", _AT_THE_MONEY),
        _leg("same", "long", "call", {"selector": "atm_offset", "offset": 2}),
    )

    seen = await desk.preview(twice)

    assert (seen.state, seen.reason_code, seen.leg_ids) == ("plan_refused", "OPTION_PREVIEW_LEGS_SHARE_AN_ID", ["same"])
    assert "1 and 3" in seen.notice and "'same'" not in seen.notice
    assert (seen.verdict.can_be_deployed, seen.verdict.red) == (False, True)
    assert desk.watch.requests() == ()


# ---- The verdict ------------------------------------------------------------------


async def test_a_structure_the_plans_rules_refuse_is_a_verdict_and_asks_for_no_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    nothing_covers_it = _asking(_leg("short_call", "short", "call", _AT_THE_MONEY))

    seen = await desk.preview(nothing_covers_it)

    assert (seen.state, seen.reason_code, seen.ask_again_in_ms) == ("plan_refused", "OPTION_PREVIEW_PLAN_REFUSED", None)
    assert (seen.verdict.can_be_deployed, seen.verdict.resolves_as_built, seen.verdict.red) == (False, None, True)
    assert seen.verdict.deploy_reason_code == seen.reason_code and seen.notice in seen.verdict.deploy_sentence
    assert (seen.legs, seen.money, seen.at_expiry, seen.lean, seen.watched) == ([], None, None, None, None)
    assert desk.watch.requests() == ()


@pytest.mark.parametrize(
    "request_",
    [
        pytest.param(_asking(), id="no leg at all"),
        pytest.param(_asking(*(_leg(f"leg_{n}", "long", "call", _AT_THE_MONEY) for n in range(5))), id="a fifth leg"),
        pytest.param(
            _asking(_leg("a", "long", "call", _AT_THE_MONEY, ratio=2), _leg("b", "long", "put", _AT_THE_MONEY, ratio=2)),
            id="ratios not in lowest terms",
        ),
        pytest.param(_asking(_leg("only", "long", "put", _AT_THE_MONEY), structure="long_call"), id="a name its legs are not"),
        pytest.param(_asking(_leg("Not An Id", "long", "call", _AT_THE_MONEY)), id="a leg id the plan cannot carry"),
        pytest.param(
            _asking(_leg("only", "long", "call", _AT_THE_MONEY), underlying="BRK.B"),
            id="a ticker with a share class, whose contracts are written another way",
        ),
    ],
)
async def test_every_hard_rule_of_the_plan_is_answered_as_a_verdict_never_as_an_error(
    monkeypatch: pytest.MonkeyPatch, request_: OptionStructurePreviewRequest
) -> None:
    seen = await Desk(monkeypatch).preview(request_)

    assert (seen.state, seen.verdict.can_be_deployed, seen.verdict.red) == ("plan_refused", False, True)
    assert seen.notice


async def test_a_plan_that_stops_being_deployable_gives_up_the_lines_it_held(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    await desk.quoted(_BULL_CALL_SPREAD)
    held = desk.ibkr.open_option_lines()

    await desk.preview(_asking(_leg("short_call", "short", "call", _AT_THE_MONEY)))
    await desk.settle()

    assert len(held) == 10
    assert desk.ibkr.open_option_lines() == set()


async def test_rules_that_land_on_another_shape_than_the_one_named_are_a_red_verdict_that_names_the_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bull call spread sells the higher call. With the sold leg $5 below the bought one it is another structure."""
    desk = Desk(monkeypatch)
    upside_down = _asking(
        _leg("long_call", "long", "call", _AT_THE_MONEY),
        _leg("short_call", "short", "call", _from("long_call", -5.0)),
        structure="bull_call_spread",
    )

    seen = await desk.quoted(upside_down)

    assert seen.state == "resolved"
    assert (seen.verdict.can_be_deployed, seen.verdict.resolves_as_built, seen.verdict.red) == (True, False, True)
    assert seen.verdict.resolves_reason_code == "OPTION_STRUCTURE_STRIKES_NOT_AS_NAMED"
    assert sorted(seen.verdict.leg_ids) == ["long_call", "short_call"]
    # What it came to is still shown, so the owner can see why.
    assert [leg.contract.strike for leg in seen.legs] == [Decimal(585), Decimal(580)]


async def test_two_legs_that_land_on_one_contract_are_shown_and_have_no_money(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    one_strike = _asking(
        _leg("long_call", "long", "call", _AT_THE_MONEY),
        _leg("short_call", "short", "call", _from("long_call", 0.0)),
        structure="bull_call_spread",
    )

    seen = await desk.quoted(one_strike)

    assert (seen.state, seen.verdict.resolves_as_built) == ("resolved", False)
    assert seen.verdict.resolves_reason_code == "OPTION_STRUCTURE_SAME_CONTRACT"
    assert [leg.contract.occ_symbol for leg in seen.legs] == ["SPY261009C00585000"] * 2
    assert (seen.money, seen.at_expiry, seen.lean) == (None, None, None)


# ---- Each way there is nothing to show ---------------------------------------------


def _shows_no_figure(seen: OptionStructurePreview) -> bool:
    return (seen.legs, seen.money, seen.at_expiry, seen.lean) == ([], None, None, None)


async def test_a_process_with_no_ibkr_source_says_so_and_asks_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    watch = OptionWatchList()

    seen = await preview_option_structure(
        _BULL_CALL_SPREAD,
        quotes=None,
        watch=watch,
        model_inputs=lambda *_: _KNOWN,
        searches=searches_in_this_process(),
        strikes_each_side=2,
    )

    assert (seen.state, seen.reason_code, seen.ask_again_in_ms) == ("no_quote_source", "OPTION_PREVIEW_NO_QUOTE_SOURCE", None)
    assert _shows_no_figure(seen) and watch.requests() == ()
    # Whether it can be deployed at all does not wait for quotes.
    assert (seen.verdict.can_be_deployed, seen.verdict.resolves_as_built, seen.verdict.red) == (True, None, False)


async def test_a_feed_that_is_not_up_is_named_and_the_window_stays_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    desk.ibkr.client.is_connected.return_value = False
    await desk.settle()

    seen = await desk.preview(_BULL_CALL_SPREAD)

    assert (seen.state, seen.reason_code) == ("feed_not_up", "OPTION_PREVIEW_FEED_NOT_UP")
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is not None
    assert [asked.line_class for asked in desk.watch.requests()] == [LineClass.PREVIEW]


async def test_the_first_asks_find_the_listing_unread_and_then_quotes_still_arriving(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    # The Clerk's supervisor is already running, connected, with nothing to watch.
    await desk.settle()

    first = await desk.preview(_BULL_CALL_SPREAD)
    await desk.settle()
    desk.spy()
    await desk.settle()
    lines_open = await desk.preview(_BULL_CALL_SPREAD)
    desk.quote("call", 585, "4.08", "4.12")
    one_leg_quoted = await desk.preview(_BULL_CALL_SPREAD)

    assert (first.state, first.reason_code) == ("listing_not_read", "OPTION_PREVIEW_LISTING_NOT_READ")
    assert (lines_open.state, lines_open.reason_code) == ("quotes_arriving", "OPTION_PREVIEW_QUOTES_ARRIVING")
    assert one_leg_quoted.state == "quotes_arriving"
    assert one_leg_quoted.contract.occ_symbol == "SPY261009C00590000"
    for seen in (first, lines_open, one_leg_quoted):
        assert _shows_no_figure(seen) and seen.ask_again_in_ms is not None
    # How far the search reaches is already known while the quotes arrive.
    assert (lines_open.watched.lines_wanted, lines_open.watched.lines_held) == (10, 10)


async def test_a_quote_that_never_comes_stops_being_called_arriving_and_is_the_resolvers_own_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    await desk.watching(_BULL_CALL_SPREAD)
    desk.quote("call", 585, "4.08", "4.12")

    desk.ibkr.now += ARRIVAL_GRACE_MS
    desk.spy()
    desk.quote("call", 585, "4.08", "4.12")
    still_arriving = await desk.preview(_BULL_CALL_SPREAD)
    desk.ibkr.now += 1
    seen = await desk.preview(_BULL_CALL_SPREAD)

    assert still_arriving.state == "quotes_arriving"
    assert (seen.state, seen.reason_code) == ("not_resolved", "OPTION_CONTRACT_SEARCH_INCOMPLETE")
    assert (seen.leg_ids, seen.contract.occ_symbol) == (["short_call"], "SPY261009C00590000")
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is None


async def test_an_underlying_with_no_quote_is_arriving_at_first_and_then_named_as_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    await desk.preview(_BULL_CALL_SPREAD)
    await desk.settle()

    at_first = await desk.preview(_BULL_CALL_SPREAD)
    desk.ibkr.now += ARRIVAL_GRACE_MS + 1
    never_came = await desk.preview(_BULL_CALL_SPREAD)
    desk.spy()
    desk.ibkr.now += 5_001
    went_stale = await desk.preview(_BULL_CALL_SPREAD)

    assert at_first.state == "quotes_arriving"
    assert (never_came.state, never_came.reason_code, never_came.fault) == (
        "underlying_quote_unusable", "OPTION_CONTRACT_UNDERLYING_QUOTE_UNUSABLE", None,
    )
    assert (went_stale.state, went_stale.fault) == ("underlying_quote_unusable", "QUOTE_STALE")
    assert _shows_no_figure(never_came) and _shows_no_figure(went_stale)
    # A quote may yet come, or come fresh: the page asks again. The two are told apart in words too.
    assert never_came.ask_again_in_ms is not None and went_stale.ask_again_in_ms is not None
    assert "QUOTE_STALE" in went_stale.notice and "QUOTE_STALE" not in never_came.notice


async def test_a_faulty_quote_is_the_resolvers_refusal_with_its_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 590 call's bid is above its ask. It is the only strike a distance rule of no tolerance may pick."""
    desk = Desk(monkeypatch)

    seen = await desk.quoted(_BULL_CALL_SPREAD, c590=("2.30", "2.22"))

    assert (seen.state, seen.reason_code, seen.fault) == ("not_resolved", "OPTION_CONTRACT_NO_USABLE_QUOTE", "QUOTE_CROSSED")
    assert (seen.leg_ids, seen.contract.strike) == (["short_call"], Decimal(590))
    assert _shows_no_figure(seen)


async def test_a_preview_gets_no_line_when_none_is_spare_shows_no_figure_and_takes_none_from_a_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget of five, and a running bot's window of five calls. The preview wants puts."""
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", _AT_THE_MONEY), feed.MONEY)
    desk = Desk(monkeypatch, bot, budget=5)
    puts = _asking(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")
    await desk.watching(puts)
    bots_lines = desk.ibkr.open_option_lines()
    desk.quote_every_line()

    seen = await desk.preview(puts)

    assert (seen.state, seen.reason_code) == ("no_quote_line", "OPTION_LINE_OVER_BUDGET")
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is None
    assert (seen.watched.lines_wanted, seen.watched.lines_held, seen.watched.lines_refused) == (5, 0, 5)
    assert (seen.watched.line_budget, seen.watched.lines_open) == (5, 5)
    assert bots_lines == feed._symbols(583, 584, 585, 586, 587) == desk.ibkr.open_option_lines()
    assert desk.ibkr.cancelled == []


async def test_a_pick_that_was_refused_a_line_is_no_line_to_spare_though_other_strikes_have_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot's thirteen calls and a budget of fifteen leave a preview two lines: the 585 and 584 puts. Its rule
    asks for a put near 0.30 delta, further down. The 583 put it would look at next was refused a line."""
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", _AT_THE_MONEY), feed.MONEY)
    desk = Desk(monkeypatch, bot, budget=15, reach=6)
    by_delta = _asking(_leg("long_put", "long", "put", {"selector": "delta", "target": 0.30}), structure="long_put")

    seen = await desk.quoted(by_delta)

    assert (seen.state, seen.reason_code) == ("no_quote_line", "OPTION_LINE_OVER_BUDGET")
    assert seen.contract.strike == Decimal(583)
    assert (seen.watched.lines_wanted, seen.watched.lines_held, seen.watched.lines_refused) == (13, 2, 11)
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is None


async def test_a_clerk_with_no_line_budget_says_so_at_the_first_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch, budget=0)
    await desk.settle()

    seen = await desk.preview(_BULL_CALL_SPREAD)

    assert (seen.state, seen.reason_code) == ("no_quote_line", "OPTION_LINE_OVER_BUDGET")
    assert _shows_no_figure(seen)


async def test_a_line_ibkr_refuses_for_capacity_is_said_to_be_ibkrs_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Clerk's own budget has room, and IBKR's allowance has none: another session is using it."""
    desk = Desk(monkeypatch)
    desk.ibkr.allowance = 0
    puts = _asking(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")
    await desk.watching(puts)
    await desk.settle()

    seen = await desk.preview(puts)

    assert (seen.state, seen.reason_code) == ("no_quote_line", "OPTION_LINE_NO_CAPACITY")
    assert _shows_no_figure(seen)


# ---- Dividends that are not known --------------------------------------------------


async def test_with_dividends_unknown_a_rule_by_delta_picks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch, reach=10)
    desk.inputs = _UNKNOWN

    seen = await desk.quoted(_CALL_NEAR_30_DELTA)

    assert (seen.state, seen.reason_code) == ("pricing_inputs_unknown", "OPTION_PREVIEW_DIVIDENDS_UNKNOWN")
    # Asking again a second later would learn nothing new: the lookup failed, and is tried again in its own time.
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is None
    assert (seen.pricing_inputs.dividends_known, seen.pricing_inputs.dividends) == (False, [])
    assert _UNKNOWN.dividends_source in seen.notice


async def test_with_dividends_unknown_other_rules_still_pick_and_no_delta_or_lean_is_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    desk.inputs = _UNKNOWN

    seen = await desk.quoted(_BULL_CALL_SPREAD, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (seen.state, seen.money.net_entry_premium_cents, seen.at_expiry.gain_cents) == ("resolved", 19_400, 30_600)
    assert [(leg.delta, leg.implied_volatility, leg.delta_status) for leg in seen.legs] == [(None, None, "inputs_not_known")] * 2
    assert seen.lean is None and seen.lean_unknown_sentence
    assert seen.pricing_inputs.dividends_known is False


async def test_on_a_fleet_clerk_a_rule_by_delta_is_previewed_with_the_dividends_the_coordinator_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Clerk holds no Polygon key. It asks the coordinator, and the dividend it is told of moves the delta."""
    desk = Desk(monkeypatch, reach=10)
    going_ex_thursday = DividendOn(ex_ms=session_open_ms_utc(date(2026, 10, 8)), cash=1.80)
    read = replace(fleet.READ, rate=0.045, dividends=(going_ex_thursday,))
    coordinator = fleet.Coordinator(monkeypatch, read)
    # The reader the Clerk's own route hands a preview.
    monkeypatch.setattr(desk, "_model_inputs", option_structure_panel._model_inputs)

    seen = await desk.quoted(_CALL_NEAR_30_DELTA)

    (leg,) = seen.legs
    assert (seen.state, seen.reason_code) == ("resolved", None)
    inputs = seen.pricing_inputs
    assert (inputs.rate, inputs.rate_is_default, inputs.dividends_known) == (0.045, False, True)
    assert [(paid.ex_ms, paid.cash_per_share) for paid in inputs.dividends] == [(going_ex_thursday.ex_ms, 1.80)]
    assert (inputs.dividends_source, inputs.dividends_read_ms) == (read.dividends_source, read.dividends_read_ms)
    quoted = (leg.contract.right, int(leg.contract.strike), str(leg.quote.bid), str(leg.quote.ask))
    with_the_dividend = _our_delta(*quoted, at_ms=NOW + 400, dividends=(going_ex_thursday,))
    assert leg.delta == with_the_dividend
    assert abs(with_the_dividend - _our_delta(*quoted, at_ms=NOW + 400)) > 1e-3
    # One ask of the coordinator, by a reader that does not wait, and no vendor asked from the Clerk.
    assert coordinator.asked == [("SPY", NOW + 400, FRIDAY, False)]
    assert coordinator.vendors_asked == []


async def test_on_a_fleet_clerk_that_cannot_reach_the_coordinator_a_rule_by_delta_picks_nothing_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch, reach=10)
    coordinator = fleet.Coordinator(monkeypatch)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    coordinator.answer = unreachable
    monkeypatch.setattr(desk, "_model_inputs", option_structure_panel._model_inputs)

    seen = await desk.quoted(_CALL_NEAR_30_DELTA)

    assert (seen.state, seen.reason_code) == ("pricing_inputs_unknown", "OPTION_PREVIEW_DIVIDENDS_UNKNOWN")
    assert _shows_no_figure(seen)
    inputs = seen.pricing_inputs
    # Unknown, never none declared; and the rate is the default, marked as the default.
    assert (inputs.dividends_known, inputs.dividends, inputs.rate_is_default) == (False, [], True)
    # The reason is the one the Clerk's own read gives for a coordinator it could not reach, and the notice says it.
    as_read = option_structure_panel._model_inputs("SPY", NOW + 400, FRIDAY)
    assert as_read.dividends_unknown_why == "coordinator_unreachable"
    assert inputs.dividends_source == as_read.dividends_source
    assert as_read.dividends_source in seen.notice
    assert coordinator.vendors_asked == []


async def test_while_another_ask_is_looking_the_rate_and_dividends_up_this_one_says_so_and_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    desk.inputs = ModelInputsBeingRead("another ask is looking them up")

    seen = await desk.quoted(_BULL_CALL_SPREAD)

    assert (seen.state, seen.reason_code) == ("pricing_inputs_unknown", "OPTION_PREVIEW_PRICING_INPUTS_NOT_READ")
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is not None


async def test_a_lookup_that_takes_too_long_is_answered_as_not_read_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup goes on in its own thread. The page is answered, and asks again."""
    desk = Desk(monkeypatch)
    answered = threading.Event()

    def slow(_underlying: str, _at_ms: int, _latest_expiry: date) -> ModelInputsRead:
        answered.wait(timeout=10)
        return _KNOWN

    monkeypatch.setattr(desk, "_model_inputs", slow)
    monkeypatch.setattr(option_structure_preview, "MODEL_INPUTS_WAIT_S", 0.05)
    try:
        seen = await desk.quoted(_BULL_CALL_SPREAD)
    finally:
        answered.set()

    assert (seen.state, seen.reason_code) == ("pricing_inputs_unknown", "OPTION_PREVIEW_PRICING_INPUTS_NOT_READ")
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is not None


async def test_only_so_many_previews_wait_on_a_slow_lookup_at_once_and_one_more_is_told_at_once_to_ask_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a fleet Clerk a preview is worked on the capacity kept for commands. Pages' waits on a slow lookup are
    bounded, so they cannot take that capacity up between them."""
    desk = Desk(monkeypatch)
    monkeypatch.setattr(option_structure_preview, "MAX_PREVIEWS_WAITING_ON_INPUTS", 2)
    answered = threading.Event()
    looked_up: list[str] = []

    def slow(underlying: str, _at_ms: int, _latest_expiry: date) -> ModelInputsRead:
        looked_up.append(underlying)
        answered.wait(timeout=10)
        return _KNOWN

    await desk.quoted(_BULL_CALL_SPREAD)
    monkeypatch.setattr(desk, "_model_inputs", slow)
    waiting = [asyncio.create_task(desk.preview(_BULL_CALL_SPREAD)) for _ in range(2)]
    try:
        await _until(lambda: len(looked_up) == 2)
        # Far inside the seconds an ask may wait: this one is answered without waiting at all.
        one_more = await asyncio.wait_for(desk.preview(_BULL_CALL_SPREAD), timeout=1)
        looked_up_while_two_waited = len(looked_up)
    finally:
        answered.set()
    await asyncio.gather(*waiting)
    once_they_are_done = await desk.preview(_BULL_CALL_SPREAD)

    assert (one_more.state, one_more.reason_code) == ("pricing_inputs_unknown", "OPTION_PREVIEW_PRICING_INPUTS_NOT_READ")
    assert _shows_no_figure(one_more) and one_more.ask_again_in_ms is not None
    assert looked_up_while_two_waited == 2
    # The two that waited gave their places back.
    assert once_they_are_done.state == "resolved"


# ---- The lease ------------------------------------------------------------------------


async def test_a_page_that_stops_asking_gives_its_lines_up_after_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    await desk.quoted(_BULL_CALL_SPREAD)
    asked_at = desk.ibkr.now

    desk.ibkr.now = asked_at + PREVIEW_LEASE_MS - 1
    await desk.settle()
    just_inside = desk.ibkr.open_option_lines()
    desk.ibkr.now = asked_at + PREVIEW_LEASE_MS
    await desk.settle()

    assert len(just_inside) == 10
    assert desk.ibkr.open_option_lines() == set()
    assert desk.watch.requests() == ()


async def test_each_ask_renews_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    await desk.quoted(_BULL_CALL_SPREAD)

    for _ in range(3):
        desk.ibkr.now += PREVIEW_LEASE_MS - 1_000
        await desk.preview(_BULL_CALL_SPREAD)
        await desk.settle()

    assert len(desk.ibkr.open_option_lines()) == 10


async def test_one_more_page_than_may_hold_a_window_takes_the_one_asked_for_longest_ago(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page that was closed stops asking, and cannot say so. The one asked for longest ago is likeliest gone."""
    desk = Desk(monkeypatch)
    for page in range(MAX_PREVIEW_WINDOWS + 1):
        desk.ibkr.now += 1_000
        newest = await desk.preview(_BULL_CALL_SPREAD.model_copy(update={"preview_key": f"page-{page:04d}"}))

    assert desk.watch.on_lease() == tuple(f"preview:page-{page:04d}" for page in range(1, MAX_PREVIEW_WINDOWS + 1))
    assert newest.reason_code != "OPTION_PREVIEW_TOO_MANY_OPEN"


async def test_a_fifth_page_that_keeps_asking_is_told_too_many_are_open_and_turns_nobody_else_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five builders open, each asking every second. One gives way once. It is told so each time it asks, the
    other four keep the windows they had, and their quotes stop being called "arriving" when ten seconds are up."""
    desk = Desk(monkeypatch)
    pages = [_BULL_CALL_SPREAD.model_copy(update={"preview_key": f"page-{page:04d}"}) for page in range(5)]
    for page in pages:
        desk.ibkr.now += 100
        await desk.preview(page)
    holding = desk.watch.on_lease()

    answers: list[OptionStructurePreview] = []
    for _ in range(ARRIVAL_GRACE_MS // 1_000 + 2):
        desk.ibkr.now += 1_000
        await desk.settle(passes=1)
        answers = [await desk.preview(page) for page in pages]
        assert sorted(desk.watch.on_lease()) == sorted(holding)

    gave_way, *held = answers
    assert holding == tuple(f"preview:page-{page:04d}" for page in range(1, 5))
    assert (gave_way.state, gave_way.reason_code) == ("no_quote_line", "OPTION_PREVIEW_TOO_MANY_OPEN")
    assert _shows_no_figure(gave_way) and gave_way.ask_again_in_ms is None
    assert str(MAX_PREVIEW_WINDOWS) in gave_way.notice
    # No quote ever came for SPY here. After ten seconds the four are told that, not that quotes are arriving.
    assert {seen.state for seen in held} == {"underlying_quote_unusable"}


async def test_the_page_that_gave_way_is_let_in_again_when_another_stops_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    pages = [_BULL_CALL_SPREAD.model_copy(update={"preview_key": f"page-{page:04d}"}) for page in range(5)]
    for page in pages:
        desk.ibkr.now += 100
        await desk.preview(page)

    # The second page goes away. The others ask on for a minute.
    for _ in range(PREVIEW_LEASE_MS // 10_000):
        desk.ibkr.now += 10_000
        told = [await desk.preview(page) for page in (pages[0], *pages[2:])]

    assert told[0].state != "no_quote_line"
    assert desk.watch.on_lease().count("preview:page-0000") == 1 and "preview:page-0001" not in desk.watch.on_lease()


# ---- No line spare for a page -------------------------------------------------------------


def _on_qqq(*legs: dict, **request: Any) -> OptionStructurePreviewRequest:
    return _asking(*legs, underlying="QQQ", **request)


async def test_a_preview_asks_ibkr_for_nothing_while_every_line_is_taken_by_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget of five, all of it a bot's window on SPY. A page asks about QQQ: no QQQ quote is asked for and
    no QQQ chain is read, and the page is told there is no line. The bot's own lines are as they were."""
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", _AT_THE_MONEY), feed.MONEY)
    desk = Desk(monkeypatch, bot, budget=5)
    await desk.settle()
    desk.spy()
    await desk.settle()
    bots_lines = desk.ibkr.open_option_lines()
    asked_before = list(desk.ibkr.asked)
    listings_read = desk.ibkr.client.ib.reqSecDefOptParamsAsync.await_count
    puts = _on_qqq(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")

    first = await desk.preview(puts)
    await desk.settle()
    seen = await desk.preview(puts)
    await desk.settle()

    for answer in (first, seen):
        assert (answer.state, answer.reason_code) == ("no_quote_line", "OPTION_LINE_OVER_BUDGET")
        assert _shows_no_figure(answer) and answer.ask_again_in_ms is None
    assert desk.ibkr.asked == asked_before and "QQQ" not in desk.ibkr.lines
    assert desk.ibkr.client.ib.reqSecDefOptParamsAsync.await_count == listings_read
    assert len(bots_lines) == 5 and desk.ibkr.open_option_lines() == bots_lines and desk.ibkr.cancelled == []


async def test_a_preview_is_served_again_once_a_line_is_spare(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", _AT_THE_MONEY), feed.MONEY)
    desk = Desk(monkeypatch, bot, budget=5)
    await desk.settle()
    desk.spy()
    await desk.settle()
    puts = _on_qqq(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")
    while_full = await desk.preview(puts)

    desk.watch.unwatch("bot-0")
    await desk.settle()
    await desk.preview(puts)
    await desk.settle()

    assert while_full.state == "no_quote_line"
    assert "QQQ" in desk.ibkr.lines


async def test_a_bots_claim_on_its_own_underlying_stands_whatever_pages_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot and the page are both on SPY, and no line is spare. SPY stays quoted: it is the bot's."""
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", _AT_THE_MONEY), feed.MONEY)
    desk = Desk(monkeypatch, bot, budget=5)
    puts = _asking(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")

    await desk.watching(puts)
    await desk.preview(puts)
    await desk.settle()

    assert "SPY" in desk.ibkr.lines and "SPY" not in desk.ibkr.cancelled
    assert desk.ibkr.open_option_lines() == feed._symbols(583, 584, 585, 586, 587)


async def test_a_page_kept_out_by_ibkrs_allowance_is_told_to_ask_again_and_is_let_in_when_ibkr_has_a_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IBKR grants three lines and refuses a fourth. The bot then holds exactly those three, IBKR's lines come
    back, and a page asks about QQQ. It is asked nothing for, so nothing of its was refused by IBKR: it is
    waiting for a line, is told to ask again, and a minute after the refusal one more line is tried for."""
    held = tuple(scenes._contract("call", strike) for strike in (584, 585, 586, 587))
    desk = Desk(monkeypatch, WatchRequest("SPY", LineClass.CANDIDATE, held=held), budget=20)
    desk.ibkr.allowance = 3
    await desk.settle()
    granted = desk.ibkr.open_option_lines()
    desk.watch.watch(
        "bot-0", WatchRequest("SPY", LineClass.CANDIDATE, held=tuple(c for c in held if c.occ_symbol in granted))
    )
    desk.ibkr.allowance = None
    await desk.settle()
    puts = _on_qqq(_leg("long_put", "long", "put", _AT_THE_MONEY), structure="long_put")

    await desk.preview(puts)
    await desk.settle()
    waiting = await desk.preview(puts)
    asked_ibkr_for_qqq = "QQQ" in desk.ibkr.asked
    desk.ibkr.now += CEILING_PROBE_MS
    desk.ibkr.keep_alive()
    await desk.preview(puts)
    await desk.settle()
    let_in = await desk.preview(puts)

    assert len(granted) == 3
    assert (waiting.state, waiting.reason_code) == ("no_quote_line", "OPTION_LINE_NO_CAPACITY")
    assert _shows_no_figure(waiting) and waiting.ask_again_in_ms is not None
    # Nothing was asked of IBKR on this page's account, so nothing of its can have been refused.
    assert not asked_ibkr_for_qqq and "refused a quote line for this preview" not in waiting.notice
    assert let_in.state != "no_quote_line" and "QQQ" in desk.ibkr.lines
    assert granted <= desk.ibkr.open_option_lines() and not granted & set(desk.ibkr.cancelled)


# ---- Where the search runs ------------------------------------------------------------------


def _held_back(desk: Desk, *, wait_s: float = 5.0) -> Workers:
    """Give ``desk`` a search lane whose worker answers only when the test lets it."""
    workers = Workers()
    desk.searches = PreviewSearches(OptionSearchLane("preview", executor=workers), wait_s=wait_s)
    return workers


async def _quoted_and_ready(desk: Desk, request: OptionStructurePreviewRequest) -> None:
    await desk.watching(request)
    desk.quote_every_line()
    desk.spy()
    desk.ibkr.now += 400


async def _turns(times: int = 10) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(happened: Callable[[], bool]) -> None:
    """Wait for something to have happened, however long the machine takes over it."""
    async with asyncio.timeout(30):
        while not happened():
            await asyncio.sleep(0.001)


async def test_the_search_worker_is_started_when_a_window_is_first_asked_for_so_that_it_loads_while_quotes_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    workers = _held_back(desk)

    cannot_be_deployed = await desk.preview(_asking(_leg("short_call", "short", "call", _AT_THE_MONEY)))
    started_for_it = len(workers.made)
    first_ask = await desk.preview(_BULL_CALL_SPREAD)

    assert (cannot_be_deployed.state, started_for_it) == ("plan_refused", 0)
    # No quote has arrived, and nothing has been searched for. The worker is up for when one is.
    assert first_ask.state != "resolved"
    assert len(workers.made) == 1 and workers.made[0].searches == []


async def test_a_second_ask_while_the_search_runs_starts_no_second_search_and_is_told_to_ask_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    workers = _held_back(desk)
    await _quoted_and_ready(desk, _BULL_CALL_SPREAD)

    first = asyncio.ensure_future(desk.preview(_BULL_CALL_SPREAD))
    worker = await workers.handed()
    second = await desk.preview(_BULL_CALL_SPREAD)
    begun = len(worker.searches)
    worker.let_go()
    answered = await first

    assert (second.state, second.reason_code) == ("search_not_done", "OPTION_SEARCH_ALREADY_RUNNING")
    assert _shows_no_figure(second) and second.ask_again_in_ms is not None
    # Not a refusal of the structure: the verdict's second half is not known, and nothing is red.
    assert (second.verdict.can_be_deployed, second.verdict.resolves_as_built, second.verdict.red) == (True, None, False)
    assert second.watched.lines_held == 10
    assert begun == 1 == len(worker.searches)
    assert answered.state == "resolved"


async def test_a_page_whose_search_would_be_the_third_in_hand_is_told_to_ask_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two other pages' searches are in hand, one running and one waiting. This page's is not queued behind
    them: it is told its turn has not come, and to ask again."""
    desk = Desk(monkeypatch)
    workers = _held_back(desk)
    pages = [_BULL_CALL_SPREAD.model_copy(update={"preview_key": f"page-{page:04d}"}) for page in range(3)]
    for page in pages[1:]:
        await desk.preview(page)
    await _quoted_and_ready(desk, pages[0])

    running = asyncio.ensure_future(desk.preview(pages[0]))
    worker = await workers.handed()
    waiting = asyncio.ensure_future(desk.preview(pages[1]))
    await _until(lambda: desk.searches.lane.in_hand == 2)
    third = await desk.preview(pages[2])
    worker.let_go(0)
    (await workers.handed(2)).let_go(1)

    assert (third.state, third.reason_code) == ("search_not_done", "OPTION_SEARCH_LANE_BUSY")
    assert _shows_no_figure(third) and third.ask_again_in_ms is not None
    assert (third.verdict.resolves_as_built, third.verdict.red) == (None, False)
    assert ((await running).state, (await waiting).state) == ("resolved", "resolved")
    assert len(worker.searches) == 2


async def test_a_search_that_outlives_its_ask_is_never_shown_and_the_next_ask_searches_the_quotes_as_it_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search too slow to wait for stands on quotes read before it began. Its answer is not kept for the
    page's next ask: by the time that ask comes, those quotes are older than a pick may stand on."""
    desk = Desk(monkeypatch)
    workers = _held_back(desk, wait_s=0.05)
    await _quoted_and_ready(desk, _BULL_CALL_SPREAD)
    read_at = desk.ibkr.now

    told = await desk.preview(_BULL_CALL_SPREAD)
    worker = await workers.handed()
    worker.let_go(0)
    await _turns()
    desk.ibkr.now += 1_000
    asking = asyncio.ensure_future(desk.preview(_BULL_CALL_SPREAD))
    (await workers.handed(2)).let_go(1)
    seen = await asking

    assert (told.state, told.reason_code) == ("search_not_done", "OPTION_SEARCH_NOT_DONE_IN_TIME")
    assert told.ask_again_in_ms is not None
    # A search of its own, of the quotes as read a second later: they are 1,400 ms old then, not 400.
    assert (seen.state, seen.quotes_read_ms, seen.oldest_quote_age_ms) == ("resolved", read_at + 1_000, 1_400)
    assert [search.asked.selection_ms for search in worker.searches] == [read_at, read_at + 1_000]


def test_a_late_answer_would_be_older_than_a_pick_may_stand_on_before_a_page_could_ask_for_it() -> None:
    """Why a search that outlives its ask is not kept for the page's next one. The ask waits four seconds and
    is told to ask again in one, so the next ask comes when the quotes the search read are past the age limit
    the resolver is handed. Were the wait made shorter or the limit longer, keeping a late answer, no older
    than that limit, would be of use again: this fails then, to say so."""
    earliest_next_ask_ms = PREVIEW_SEARCH_WAIT_S * 1_000 + option_structure_preview._ASK_AGAIN_MS

    assert earliest_next_ask_ms >= DECISION_DATA_MAX_AGE_MS


async def test_a_search_that_could_not_be_run_is_said_plainly_and_is_no_refusal_of_the_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    desk.searches = PreviewSearches(OptionSearchLane("preview", executor=_Dies))
    await _quoted_and_ready(desk, _BULL_CALL_SPREAD)

    seen = await desk.preview(_BULL_CALL_SPREAD)

    assert (seen.state, seen.reason_code) == ("search_not_done", "OPTION_SEARCH_WORKER_FAILED")
    # Nothing is running that asking again in a second would find.
    assert _shows_no_figure(seen) and seen.ask_again_in_ms is None
    assert (seen.verdict.can_be_deployed, seen.verdict.resolves_as_built, seen.verdict.red) == (True, None, False)
    assert seen.verdict.resolves_reason_code is None


async def _largest_gap_while[T](asking: Awaitable[T]) -> tuple[T, float, float]:
    """Run ``asking`` with a ticker beside it on the same event loop. Returns its answer, the longest the loop
    went between two runs of the ticker, and how long the ask took, both in seconds."""
    loop = asyncio.get_running_loop()
    gaps: list[float] = []

    async def tick() -> None:
        last = loop.time()
        while True:
            await asyncio.sleep(0.001)
            now = loop.time()
            gaps.append(now - last)
            last = now

    ticking = asyncio.ensure_future(tick())
    await asyncio.sleep(0.02)
    gaps.clear()
    began = loop.time()
    try:
        answer = await asking
    finally:
        took = loop.time() - began
        ticking.cancel()
    return answer, max(gaps), took


async def test_the_event_loop_keeps_running_while_a_real_worker_process_searches_for_an_iron_condor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop that stamps quote receipts, runs the supervisor and sends orders is not stopped by a preview.

    An iron condor picks two legs by delta: forty-two contracts priced on the full tree, with ten strikes
    watched either side. A ticker runs on the event loop beside one ask, and the longest the loop went between
    two of its ticks is compared with how long that ask took, nearly all of which is its search. Measured on
    macOS, CPython 3.12.13, 2026-10-07, three asks each way:

    - searched in a worker process, as here: the ask took 1,241 to 1,410 ms and the loop's longest gap was 3.5
      to 5.5 ms, 0.3 to 0.4 percent of the ask;
    - the same search handed to a thread of this process, as it was first built: the ask took 1,009 to 1,133
      ms and the loop did not run for 1,005 to 1,130 ms of it, 99.5 to 99.7 percent.

    The pricer lets go of the interpreter lock for under a microsecond at a time, and a thread waiting for it
    never gets in. A search in this process therefore fails this test, in a thread or not. The gap is held to
    half the ask and not to a number of milliseconds: a machine with too few processors stretches both, and
    half still parts a loop that ran throughout from one that waited for the search.
    """
    desk = Desk(monkeypatch, reach=10)
    # A wait long enough that a worker starved of the processor still answers: this ask is never told that
    # its search is not done.
    desk.searches = PreviewSearches(OptionSearchLane("preview"), wait_s=120.0)
    iron_condor = _as_the_builder_starts(NamedStructure.IRON_CONDOR)
    try:
        await _quoted_and_ready(desk, iron_condor)
        # The first ask starts the worker and waits for it to load. The ask that is measured finds it up.
        loaded = await desk.preview(iron_condor)
        desk.quote_every_line()
        desk.spy()
        desk.ibkr.now += 400

        seen, largest_gap_s, took_s = await _largest_gap_while(desk.preview(iron_condor))
    finally:
        desk.searches.close()

    assert (loaded.state, seen.state) == ("resolved", "resolved"), (loaded.reason_code, seen.reason_code, seen.notice)
    assert sorted(leg.leg_id for leg in seen.legs) == ["long_call", "long_put", "short_call", "short_put"]
    # A search long enough to tell a loop that ran through it from one that waited for it.
    assert took_s >= 0.2, took_s
    assert largest_gap_s < took_s / 2, (largest_gap_s, took_s)
