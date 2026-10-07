"""The contract resolver: what each rule picks from the quotes held, and each way it refuses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options import resolver
from app.options.american import (
    MS_PER_YEAR,
    AmericanIv,
    AmericanIvStatus,
    CashDividend,
    american_implied_volatility,
    american_value,
    years_from_ms,
)
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from app.options.named_structures import NamedStructure
from app.options.quotes import HeldQuote, QuoteDataType, QuoteFault, QuoteSide, quote_fault
from app.options.requirement import LegQuote, natural_net_price
from app.options.resolver import (
    DEFAULT_COHERENCE_WINDOW_MS,
    Chain,
    ContractsNotResolved,
    DividendOn,
    FeedState,
    Listing,
    ModelInputs,
    PassedOver,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
    WatchedContract,
    recheck,
    resolve_contracts,
)
from app.options.structure_catalogue import starting_plan
from app.options.structure_check import require_named_shape, resolved_structure
from app.schemas.action_plan import ActionPlan
from app.utils.timestamps import to_ms_utc

_NY = ZoneInfo("America/New_York")
_GENERATION = 7
_TUESDAY = date(2026, 10, 6)
_WEDNESDAY = date(2026, 10, 7)
_FRIDAY = date(2026, 10, 9)
_NEXT_FRIDAY = date(2026, 10, 16)
_NOVEMBER = date(2026, 11, 20)
_EXPIRIES = (_TUESDAY, _WEDNESDAY, _FRIDAY, _NEXT_FRIDAY, _NOVEMBER)
# A small tree keeps the tests quick; the model's own accuracy is the pricer's tests' business.
_MODEL = ModelInputs(rate=0.045, rate_source="test", dividends=(), dividends_source="test: none", tree_steps=101)
_LIMITS = ResolverLimits(
    max_quote_age_ms=5_000, coherence_window_ms=DEFAULT_COHERENCE_WINDOW_MS, entry_cutoff_lead_ms=60 * 60_000
)
# How far the underlying's live quote has moved from the decision bar's close.
_SINCE_THE_BAR_CLOSED = Decimal("0.11")


def _at(day: date, hour: int, minute: int, second: int = 0) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_NY))


def _contract(right: str, strike: int | str, expiry: date = _FRIDAY) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=strike)


def _quote(bid: str | None, ask: str | None, received_ms: int, **changes: object) -> HeldQuote:
    held = HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), received_ms),
        ask=None if ask is None else QuoteSide(Decimal(ask), received_ms),
        data_type=QuoteDataType.LIVE,
        generation=_GENERATION,
    )
    return replace(held, **changes)


@dataclass(frozen=True)
class _Scene:
    """Everything one resolution is handed."""

    chain: Chain
    underlying: UnderlyingObservation
    feed: FeedState
    model: ModelInputs
    limits: ResolverLimits
    decision_bar_close_ms: int
    selection_ms: int

    def resolve(self, plan: ActionPlan) -> resolver.Resolution:
        return resolve_contracts(
            plan,
            chain=self.chain,
            underlying=self.underlying,
            feed=self.feed,
            model=self.model,
            limits=self.limits,
            decision_bar_close_ms=self.decision_bar_close_ms,
            selection_ms=self.selection_ms,
        )

    def refusal(self, plan: ActionPlan) -> ContractsNotResolved:
        with pytest.raises(ContractsNotResolved) as refused:
            self.resolve(plan)
        return refused.value

    def holding(self, contract: OptionContract, quote: HeldQuote | None, vendor_delta: float | None = None) -> _Scene:
        """The same scene with one contract's quote replaced."""
        watched = dict(self.chain.watched) | {contract.occ_symbol: WatchedContract(quote, vendor_delta)}
        return replace(self, chain=replace(self.chain, watched=watched))

    def quote_of(self, contract: OptionContract) -> HeldQuote:
        quote = self.chain.watched[contract.occ_symbol].quote
        assert quote is not None
        return quote


def _scene(
    *,
    day: date = _TUESDAY,
    selected_at: tuple[int, int, int] = (10, 31, 2),
    money: str = "585.00",
    listed: range = range(560, 611),
    watched: range = range(570, 601),
    expiries: tuple[date, ...] = _EXPIRIES,
    model: ModelInputs = _MODEL,
    limits: ResolverLimits = _LIMITS,
) -> _Scene:
    """SPY with its decision bar closed at ``money`` and now quoted eleven cents higher, a dollar-wide ladder of
    strikes, and a two-cent market around the model's own price for each."""
    price = Decimal(money)
    quoted = price + _SINCE_THE_BAR_CLOSED
    selection_ms = _at(day, *selected_at)
    received_ms = selection_ms - 400
    listings, held = [], {}
    for expiry in expiries:
        years = years_from_ms(session_close_ms_utc(expiry) - selection_ms)
        for right in ("call", "put"):
            for strike in listed:
                contract = _contract(right, strike, expiry)
                listings.append(Listing(contract, multiplier=100, shares_delivered=100, exercise="american"))
                if strike not in watched or years <= 0:
                    continue
                worth = american_value(
                    spot=float(quoted),
                    strike=float(strike),
                    years_to_expiry=years,
                    rate=model.rate,
                    volatility=0.20,
                    right=right,
                    dividends=(),
                    steps=model.tree_steps,
                )
                cents = max(1, round((worth.price if worth is not None else 0.0) * 100))
                held[contract.occ_symbol] = WatchedContract(
                    _quote(f"{(cents - 1) / 100:.2f}", f"{(cents + 1) / 100:.2f}", received_ms), vendor_delta=None
                )
    return _Scene(
        chain=Chain(listings=tuple(listings), watched=held),
        underlying=UnderlyingObservation(
            pricing_bar_close=price, quote=_quote(str(quoted - Decimal("0.01")), str(quoted + Decimal("0.01")), received_ms)
        ),
        feed=FeedState(generation=_GENERATION, unhealthy=None),
        model=model,
        limits=limits,
        decision_bar_close_ms=_at(day, selected_at[0], selected_at[1]),
        selection_ms=selection_ms,
    )


def _leg(leg_id: str, right: str, strike: dict, *, expiry: dict | None = None, position: str = "long", ratio: int = 1) -> dict:
    return {
        "leg_id": leg_id,
        "instrument": {"kind": "option", "underlying": "SPY"},
        "position": position,
        "qty_ratio": ratio,
        "right": right,
        "strike": strike,
        "expiry": expiry or {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(_FRIDAY)},
    }


def _plan(*legs: dict) -> ActionPlan:
    return ActionPlan.model_validate(
        {"on_enter": list(legs), "on_exit": [{"kind": "close_leg", "entry_leg_id": leg["leg_id"]} for leg in legs]}
    )


def _one(right: str, strike: dict, **leg: object) -> ActionPlan:
    return _plan(_leg("only", right, strike, **leg))


_AT_THE_MONEY = {"selector": "atm"}


# ---- What each strike rule picks ------------------------------------------


@pytest.mark.parametrize(
    ("strike", "picks"),
    [
        pytest.param(_AT_THE_MONEY, 585, id="at the money"),
        pytest.param({"selector": "atm_offset", "offset": 2}, 587, id="two strikes above the money"),
        pytest.param({"selector": "atm_offset", "offset": -3}, 582, id="three strikes below the money"),
        pytest.param({"selector": "absolute", "strike": 590.0}, 590, id="one named strike"),
        pytest.param({"selector": "percent", "percent": 2.0}, 597, id="2% above the price, which is 596.70"),
        pytest.param({"selector": "percent", "percent": -1.5}, 576, id="1.5% below the price, which is 576.225"),
    ],
)
def test_a_strike_rule_picks_the_listed_strike_it_means(strike: dict, picks: int) -> None:
    resolution = _scene().resolve(_one("call", strike))

    assert resolution.picked == {"only": _contract("call", picks)}


def test_a_distance_rule_is_measured_from_the_other_legs_pick_whichever_is_written_first() -> None:
    plan = _plan(
        _leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
        _leg("long_call", "call", _AT_THE_MONEY),
    )

    resolution = _scene().resolve(plan)

    assert dict(resolution.picked) == {"short_call": _contract("call", 590), "long_call": _contract("call", 585)}
    assert list(resolution.picked) == ["short_call", "long_call"]
    assert [leg.leg_id for leg in resolution.evidence.legs] == ["long_call", "short_call"]


@pytest.mark.parametrize(("right", "target"), [("call", 0.30), ("put", 0.30), ("call", 0.50), ("put", 0.15)])
def test_a_delta_rule_picks_the_contract_whose_delta_is_nearest_the_target(right: str, target: float) -> None:
    scene = _scene()

    resolution = scene.resolve(_one(right, {"selector": "delta", "target": target}))

    (leg,) = resolution.evidence.legs
    sizes = {
        candidate.contract: abs(candidate.priced.delta)
        for candidate in leg.candidates
        if candidate.priced is not None and candidate.priced.delta is not None
    }
    picked = resolution.picked["only"]
    assert len(sizes) == 31
    assert abs(sizes[picked] - target) == min(abs(size - target) for size in sizes.values())
    assert abs(sizes[picked] - target) <= 0.05
    # A put's delta is negative; the target is its size.
    assert all((candidate.priced.delta > 0) == (right == "call") for candidate in leg.candidates)


def test_the_delta_a_pick_is_judged_by_is_the_models_own_for_that_quote() -> None:
    """Solved from the contract's mid against the underlying's mid, over the time left to the expiry day's close."""
    scene = _scene()
    contract = _contract("put", 580)

    resolution = scene.resolve(_one("put", {"selector": "absolute", "strike": 580.0}))

    (candidate,) = [c for c in resolution.evidence.legs[0].candidates if c.contract == contract]
    quote = scene.quote_of(contract)
    years = (session_close_ms_utc(_FRIDAY) - scene.selection_ms) / MS_PER_YEAR
    expected = american_implied_volatility(
        price=float((quote.bid.price + quote.ask.price) / 2),
        spot=585.11,
        strike=580.0,
        years_to_expiry=years,
        rate=0.045,
        right="put",
        dividends=(),
        steps=101,
    )
    assert candidate.passed_over is None
    assert candidate.priced.years_to_expiry == years
    assert (candidate.priced.status, candidate.priced.implied_volatility, candidate.priced.delta) == (
        AmericanIvStatus.SOLVED,
        expected.volatility,
        expected.delta,
    )
    assert resolution.evidence.model_spot == 585.11


def test_a_dividend_before_expiry_reaches_the_model() -> None:
    going_ex = DividendOn(ex_ms=_at(_NEXT_FRIDAY, 9, 30), cash=1.80)
    paying = replace(_MODEL, dividends=(going_ex,), dividends_source="test: one dividend")
    plan = _one("put", {"selector": "absolute", "strike": 585.0}, expiry={"selector": "min_dte", "days": 30})

    without = _scene().resolve(plan).evidence.legs[0]
    scene = _scene(model=paying)
    with_it = scene.resolve(plan).evidence.legs[0]

    def picked(leg: resolver.LegEvidence) -> resolver.Candidate:
        (candidate,) = [c for c in leg.candidates if c.passed_over is None]
        return candidate

    quote = scene.quote_of(picked(with_it).contract)
    expected = american_implied_volatility(
        price=float((quote.bid.price + quote.ask.price) / 2),
        spot=585.11,
        strike=585.0,
        years_to_expiry=picked(with_it).priced.years_to_expiry,
        rate=0.045,
        right="put",
        dividends=(CashDividend(years_to_ex=years_from_ms(going_ex.ex_ms - scene.selection_ms), cash=1.80),),
        steps=101,
    )
    assert picked(with_it).contract.expiry_date == _NOVEMBER
    assert picked(with_it).priced.delta == expected.delta
    assert picked(with_it).priced.delta != picked(without).priced.delta


# ---- What each expiry rule picks ------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "picks"),
    [
        pytest.param({"selector": "same_day"}, _TUESDAY, id="the decision's own date"),
        pytest.param({"selector": "min_dte", "days": 1}, _WEDNESDAY, id="at least a day out"),
        pytest.param({"selector": "min_dte", "days": 4}, _NEXT_FRIDAY, id="at least four days out, past this Friday's three"),
        pytest.param({"selector": "nearest_weekly"}, _FRIDAY, id="the week's last session"),
        pytest.param(
            {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(_NOVEMBER)}, _NOVEMBER, id="one named date"
        ),
    ],
)
def test_an_expiry_rule_picks_the_listed_expiry_it_means(expiry: dict, picks: date) -> None:
    resolution = _scene().resolve(_one("call", _AT_THE_MONEY, expiry=expiry))

    assert resolution.picked["only"].expiry_date == picks
    assert resolution.evidence.legs[0].expiry_ms == option_expiry_anchor_ms(picks)


def test_the_nearest_weekly_on_a_friday_is_the_following_weeks() -> None:
    resolution = _scene(day=_FRIDAY, expiries=(_FRIDAY, _NEXT_FRIDAY)).resolve(
        _one("call", _AT_THE_MONEY, expiry={"selector": "nearest_weekly"})
    )

    assert resolution.picked["only"].expiry_date == _NEXT_FRIDAY


def test_a_week_that_ends_on_a_thursday_has_its_weekly_on_the_thursday() -> None:
    """Good Friday 2026 is April 3rd, so that week's last session is Thursday the 2nd."""
    monday, wednesday, thursday, next_friday = date(2026, 3, 30), date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 10)

    resolution = _scene(day=monday, expiries=(wednesday, thursday, next_friday)).resolve(
        _one("call", _AT_THE_MONEY, expiry={"selector": "nearest_weekly"})
    )

    assert resolution.picked["only"].expiry_date == thursday


@pytest.mark.parametrize(
    "expiry",
    [
        pytest.param({"selector": "same_day"}, id="no expiry today"),
        pytest.param({"selector": "min_dte", "days": 90}, id="nothing listed that far out"),
        pytest.param({"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(date(2026, 10, 23))}, id="an unlisted date"),
        pytest.param({"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(date(2026, 10, 2))}, id="a date gone by"),
    ],
)
def test_an_expiry_the_chain_does_not_list_is_refused_as_that(expiry: dict) -> None:
    refused = _scene(expiries=(date(2026, 10, 2), _FRIDAY, _NEXT_FRIDAY)).refusal(_one("call", _AT_THE_MONEY, expiry=expiry))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.EXPIRY_NOT_LISTED, ("only",))
    assert refused.evidence.legs[0].expiry_ms is None


@pytest.mark.parametrize(
    ("day", "selected_at", "offered"),
    [
        pytest.param(_TUESDAY, (14, 59, 59), True, id="a second before the cutoff, an hour before a 16:00 close"),
        pytest.param(_TUESDAY, (15, 0, 0), False, id="at the cutoff"),
        pytest.param(date(2026, 11, 27), (11, 59, 59), True, id="a half day, a second before its noon cutoff"),
        pytest.param(date(2026, 11, 27), (12, 30, 0), False, id="a half day, half an hour before its 13:00 close"),
    ],
)
def test_nothing_expiring_today_is_offered_after_the_entry_cutoff(
    day: date, selected_at: tuple[int, int, int], offered: bool
) -> None:
    scene = _scene(day=day, selected_at=selected_at, expiries=(day, date(2026, 12, 18)))
    today = _one("call", _AT_THE_MONEY, expiry={"selector": "same_day"})
    later = _one("call", _AT_THE_MONEY, expiry={"selector": "min_dte", "days": 1})

    if offered:
        assert scene.resolve(today).picked["only"].expiry_date == day
    else:
        refused = scene.refusal(today)
        assert (refused.reason, refused.leg_ids) == (ResolverRefusal.PAST_ENTRY_CUTOFF, ("only",))
    # The cutoff is each contract's own: one expiring another day is still offered.
    assert scene.resolve(later).picked["only"].expiry_date == date(2026, 12, 18)


def test_time_to_expiry_ends_at_the_expiry_days_own_close() -> None:
    """The day after Thanksgiving closes at 13:00; a contract expiring then has three hours less than its 16:00 anchor says."""
    half_day = date(2026, 11, 27)
    scene = _scene(day=date(2026, 11, 25), expiries=(half_day,))

    resolution = scene.resolve(_one("call", _AT_THE_MONEY, expiry={"selector": "min_dte", "days": 1}))

    (picked,) = [c for c in resolution.evidence.legs[0].candidates if c.passed_over is None]
    assert picked.priced.years_to_expiry == (_at(half_day, 13, 0) - scene.selection_ms) / MS_PER_YEAR


# ---- Ties -----------------------------------------------------------------


def test_the_money_between_two_strikes_is_the_lower() -> None:
    assert _scene(money="585.50").resolve(_one("call", _AT_THE_MONEY)).picked["only"] == _contract("call", 585)


def test_the_money_is_the_decision_bars_close_not_where_the_quote_has_moved_since() -> None:
    """The bar closed at 585.45, nearer 585. The quote is at 585.56 by the selection, nearer 586."""
    scene = _scene(money="585.45")

    assert scene.resolve(_one("call", _AT_THE_MONEY)).picked["only"] == _contract("call", 585)
    assert scene.resolve(_one("call", {"selector": "percent", "percent": 0.0})).picked["only"] == _contract("call", 585)


def test_a_percent_rule_between_two_strikes_takes_the_one_nearer_the_money_then_the_lower() -> None:
    """A quarter of a percent under 600 is 598.50: 598 and 599 are as near it, and 599 is nearer the money.
    At the price itself, with the price at 587.50, 587 and 588 are as near both: the lower."""
    below = _scene(money="600.00").resolve(_one("call", {"selector": "percent", "percent": -0.25}))
    at_the_price = _scene(money="587.50").resolve(_one("call", {"selector": "percent", "percent": 0.0}))

    assert below.picked["only"] == _contract("call", 599)
    assert at_the_price.picked["only"] == _contract("call", 587)


@pytest.mark.parametrize(
    ("dollars", "picks"),
    [
        pytest.param(5.5, 590, id="590.50 above: 590 and 591 are as near, and 590 is the narrower spread"),
        pytest.param(-5.5, 580, id="579.50 below: 579 and 580 are as near, and 580 is the narrower spread"),
    ],
)
def test_a_distance_rule_between_two_strikes_takes_the_narrower_spread(dollars: float, picks: int) -> None:
    plan = _plan(
        _leg("anchor", "call", _AT_THE_MONEY),
        _leg(
            "other",
            "call",
            {"selector": "leg_distance", "leg_id": "anchor", "dollars": dollars, "tolerance_dollars": 0.5},
        ),
    )

    assert _scene().resolve(plan).picked["other"] == _contract("call", picks)


def _deltas(monkeypatch: pytest.MonkeyPatch, by_strike: dict[float, float | AmericanIvStatus]) -> None:
    """Have the model answer a given delta for each strike, or a status with no number."""

    def answer(*, strike: float, **_: object) -> AmericanIv:
        delta = by_strike.get(strike, 0.99)
        if isinstance(delta, AmericanIvStatus):
            return AmericanIv(delta)
        return AmericanIv(AmericanIvStatus.SOLVED, volatility=0.2, delta=delta)

    monkeypatch.setattr(resolver, "american_implied_volatility", answer)


@pytest.mark.parametrize(
    ("money", "picks"),
    [
        pytest.param("586.90", 586, id="586 is nearer the money"),
        pytest.param("587.40", 588, id="588 is nearer the money"),
        pytest.param("587.00", 586, id="as near the money too: the lower"),
    ],
)
def test_two_contracts_as_near_a_delta_target_go_to_the_one_nearer_the_money_then_the_lower(
    monkeypatch: pytest.MonkeyPatch, money: str, picks: int
) -> None:
    """The 586 call at 0.625 and the 588 call at 0.375 are each an eighth from a target of one half."""
    _deltas(monkeypatch, {586.0: 0.625, 588.0: 0.375})

    resolution = _scene(money=money).resolve(_one("call", {"selector": "delta", "target": 0.5, "tolerance_delta": 0.2}))

    assert resolution.picked["only"] == _contract("call", picks)


# ---- Tolerances, each in its own unit -------------------------------------


def test_a_delta_farther_from_its_target_than_the_tolerance_is_no_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    _deltas(monkeypatch, {584.0: 0.5625, 585.0: 0.4375})
    rule = {"selector": "delta", "target": 0.5, "tolerance_delta": 0.0624}

    refused = _scene().refusal(_one("call", rule))

    assert (refused.reason, refused.fault) == (ResolverRefusal.NOTHING_CLOSE_ENOUGH, None)
    assert _scene().resolve(_one("call", rule | {"tolerance_delta": 0.0625})).picked["only"] == _contract("call", 585)


def test_a_percent_farther_from_its_target_than_the_tolerance_is_no_pick() -> None:
    """4.75% under 600 is 571.50, half a dollar from 571 and from 572. The tolerance is a percent of the
    price: 0.085% of 600 is 0.51, which reaches them, where 0.085% of the 571.50 target would be 0.486."""
    rule = {"selector": "percent", "percent": -4.75, "tolerance_percent": 0.083}

    refused = _scene(money="600.00").refusal(_one("call", rule))

    assert refused.reason is ResolverRefusal.NOTHING_CLOSE_ENOUGH
    assert _scene(money="600.00").resolve(_one("call", rule | {"tolerance_percent": 0.085})).picked["only"] == _contract(
        "call", 572
    )


def test_a_distance_that_lands_between_strikes_is_no_pick_unless_dollars_are_allowed() -> None:
    def plan(tolerance: float) -> ActionPlan:
        return _plan(
            _leg("anchor", "call", _AT_THE_MONEY),
            _leg(
                "other",
                "call",
                {"selector": "leg_distance", "leg_id": "anchor", "dollars": 5.5, "tolerance_dollars": tolerance},
            ),
        )

    refused = _scene().refusal(plan(0.0))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.NOTHING_CLOSE_ENOUGH, ("other",))
    # The leg already resolved stays in the evidence beside the one that was not.
    assert [(leg.leg_id, leg.picked) for leg in refused.evidence.legs] == [
        ("anchor", _contract("call", 585)),
        ("other", None),
    ]
    assert _scene().refusal(plan(0.49)).reason is ResolverRefusal.NOTHING_CLOSE_ENOUGH
    assert _scene().resolve(plan(0.5)).picked["other"] == _contract("call", 590)


@pytest.mark.parametrize(
    "strike",
    [
        pytest.param({"selector": "absolute", "strike": 590.5}, id="a strike the chain does not list"),
        pytest.param({"selector": "atm_offset", "offset": 40}, id="more strikes above the money than are listed"),
        pytest.param({"selector": "atm_offset", "offset": -26}, id="one strike below the lowest listed"),
    ],
)
def test_a_rule_naming_one_strike_the_chain_does_not_list_is_refused_as_that(strike: dict) -> None:
    refused = _scene().refusal(_one("call", strike))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.STRIKE_NOT_LISTED, ("only",))


# ---- Quotes: each fault refused as itself ---------------------------------

_NOW = 1_000_000
_FRESH = _NOW - 400


@pytest.mark.parametrize(
    ("quote", "needs_a_bid", "fault"),
    [
        pytest.param(_quote("1.00", "1.02", _FRESH), True, None, id="a live two-sided market, fresh"),
        pytest.param(_quote("1.00", "1.02", _NOW - 5_000), True, None, id="exactly as old as the limit"),
        pytest.param(_quote("0", "0.02", _FRESH), False, None, id="no bid on an option to be bought"),
        pytest.param(_quote("1.00", "1.00", _FRESH), True, None, id="bid and ask the same"),
        pytest.param(_quote("1.00", "1.02", _FRESH, generation=6), True, QuoteFault.OLD_GENERATION, id="before the reconnect"),
        pytest.param(
            _quote("1.00", "1.02", _FRESH, data_type=QuoteDataType.DELAYED), True, QuoteFault.NOT_LIVE, id="delayed data"
        ),
        pytest.param(
            _quote("1.00", "1.02", _FRESH, data_type=QuoteDataType.FROZEN), True, QuoteFault.NOT_LIVE, id="frozen data"
        ),
        pytest.param(_quote(None, "1.02", _FRESH), False, QuoteFault.ONE_SIDED, id="an ask and no bid"),
        pytest.param(_quote("1.00", None, _FRESH), False, QuoteFault.ONE_SIDED, id="a bid and no ask"),
        pytest.param(_quote("NaN", "1.02", _FRESH), False, QuoteFault.NOT_A_PRICE, id="a bid that is no number"),
        pytest.param(_quote("-1", "1.02", _FRESH), False, QuoteFault.NOT_A_PRICE, id="the provider's minus one for no bid"),
        pytest.param(_quote("0", "0", _FRESH), False, QuoteFault.ASK_NOT_ABOVE_ZERO, id="nothing offered"),
        pytest.param(_quote("0", "0.02", _FRESH), True, QuoteFault.BID_NOT_ABOVE_ZERO, id="no bid on an option to be sold"),
        pytest.param(_quote("1.03", "1.02", _FRESH), True, QuoteFault.CROSSED, id="bid above ask"),
        pytest.param(_quote("1.00", "1.02", _NOW - 5_001), True, QuoteFault.STALE, id="a millisecond older than the limit"),
        pytest.param(_quote("1.00", "1.02", _NOW + 1), True, QuoteFault.STALE, id="received after the moment it is used at"),
        pytest.param(
            replace(_quote("1.00", "1.02", _FRESH), ask=QuoteSide(Decimal("1.02"), _NOW - 9_000)),
            True,
            QuoteFault.STALE,
            id="a fresh bid beside an old ask",
        ),
        pytest.param(
            _quote(None, "1.02", _NOW - 9_000, generation=6, data_type=QuoteDataType.DELAYED),
            True,
            QuoteFault.OLD_GENERATION,
            id="several faults at once: the first asked",
        ),
    ],
)
def test_a_quote_is_usable_or_fails_as_one_thing(quote: HeldQuote, needs_a_bid: bool, fault: QuoteFault | None) -> None:
    assert quote_fault(quote, at_ms=_NOW, generation=_GENERATION, max_age_ms=5_000, needs_a_bid=needs_a_bid) is fault


@pytest.mark.parametrize(
    ("changed", "fault"),
    [
        pytest.param({"ask": None}, QuoteFault.ONE_SIDED, id="one-sided"),
        pytest.param({"bid": QuoteSide(Decimal("9.99"), 0)}, QuoteFault.CROSSED, id="crossed"),
        pytest.param({"data_type": QuoteDataType.DELAYED}, QuoteFault.NOT_LIVE, id="delayed"),
        pytest.param({"generation": _GENERATION - 1}, QuoteFault.OLD_GENERATION, id="an older generation"),
    ],
)
def test_a_rule_naming_one_contract_whose_quote_is_faulty_is_refused_with_the_fault(changed: dict, fault: QuoteFault) -> None:
    scene = _scene()
    contract = _contract("call", 585)
    quote = scene.quote_of(contract)
    if "bid" in changed:
        changed = {"bid": QuoteSide(changed["bid"].price, quote.bid.received_ms)}

    refused = scene.holding(contract, replace(quote, **changed)).refusal(_one("call", _AT_THE_MONEY))

    assert (refused.reason, refused.fault, refused.leg_ids) == (ResolverRefusal.NO_USABLE_QUOTE, fault, ("only",))
    (candidate,) = [c for c in refused.evidence.legs[0].candidates if c.contract == contract]
    assert (candidate.fault, candidate.passed_over) == (fault, PassedOver.QUOTE_FAULT)


def test_a_stale_quote_is_refused_as_stale() -> None:
    scene = _scene()
    contract = _contract("call", 585)
    old = _quote("3.00", "3.02", scene.selection_ms - 5_001)

    refused = scene.holding(contract, old).refusal(_one("call", _AT_THE_MONEY))

    assert (refused.reason, refused.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.STALE)


def test_a_leg_to_be_sold_needs_a_bid_and_a_leg_to_be_bought_does_not() -> None:
    scene = _scene()
    far_call = _contract("call", 600)
    no_bid = scene.holding(far_call, _quote("0", "0.02", scene.selection_ms - 400))
    bought = _one("call", {"selector": "absolute", "strike": 600.0})
    sold = _plan(
        _leg("short_call", "call", {"selector": "absolute", "strike": 600.0}, position="short"),
        _leg("long_call", "call", _AT_THE_MONEY),
    )

    refused = no_bid.refusal(sold)

    assert no_bid.resolve(bought).picked["only"] == far_call
    assert (refused.reason, refused.fault, refused.leg_ids) == (
        ResolverRefusal.NO_USABLE_QUOTE,
        QuoteFault.BID_NOT_ABOVE_ZERO,
        ("short_call",),
    )


def test_a_nearest_rule_passes_over_a_faulty_quote_for_the_next_nearest_within_its_tolerance() -> None:
    """2% above the price is 596.70. With 597's quote one-sided, 596 is the nearest usable, 0.70 away."""
    scene = _scene()
    nearest = _contract("call", 597)
    one_sided = scene.holding(nearest, replace(scene.quote_of(nearest), bid=None))

    resolution = one_sided.resolve(_one("call", {"selector": "percent", "percent": 2.0}))

    assert resolution.picked["only"] == _contract("call", 596)
    passed = {c.contract.strike: c.passed_over for c in resolution.evidence.legs[0].candidates}
    assert (passed[Decimal(597)], passed[Decimal(596)], passed[Decimal(598)]) == (
        PassedOver.QUOTE_FAULT,
        None,
        PassedOver.NOT_THE_NEAREST,
    )


def test_a_nearest_rule_whose_every_strike_in_tolerance_has_a_faulty_quote_is_refused_with_the_nearests_fault() -> None:
    """2% above the price is 596.70, and half a percent of the price is 2.925: 594 to 599 may be picked."""
    scene = _scene()
    for strike in range(594, 600):
        contract = _contract("call", strike)
        scene = scene.holding(contract, replace(scene.quote_of(contract), ask=None))

    refused = scene.refusal(_one("call", {"selector": "percent", "percent": 2.0}))

    assert (refused.reason, refused.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.ONE_SIDED)
    assert "SPY261009C00597000" in str(refused) and "the 5 others" in str(refused)


def test_contracts_on_another_root_are_not_the_plans() -> None:
    scene = _scene()
    elsewhere = tuple(
        replace(listing, contract=listing.contract.model_copy(update={"root": "QQQ"})) for listing in scene.chain.listings
    )

    refused = replace(scene, chain=replace(scene.chain, listings=elsewhere)).refusal(_one("call", _AT_THE_MONEY))

    assert refused.reason is ResolverRefusal.EXPIRY_NOT_LISTED


# ---- Search incomplete, never "nothing close enough" -----------------------


@pytest.mark.parametrize(
    ("right", "strike", "unsearched"),
    [
        pytest.param("call", {"selector": "percent", "percent": 3.0}, 603, id="3% above is 602.55, past the 600 the window ends at"),
        pytest.param("call", {"selector": "absolute", "strike": 605.0}, 605, id="a listed strike outside the window"),
        pytest.param("call", {"selector": "atm_offset", "offset": 16}, 601, id="sixteen strikes up, one past the window"),
        pytest.param(
            "call", {"selector": "delta", "target": 0.02, "tolerance_delta": 0.2}, 601, id="a call delta past the window's top"
        ),
        pytest.param(
            "put", {"selector": "delta", "target": 0.02, "tolerance_delta": 0.2}, 569, id="a put delta past the window's bottom"
        ),
        pytest.param(
            "put", {"selector": "delta", "target": 0.98, "tolerance_delta": 0.2}, 601, id="a deep put delta past the window's top"
        ),
    ],
)
def test_a_target_outside_the_watched_window_is_search_incomplete(right: str, strike: dict, unsearched: int) -> None:
    refused = _scene().refusal(_one(right, strike))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.SEARCH_INCOMPLETE, ("only",))
    assert _contract(right, unsearched).occ_symbol in str(refused)


def test_a_delta_at_the_windows_edge_is_search_incomplete_even_inside_its_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 600 call is the last one watched and has a delta of 0.12. A target of 0.10 is within tolerance of it,
    and the 601 call, which nobody is watching, could be nearer."""
    _deltas(monkeypatch, {599.0: 0.15, 600.0: 0.12})
    rule = {"selector": "delta", "target": 0.10, "tolerance_delta": 0.05}

    refused = _scene().refusal(_one("call", rule))

    assert refused.reason is ResolverRefusal.SEARCH_INCOMPLETE
    assert "SPY261009C00601000" in str(refused)
    # Where the window ends at the last listed strike, nothing is unsearched: the pick stands.
    assert _scene(listed=range(560, 601)).resolve(_one("call", rule)).picked["only"] == _contract("call", 600)


def test_a_watched_contract_with_no_quote_yet_stops_the_search() -> None:
    scene = _scene()
    just_added = _contract("call", 597)

    refused = scene.holding(just_added, None).refusal(_one("call", {"selector": "percent", "percent": 2.0}))

    assert refused.reason is ResolverRefusal.SEARCH_INCOMPLETE
    (candidate,) = [c for c in refused.evidence.legs[0].candidates if c.contract == just_added]
    assert candidate.passed_over is PassedOver.NO_QUOTE_HELD


def test_nothing_close_enough_needs_usable_quotes_on_both_sides_of_a_delta_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between 584 at 0.56 and 585 at 0.44 nothing is within 0.05 of 0.50. With 585's quote faulty the
    search is not over: the reason is the quote, not the chain."""
    _deltas(monkeypatch, {584.0: 0.56, 585.0: 0.44})
    rule = {"selector": "delta", "target": 0.5, "tolerance_delta": 0.05}
    scene = _scene()
    next_strike = _contract("call", 585)

    bracketed = scene.refusal(_one("call", rule))
    faulty = scene.holding(next_strike, replace(scene.quote_of(next_strike), ask=None)).refusal(_one("call", rule))

    assert (bracketed.reason, bracketed.fault) == (ResolverRefusal.NOTHING_CLOSE_ENOUGH, None)
    assert (faulty.reason, faulty.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.ONE_SIDED)


def test_a_contract_the_model_cannot_solve_is_unusable_for_a_delta_rule_and_usable_for_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deltas(monkeypatch, {584.0: 0.56, 585.0: AmericanIvStatus.BELOW_EXERCISE_VALUE, 586.0: 0.30})
    scene = _scene()

    by_delta = scene.refusal(_one("call", {"selector": "delta", "target": 0.5, "tolerance_delta": 0.05}))
    at_the_money = scene.resolve(_one("call", _AT_THE_MONEY))

    assert (by_delta.reason, by_delta.fault) == (ResolverRefusal.NO_USABLE_QUOTE, AmericanIvStatus.BELOW_EXERCISE_VALUE)
    (unsolved,) = [c for c in by_delta.evidence.legs[0].candidates if c.contract == _contract("call", 585)]
    assert (unsolved.passed_over, unsolved.priced.delta) == (PassedOver.NOT_SOLVED, None)
    # A rule that does not ask for a delta still picks it; its evidence says the delta was not solved.
    assert at_the_money.picked["only"] == _contract("call", 585)
    (picked,) = [c for c in at_the_money.evidence.legs[0].candidates if c.passed_over is None]
    assert picked.priced.status is AmericanIvStatus.BELOW_EXERCISE_VALUE


def test_a_delta_rule_with_no_usable_quote_at_all_names_the_fault_nearest_the_money() -> None:
    scene = _scene()
    stale = scene
    for strike in range(570, 601):
        contract = _contract("call", strike)
        stale = stale.holding(contract, _quote("1.00", "1.02", scene.selection_ms - 60_000))

    refused = stale.refusal(_one("call", {"selector": "delta", "target": 0.3}))

    assert (refused.reason, refused.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.STALE)
    assert "SPY261009C00585000" in str(refused)


# ---- Non-standard contracts -----------------------------------------------


@pytest.mark.parametrize(
    "odd",
    [
        pytest.param({"multiplier": 10}, id="ten shares a contract"),
        pytest.param({"shares_delivered": 150}, id="delivers 150 shares"),
        pytest.param({"exercise": "european"}, id="European exercise"),
    ],
)
def test_a_contract_that_is_not_standard_is_never_picked(odd: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    scene = _scene()
    at_the_money = _contract("call", 585)
    listings = tuple(replace(listing, **odd) if listing.contract == at_the_money else listing for listing in scene.chain.listings)
    scene = replace(scene, chain=replace(scene.chain, listings=listings))

    # The ladder a rule counts along has only standard contracts on it: the money is now 584, and one up is 586.
    assert scene.resolve(_one("call", _AT_THE_MONEY)).picked["only"] == _contract("call", 584)
    assert scene.resolve(_one("call", {"selector": "atm_offset", "offset": 1})).picked["only"] == _contract("call", 586)
    assert scene.refusal(_one("call", {"selector": "absolute", "strike": 585.0})).reason is ResolverRefusal.STRIKE_NOT_LISTED
    _deltas(monkeypatch, {584.0: 0.56, 585.0: 0.50, 586.0: 0.46})
    by_delta = scene.resolve(_one("call", {"selector": "delta", "target": 0.5, "tolerance_delta": 0.1}))
    assert by_delta.picked["only"] == _contract("call", 586)
    (passed,) = [c for c in by_delta.evidence.legs[0].candidates if c.contract == at_the_money]
    assert (passed.passed_over, passed.priced) == (PassedOver.NOT_STANDARD, None)


# ---- The feed, the underlying, and observations that belong together ------


def test_an_unhealthy_feed_a_silent_underlying_and_an_unusable_underlying_quote_are_three_reasons() -> None:
    scene = _scene()
    plan = _one("call", _AT_THE_MONEY)

    unhealthy = replace(scene, feed=FeedState(generation=_GENERATION, unhealthy="reconnecting")).refusal(plan)
    silent = replace(scene, underlying=replace(scene.underlying, pricing_bar_close=None)).refusal(plan)
    crossed = replace(
        scene, underlying=replace(scene.underlying, quote=_quote("585.10", "585.00", scene.selection_ms - 400))
    ).refusal(plan)
    unheld = replace(scene, underlying=replace(scene.underlying, quote=None)).refusal(plan)

    assert (unhealthy.reason, unhealthy.fault) == (ResolverRefusal.FEED_UNHEALTHY, None)
    assert "reconnecting" in str(unhealthy)
    assert (silent.reason, silent.fault) == (ResolverRefusal.UNDERLYING_DID_NOT_TRADE, None)
    assert (crossed.reason, crossed.fault) == (ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, QuoteFault.CROSSED)
    assert (unheld.reason, unheld.fault) == (ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, None)
    assert all(refused.evidence.legs == () and refused.leg_ids == () for refused in (unhealthy, silent, crossed, unheld))


def test_quotes_received_too_far_apart_are_refused_though_each_is_fresh() -> None:
    scene = _scene()
    spread = _plan(
        _leg("long_call", "call", _AT_THE_MONEY),
        _leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
    )
    sold = _contract("call", 590)
    quote = scene.quote_of(sold)

    def received(ago_ms: int) -> HeldQuote:
        when = scene.selection_ms - ago_ms
        return replace(quote, bid=QuoteSide(quote.bid.price, when), ask=QuoteSide(quote.ask.price, when))

    refused = scene.holding(sold, received(2_401)).refusal(spread)

    # The others were received 400 ms before the selection: 2,001 ms later than this one.
    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.OBSERVATIONS_APART, ("short_call",))
    assert scene.holding(sold, received(2_400)).resolve(spread).picked["short_call"] == sold


def test_one_side_of_one_quote_received_long_before_the_rest_is_enough_to_refuse() -> None:
    scene = _scene()
    contract = _contract("call", 585)
    quote = scene.quote_of(contract)
    old_bid = replace(quote, bid=QuoteSide(quote.bid.price, scene.selection_ms - 3_000))

    refused = scene.holding(contract, old_bid).refusal(_one("call", _AT_THE_MONEY))

    assert refused.reason is ResolverRefusal.OBSERVATIONS_APART


# ---- From a plan to a checked structure ------------------------------------


@pytest.mark.parametrize("name", list(NamedStructure))
def test_every_named_structures_starting_plan_resolves_to_contracts_that_pass_its_own_check(name: NamedStructure) -> None:
    """The whole way from rules to a structure: picked here, checked as the plan's legs and as the named
    shape, at the price the picks' own quotes give."""
    plan = starting_plan(name, "SPY")

    resolution = _scene().resolve(plan)

    structure = resolved_structure(plan, resolution.picked)
    net_price = natural_net_price(
        structure,
        {leg_id: LegQuote(bid=quote.bid.price, ask=quote.ask.price) for leg_id, quote in resolution.quotes.items()},
    )
    require_named_shape(plan, structure, net_price=net_price)
    assert {contract.expiry_date for contract in resolution.picked.values()} == {_FRIDAY}
    assert list(resolution.quotes) == [leg.leg_id for leg in plan.on_enter]


# ---- Evidence, and the recheck --------------------------------------------


def test_the_evidence_holds_what_the_pick_was_made_from() -> None:
    scene = _scene()
    seen_by_vendor = _contract("call", 585)
    scene = scene.holding(seen_by_vendor, scene.quote_of(seen_by_vendor), vendor_delta=0.52)

    evidence = scene.resolve(_one("call", {"selector": "delta", "target": 0.5})).evidence

    assert (evidence.decision_bar_close_ms, evidence.selection_ms) == (_at(_TUESDAY, 10, 31), _at(_TUESDAY, 10, 31, 2))
    assert (evidence.generation, evidence.money, evidence.model_spot) == (_GENERATION, Decimal("585.00"), 585.11)
    assert (evidence.model, evidence.limits, evidence.underlying_quote) == (_MODEL, _LIMITS, scene.underlying.quote)
    assert evidence.watched == tuple(sorted(scene.chain.watched))
    (leg,) = evidence.legs
    assert [c.contract.strike for c in leg.candidates] == [Decimal(strike) for strike in range(570, 601)]
    (with_vendor,) = [c for c in leg.candidates if c.contract == seen_by_vendor]
    assert with_vendor.vendor_delta == 0.52
    assert with_vendor.quote == scene.quote_of(seen_by_vendor)
    assert all(c.priced.status is AmericanIvStatus.SOLVED and c.priced.implied_volatility is not None for c in leg.candidates)
    assert [c.passed_over for c in leg.candidates].count(None) == 1


def test_a_plan_with_no_option_leg_has_nothing_to_resolve() -> None:
    stock = ActionPlan.model_validate(
        {
            "on_enter": [
                {"leg_id": "shares", "instrument": {"kind": "stock", "underlying": "SPY"}, "position": "long", "qty_ratio": 1}
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "shares"}],
        }
    )

    with pytest.raises(ValueError, match="no option leg"):
        _scene().resolve(stock)


def _spread() -> ActionPlan:
    return _plan(
        _leg("long_call", "call", _AT_THE_MONEY),
        _leg("short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}, position="short"),
    )


def _recheck(scene: _Scene, resolution: resolver.Resolution, later_ms: int) -> resolver.Recheck:
    return recheck(
        resolution,
        _spread(),
        chain=scene.chain,
        underlying=scene.underlying,
        feed=scene.feed,
        limits=scene.limits,
        recheck_ms=scene.selection_ms + later_ms,
    )


def test_a_recheck_passes_while_the_picked_quotes_still_stand_and_keeps_what_it_saw() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())

    seen = _recheck(scene, resolution, 1_500)

    assert seen.recheck_ms == scene.selection_ms + 1_500
    assert dict(seen.quotes) == {
        "long_call": scene.quote_of(_contract("call", 585)),
        "short_call": scene.quote_of(_contract("call", 590)),
    }
    assert seen.underlying_quote == scene.underlying.quote


def test_a_recheck_refuses_once_a_picked_quote_has_aged_past_the_limit() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(scene, resolution, 4_601)

    # Every quote here was received 400 ms before the selection; the underlying's is asked first.
    assert (refused.value.reason, refused.value.fault) == (ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, QuoteFault.STALE)
    assert refused.value.recheck.recheck_ms == scene.selection_ms + 4_601
    assert refused.value.evidence is resolution.evidence


@pytest.mark.parametrize(
    ("change", "reason", "fault"),
    [
        pytest.param("lost its bid", ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.BID_NOT_ABOVE_ZERO, id="the sold leg has no bid now"),
        pytest.param("not held", ResolverRefusal.SEARCH_INCOMPLETE, None, id="the sold leg's quote is no longer held"),
        pytest.param("reconnected", ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, QuoteFault.OLD_GENERATION, id="a reconnect since"),
        pytest.param("unhealthy", ResolverRefusal.FEED_UNHEALTHY, None, id="the feed went unhealthy"),
        pytest.param("apart", ResolverRefusal.OBSERVATIONS_APART, None, id="one leg ticked and the rest did not"),
    ],
)
def test_a_recheck_refuses_when_the_picks_no_longer_stand(
    change: str, reason: ResolverRefusal, fault: QuoteFault | None
) -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())
    sold = _contract("call", 590)
    quote = scene.quote_of(sold)
    later = {
        "lost its bid": lambda: scene.holding(sold, replace(quote, bid=QuoteSide(Decimal(0), quote.bid.received_ms))),
        "not held": lambda: scene.holding(sold, None),
        "reconnected": lambda: replace(scene, feed=FeedState(generation=_GENERATION + 1, unhealthy=None)),
        "unhealthy": lambda: replace(scene, feed=FeedState(generation=_GENERATION, unhealthy="data farm down")),
        "apart": lambda: scene.holding(
            sold,
            replace(
                quote,
                bid=QuoteSide(quote.bid.price, scene.selection_ms + 1_900),
                ask=QuoteSide(quote.ask.price, scene.selection_ms + 1_900),
            ),
        ),
    }[change]()

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(later, resolution, 2_000)

    assert (refused.value.reason, refused.value.fault) == (reason, fault)
    assert refused.value.recheck is not None
