"""The option structure preview: what the owner's rules would pick right now, and each way it has nothing to show.

Every preview here is asked of a real supervisor on a stand-in IBKR
connection: the chain is listed, lines are opened and quotes arrive through
its callbacks, as they would from the Gateway.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import numpy as np
import pytest

from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.broker.ibkr.option_quotes import OptionQuoteLines, WatchRequest
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.options.american import CashDividend, american_implied_volatility, years_from_ms
from app.options.expiry import option_expiry_anchor_ms
from app.options.lean import ABOUT_FLAT_BELOW_DELTA
from app.options.resolver import DividendOn
from app.options.watch import LineClass
from app.schemas.option_preview import OptionStructurePreview, OptionStructurePreviewRequest
from app.services import option_structure_preview
from app.services.broker_v2_panel import option_structure_panel
from app.services.option_model_inputs import ModelInputsBeingRead, ModelInputsRead
from app.services.option_structure_preview import (
    ARRIVAL_GRACE_MS,
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

# A preview lane that searches in this process, through the pickling a worker process's pipe does.
from tests.services.test_option_search import searches_in_this_process

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

    async def settle(self) -> None:
        await feed._settle(self.status)

    def spy(self) -> None:
        self.ibkr.tick("SPY", bid=585.10, ask=585.12)

    def quote(self, right: str, strike: int, bid: str, ask: str, expiry: date = FRIDAY) -> None:
        self.ibkr.tick(scenes._contract(right, strike, expiry).occ_symbol, bid=float(bid), ask=float(ask))

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

    ours = [_our_delta("call", 585, "4.08", "4.12", at_ms=NOW + 400), _our_delta("call", 590, "2.18", "2.22", at_ms=NOW + 400)]
    assert [leg.delta for leg in seen.legs] == ours
    assert all(leg.delta_status == "solved" and leg.implied_volatility > 0 for leg in seen.legs)
    _close(seen.lean.net_delta, ours[0] - ours[1])
    _close(seen.lean.shares_per_structure, 100 * (ours[0] - ours[1]))
    _close(seen.lean.shares, 100 * (ours[0] - ours[1]))
    assert (seen.lean.direction, seen.lean.gains_when_underlying_falls, seen.lean.warning_sentence) == ("up", False, None)
    _close(seen.lean.about_flat_below_shares_per_structure, 100 * ABOUT_FLAT_BELOW_DELTA)

    verdict = seen.verdict
    assert (verdict.can_be_deployed, verdict.resolves_as_built, verdict.red, verdict.leg_ids) == (True, True, False, [])
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
    assert (seen.verdict.resolves_as_built, seen.verdict.red) == (True, False)
    # It sells the call nearer the money, so it gains as SPY falls.
    assert (seen.lean.direction, seen.lean.gains_when_underlying_falls) == ("down", True)


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


# ---- The verdict ------------------------------------------------------------------


async def test_a_structure_the_plans_rules_refuse_is_a_verdict_and_asks_for_no_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    desk = Desk(monkeypatch)
    nothing_covers_it = _asking(_leg("short_call", "short", "call", _AT_THE_MONEY))

    seen = await desk.preview(nothing_covers_it)

    assert (seen.state, seen.reason_code) == ("plan_refused", "OPTION_PREVIEW_PLAN_REFUSED")
    assert (seen.verdict.can_be_deployed, seen.verdict.resolves_as_built, seen.verdict.red) == (False, None, True)
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
    assert _shows_no_figure(seen)
    assert (seen.pricing_inputs.dividends_known, seen.pricing_inputs.dividends) == (False, [])


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


async def test_one_more_page_than_may_hold_a_window_gives_up_the_one_asked_for_longest_ago(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    for page in range(MAX_PREVIEW_WINDOWS + 1):
        desk.ibkr.now += 1_000
        await desk.preview(_BULL_CALL_SPREAD.model_copy(update={"preview_key": f"page-{page:04d}"}))

    assert desk.watch.on_lease() == tuple(f"preview:page-{page:04d}" for page in range(1, MAX_PREVIEW_WINDOWS + 1))
