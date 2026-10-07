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
_LIMITS = ResolverLimits(max_quote_age_ms=5_000, entry_cutoff_lead_ms=60 * 60_000)
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
        pytest.param(
            {"selector": "min_dte", "days": 9},
            _NEXT_FRIDAY,
            id="days are the calendar's: next Friday is ten of them away and eight sessions",
        ),
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
    (leg,) = refused.evidence.legs
    assert (leg.expiry_ms, leg.listed_strikes_thousandths, leg.candidates) == (None, (), ())
    # What the rule chose from: the expiry gone by is not among them.
    assert leg.listed_expiries_ms == (option_expiry_anchor_ms(_FRIDAY), option_expiry_anchor_ms(_NEXT_FRIDAY))


def test_an_expiry_listed_on_a_day_with_no_session_is_refused_as_that() -> None:
    """Good Friday 2026 is April 3rd. A chain that lists an expiry on it disagrees with the calendar."""
    good_friday = date(2026, 4, 3)
    scene = _scene(day=date(2026, 3, 30), expiries=(date(2026, 4, 10),))
    on_no_session = Listing(_contract("call", 585, good_friday), multiplier=100, shares_delivered=100, exercise="american")
    scene = replace(scene, chain=replace(scene.chain, listings=(*scene.chain.listings, on_no_session)))

    refused = scene.refusal(_one("call", {"selector": "delta", "target": 0.3}, expiry={"selector": "min_dte", "days": 1}))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.EXPIRY_NOT_A_SESSION, ("only",))
    assert (refused.evidence.legs[0].expiry_ms, refused.evidence.legs[0].candidates) == (option_expiry_anchor_ms(good_friday), ())
    # The week's last session is the Thursday, which lists nothing, so the nearest weekly is the next week's.
    assert scene.resolve(_one("call", _AT_THE_MONEY, expiry={"selector": "nearest_weekly"})).picked["only"].expiry_date == date(
        2026, 4, 10
    )


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
        # The expiry the rule came to is kept, though no strike was searched for.
        assert (refused.evidence.legs[0].expiry_ms, refused.evidence.legs[0].candidates) == (option_expiry_anchor_ms(day), ())
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


def test_a_percent_is_the_decimal_it_was_written_as() -> None:
    """A tenth of a percent above 500 is 500.50 exactly: 500 and 501 are as near, and 500 is nearer the money.
    The float nearest 0.1 is a hair above it, which would put the target nearer 501."""
    scene = _scene(money="500.00", listed=range(480, 521), watched=range(490, 511))

    assert scene.resolve(_one("call", {"selector": "percent", "percent": 0.1})).picked["only"] == _contract("call", 500)


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
    # Nothing was picked, so no usable contract is told another was nearer: the nearest is among them.
    assert {c.passed_over for c in refused.evidence.legs[0].candidates} == {PassedOver.LEG_REFUSED}
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
        pytest.param(
            _quote("1.03", None, _NOW - 9_000, data_type=QuoteDataType.DELAYED),
            True,
            QuoteFault.ONE_SIDED,
            id="whether it has arrived is asked before what it says",
        ),
    ],
)
def test_a_quote_is_usable_or_fails_as_one_thing(quote: HeldQuote, needs_a_bid: bool, fault: QuoteFault | None) -> None:
    assert quote_fault(quote, at_ms=_NOW, generation=_GENERATION, max_age_ms=5_000, needs_a_bid=needs_a_bid) is fault


@pytest.mark.parametrize(
    ("changed", "reason", "fault", "stands_as"),
    [
        pytest.param(
            {"bid": Decimal("9.99")}, ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.CROSSED, PassedOver.QUOTE_FAULT, id="crossed"
        ),
        pytest.param(
            {"data_type": QuoteDataType.DELAYED},
            ResolverRefusal.NO_USABLE_QUOTE,
            QuoteFault.NOT_LIVE,
            PassedOver.QUOTE_FAULT,
            id="delayed",
        ),
        pytest.param(
            {"ask": None}, ResolverRefusal.SEARCH_INCOMPLETE, QuoteFault.ONE_SIDED, PassedOver.QUOTE_NOT_ARRIVED, id="one side yet"
        ),
        pytest.param(
            {"generation": _GENERATION - 1},
            ResolverRefusal.SEARCH_INCOMPLETE,
            QuoteFault.OLD_GENERATION,
            PassedOver.QUOTE_NOT_ARRIVED,
            id="not quoted since the reconnect",
        ),
    ],
)
def test_a_rule_naming_one_contract_is_refused_with_what_is_wrong_with_its_quote(
    changed: dict, reason: ResolverRefusal, fault: QuoteFault, stands_as: PassedOver
) -> None:
    """A quote that fails a test is no usable quote. One that has not arrived whole leaves the search incomplete."""
    scene = _scene()
    contract = _contract("call", 585)
    quote = scene.quote_of(contract)
    if "bid" in changed:
        changed = {"bid": QuoteSide(changed["bid"], quote.bid.received_ms)}

    refused = scene.holding(contract, replace(quote, **changed)).refusal(_one("call", _AT_THE_MONEY))

    assert (refused.reason, refused.fault, refused.leg_ids) == (reason, fault, ("only",))
    assert refused.contract == contract
    (candidate,) = [c for c in refused.evidence.legs[0].candidates if c.contract == contract]
    assert (candidate.fault, candidate.passed_over) == (fault, stands_as)


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
    """2% above the price is 596.70. With 597's quote stale, 596 is the nearest usable, 0.70 away."""
    scene = _scene()
    nearest = _contract("call", 597)
    stale = scene.holding(nearest, _quote("0.10", "0.12", scene.selection_ms - 5_001))

    resolution = stale.resolve(_one("call", {"selector": "percent", "percent": 2.0}))

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
        scene = scene.holding(contract, replace(scene.quote_of(contract), data_type=QuoteDataType.FROZEN))

    refused = scene.refusal(_one("call", {"selector": "percent", "percent": 2.0}))

    assert (refused.reason, refused.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.NOT_LIVE)
    assert refused.contract == _contract("call", 597)
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
        pytest.param(
            "call",
            {"selector": "delta", "target": 0.02, "tolerance_delta": 0.001},
            601,
            id="a call delta past the window's top, with nothing in the window within its tolerance",
        ),
        pytest.param(
            "put",
            {"selector": "delta", "target": 0.02, "tolerance_delta": 0.001},
            569,
            id="a put delta past the window's bottom, with nothing in the window within its tolerance",
        ),
    ],
)
def test_a_target_outside_the_watched_window_is_search_incomplete(right: str, strike: dict, unsearched: int) -> None:
    refused = _scene().refusal(_one(right, strike))

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.SEARCH_INCOMPLETE, ("only",))
    assert refused.contract == _contract(right, unsearched)
    assert _contract(right, unsearched).occ_symbol in str(refused)


def test_the_money_outside_the_watched_window_is_search_incomplete() -> None:
    """The bar closed at 605, a listed strike nobody is watching. The money is not the nearest watched strike."""
    refused = _scene(money="605.00").refusal(_one("call", _AT_THE_MONEY))

    assert (refused.reason, refused.contract) == (ResolverRefusal.SEARCH_INCOMPLETE, _contract("call", 605))


def test_a_delta_past_the_last_listed_strike_is_nothing_close_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every listed strike is watched and the 600 call, the last, has 0.12. Nothing is left unsearched."""
    _deltas(monkeypatch, {599.0: 0.15, 600.0: 0.12})
    whole_chain_watched = _scene(listed=range(570, 601))

    refused = whole_chain_watched.refusal(_one("call", {"selector": "delta", "target": 0.02, "tolerance_delta": 0.05}))

    assert (refused.reason, refused.fault, refused.contract) == (ResolverRefusal.NOTHING_CLOSE_ENOUGH, None, None)


def test_a_delta_rule_with_no_quote_arrived_for_any_contract_is_search_incomplete() -> None:
    """Nothing is known of any call: that is a window not yet filled, not a chain of faulty quotes."""
    refused = _scene(watched=range(0)).refusal(_one("call", {"selector": "delta", "target": 0.3}))

    assert (refused.reason, refused.fault) == (ResolverRefusal.SEARCH_INCOMPLETE, None)


def test_a_strike_rule_with_nothing_listed_near_its_target_is_nothing_close_enough_whatever_is_watched() -> None:
    """9% above the price is 637.65, and the chain's last strike is 610. No quote could change that."""
    nothing_watched = _scene(watched=range(0))

    refused = nothing_watched.refusal(_one("call", {"selector": "percent", "percent": 9.0}))

    assert refused.reason is ResolverRefusal.NOTHING_CLOSE_ENOUGH


@pytest.mark.parametrize(
    ("beyond", "tolerance", "reason", "fault", "turns_on"),
    [
        pytest.param("no quote", 0.05, ResolverRefusal.SEARCH_INCOMPLETE, None, 595, id="within tolerance, an unwatched strike beyond"),
        pytest.param("no quote", 0.01, ResolverRefusal.SEARCH_INCOMPLETE, None, 595, id="outside it, an unwatched strike beyond"),
        pytest.param("one side", 0.05, ResolverRefusal.SEARCH_INCOMPLETE, QuoteFault.ONE_SIDED, 595, id="one not arrived beyond"),
        pytest.param("usable", 0.01, ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.STALE, 594, id="outside it, a usable strike beyond"),
        pytest.param("not listed", 0.01, ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.STALE, 594, id="outside it, the chain ends"),
    ],
)
def test_a_delta_rule_looks_past_a_faulty_quote_toward_its_target(
    monkeypatch: pytest.MonkeyPatch, beyond: str, tolerance: float, reason: ResolverRefusal, fault: QuoteFault | None, turns_on: int
) -> None:
    """The 593 call has 0.25 and the target is 0.20. The 594 call's quote is stale, so it is passed over, and
    what lies beyond it decides: a contract nothing is known of could have been the pick."""
    _deltas(monkeypatch, {592.0: 0.30, 593.0: 0.25, 595.0: 0.15})
    scene = _scene(listed=range(560, 595 if beyond == "not listed" else 611))
    scene = scene.holding(_contract("call", 594), _quote("0.40", "0.42", scene.selection_ms - 5_001))
    next_one = _contract("call", 595)
    if beyond == "no quote":
        scene = scene.holding(next_one, None)
    elif beyond == "one side":
        scene = scene.holding(next_one, replace(scene.quote_of(next_one), bid=None))

    refused = scene.refusal(_one("call", {"selector": "delta", "target": 0.20, "tolerance_delta": tolerance}))

    assert (refused.reason, refused.fault, refused.contract) == (reason, fault, _contract("call", turns_on))


@pytest.mark.parametrize("beyond", ["usable", "not listed"])
def test_a_delta_rule_keeps_its_pick_when_nothing_unknown_lies_past_the_faulty_quote(
    monkeypatch: pytest.MonkeyPatch, beyond: str
) -> None:
    """As above with the 593 call inside the tolerance: past the stale 594 is a usable 595 beyond the target,
    or the end of the chain. Either way nothing could have been nearer."""
    _deltas(monkeypatch, {592.0: 0.30, 593.0: 0.25, 595.0: 0.15})
    scene = _scene(listed=range(560, 595 if beyond == "not listed" else 611))
    scene = scene.holding(_contract("call", 594), _quote("0.40", "0.42", scene.selection_ms - 5_001))

    resolution = scene.resolve(_one("call", {"selector": "delta", "target": 0.20, "tolerance_delta": 0.05}))

    assert resolution.picked["only"] == _contract("call", 593)
    passed = {c.contract.strike: c.passed_over for c in resolution.evidence.legs[0].candidates}
    assert (passed[Decimal(593)], passed[Decimal(594)]) == (None, PassedOver.QUOTE_FAULT)


def test_a_delta_rule_does_not_end_its_walk_at_a_usable_contract_still_short_of_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Far from the money a cent of quote puts deltas out of order: the 594 call reads 0.26, above the 593's
    0.25. A target of 0.20 still lies past both, so the 594 is not where the search ends, and the 595, at
    0.16 the nearest, is the pick only while its quote is held."""
    _deltas(monkeypatch, {592.0: 0.30, 593.0: 0.25, 594.0: 0.26, 595.0: 0.16, 596.0: 0.10})
    rule = {"selector": "delta", "target": 0.20, "tolerance_delta": 0.05}
    scene = _scene()
    just_added = _contract("call", 595)

    refused = scene.holding(just_added, None).refusal(_one("call", rule))

    assert scene.resolve(_one("call", rule)).picked["only"] == just_added
    assert (refused.reason, refused.contract) == (ResolverRefusal.SEARCH_INCOMPLETE, just_added)
    assert "SPY261009C00593000 has 0.2500" in str(refused)


def test_a_delta_rule_that_passed_over_several_names_the_first_toward_its_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """From the 593 call at 0.25 toward 0.20: the 594 is stale and the 595 crossed, then the 596 lies beyond."""
    _deltas(monkeypatch, {592.0: 0.30, 593.0: 0.25, 596.0: 0.10})
    scene = _scene()
    scene = scene.holding(_contract("call", 594), _quote("0.40", "0.42", scene.selection_ms - 5_001))
    scene = scene.holding(_contract("call", 595), _quote("0.50", "0.30", scene.selection_ms - 400))

    refused = scene.refusal(_one("call", {"selector": "delta", "target": 0.20, "tolerance_delta": 0.01}))

    assert (refused.reason, refused.fault, refused.contract) == (
        ResolverRefusal.NO_USABLE_QUOTE,
        QuoteFault.STALE,
        _contract("call", 594),
    )


def test_a_put_delta_rule_looks_past_a_faulty_quote_down_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A put's delta shrinks as the strike falls: from the 577 put at 0.25 a target of 0.20 lies below it."""
    _deltas(monkeypatch, {578.0: -0.30, 577.0: -0.25})
    scene = _scene()
    scene = scene.holding(_contract("put", 576), _quote("0.40", "0.42", scene.selection_ms - 5_001))
    scene = scene.holding(_contract("put", 575), None)

    refused = scene.refusal(_one("put", {"selector": "delta", "target": 0.20}))

    assert (refused.reason, refused.contract) == (ResolverRefusal.SEARCH_INCOMPLETE, _contract("put", 575))


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


@pytest.mark.parametrize(
    ("arrived", "fault"),
    [
        pytest.param("nothing", None, id="no quote yet"),
        pytest.param("one side", QuoteFault.ONE_SIDED, id="its ask and not its bid"),
        pytest.param("neither side", QuoteFault.ONE_SIDED, id="a quote with neither side in it"),
        pytest.param("before the reconnect", QuoteFault.OLD_GENERATION, id="only a quote from before the reconnect"),
    ],
)
def test_a_watched_contract_whose_quote_has_not_arrived_stops_the_search(arrived: str, fault: QuoteFault | None) -> None:
    """2% above the price is 596.70, and 597 is the nearest. Nothing is known of it yet, so 596 is not picked
    in its place as it would be were 597's quote faulty."""
    scene = _scene()
    just_added = _contract("call", 597)
    quote = scene.quote_of(just_added)
    held = {
        "nothing": None,
        "one side": replace(quote, bid=None),
        "neither side": replace(quote, bid=None, ask=None),
        "before the reconnect": replace(quote, generation=_GENERATION - 1),
    }[arrived]

    refused = scene.holding(just_added, held).refusal(_one("call", {"selector": "percent", "percent": 2.0}))

    assert (refused.reason, refused.fault, refused.contract) == (ResolverRefusal.SEARCH_INCOMPLETE, fault, just_added)
    assert just_added.occ_symbol in str(refused)
    (candidate,) = [c for c in refused.evidence.legs[0].candidates if c.contract == just_added]
    assert (candidate.passed_over, candidate.fault) == (PassedOver.QUOTE_NOT_ARRIVED, fault)


def test_nothing_close_enough_needs_usable_quotes_on_both_sides_of_a_delta_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between 584 at 0.56 and 585 at 0.44 nothing is within 0.05 of 0.50. With 585's quote faulty the
    reason is the quote, not the chain."""
    _deltas(monkeypatch, {584.0: 0.56, 585.0: 0.44, 586.0: 0.30})
    rule = {"selector": "delta", "target": 0.5, "tolerance_delta": 0.05}
    scene = _scene()
    next_strike = _contract("call", 585)
    crossed = replace(scene.quote_of(next_strike), bid=QuoteSide(Decimal("99"), scene.selection_ms - 400))

    bracketed = scene.refusal(_one("call", rule))
    faulty = scene.holding(next_strike, crossed).refusal(_one("call", rule))

    assert (bracketed.reason, bracketed.fault, bracketed.contract) == (ResolverRefusal.NOTHING_CLOSE_ENOUGH, None, None)
    assert (faulty.reason, faulty.fault, faulty.contract) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.CROSSED, next_strike)


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


def test_a_delta_rule_whose_every_watched_quote_arrived_faulty_names_the_one_nearest_the_money() -> None:
    """Every watched call's quote has arrived, and is a minute old. The twenty listed strikes nobody watches do
    not make this a search to wait on: what is wrong is the quotes held."""
    scene = _scene()
    stale = scene
    for strike in range(570, 601):
        contract = _contract("call", strike)
        stale = stale.holding(contract, _quote("1.00", "1.02", scene.selection_ms - 60_000))

    refused = stale.refusal(_one("call", {"selector": "delta", "target": 0.3}))

    assert (refused.reason, refused.fault, refused.contract) == (
        ResolverRefusal.NO_USABLE_QUOTE,
        QuoteFault.STALE,
        _contract("call", 585),
    )
    assert "SPY261009C00585000" in str(refused)


@pytest.mark.parametrize(
    ("arrived_at", "wrong_with_it"),
    [
        pytest.param(572, "crossed", id="the one arrival is crossed"),
        pytest.param(570, "unsolved", id="the one arrival is under its exercise value"),
    ],
)
def test_a_delta_rule_with_one_faulty_arrival_and_the_rest_not_arrived_is_search_incomplete(
    monkeypatch: pytest.MonkeyPatch, arrived_at: int, wrong_with_it: str
) -> None:
    """Just after a reconnect one call's quote has arrived, and cannot be used. Thirty that could be the pick
    are still unknown, so the reason is theirs: the one nearest the money is named, not the faulty arrival."""
    _deltas(monkeypatch, {float(arrived_at): AmericanIvStatus.BELOW_EXERCISE_VALUE})
    scene = _scene()
    reconnecting = scene
    for strike in range(570, 601):
        contract = _contract("call", strike)
        quote = scene.quote_of(contract)
        if strike != arrived_at:
            quote = replace(quote, generation=_GENERATION - 1)
        elif wrong_with_it == "crossed":
            quote = replace(quote, bid=QuoteSide(Decimal("99"), scene.selection_ms - 400))
        reconnecting = reconnecting.holding(contract, quote)

    refused = reconnecting.refusal(_one("call", {"selector": "delta", "target": 0.3}))

    assert (refused.reason, refused.fault, refused.contract) == (
        ResolverRefusal.SEARCH_INCOMPLETE,
        QuoteFault.OLD_GENERATION,
        _contract("call", 585),
    )
    assert "SPY261009C00585000 has not been quoted since the last reconnect" in str(refused)


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


def test_two_legs_too_far_apart_are_refused_though_each_is_within_the_window_of_the_underlying() -> None:
    """The bought leg's quote came 1,100 ms before the underlying's and the sold leg's 1,100 ms after it."""
    scene = _scene()
    bought, sold = _contract("call", 585), _contract("call", 590)
    at_the_underlying = scene.selection_ms - 1_500
    scene = replace(scene, underlying=replace(scene.underlying, quote=_received(scene.underlying.quote, at_the_underlying)))

    def with_legs_received(before_ms: int, after_ms: int) -> _Scene:
        return scene.holding(bought, _received(scene.quote_of(bought), at_the_underlying - before_ms)).holding(
            sold, _received(scene.quote_of(sold), at_the_underlying + after_ms)
        )

    refused = with_legs_received(1_100, 1_100).refusal(_spread())

    assert (refused.reason, refused.leg_ids) == (ResolverRefusal.OBSERVATIONS_APART, ("long_call", "short_call"))
    assert with_legs_received(1_000, 1_000).resolve(_spread()).picked["short_call"] == sold


def test_an_ask_received_long_before_the_rest_is_enough_to_refuse() -> None:
    scene = _scene()
    contract = _contract("call", 585)
    quote = scene.quote_of(contract)
    old_ask = replace(quote, ask=QuoteSide(quote.ask.price, scene.selection_ms - 3_000))

    refused = scene.holding(contract, old_ask).refusal(_one("call", _AT_THE_MONEY))

    assert refused.reason is ResolverRefusal.OBSERVATIONS_APART


@pytest.mark.parametrize("close", ["NaN", "0", "-585.00", "Infinity"])
def test_a_bar_close_that_is_no_price_is_the_callers_mistake_not_a_refusal(close: str) -> None:
    scene = _scene()

    with pytest.raises(ValueError, match="a price above zero"):
        replace(scene, underlying=replace(scene.underlying, pricing_bar_close=Decimal(close))).resolve(_one("call", _AT_THE_MONEY))


@pytest.mark.parametrize("limit", ["max_quote_age_ms", "entry_cutoff_lead_ms", "coherence_window_ms"])
def test_a_limit_below_zero_is_the_callers_mistake(limit: str) -> None:
    """A cutoff lead below zero would put the cutoff after the contract's last trade, and offer it expired."""
    limits = {"max_quote_age_ms": 5_000, "entry_cutoff_lead_ms": 60 * 60_000, "coherence_window_ms": 2_000}

    with pytest.raises(ValueError, match=limit):
        ResolverLimits(**limits | {limit: -1})
    assert getattr(ResolverLimits(**limits | {limit: 0}), limit) == 0


@pytest.mark.parametrize(
    "wrong",
    [
        pytest.param({"tree_steps": 100}, id="an even number of steps"),
        pytest.param({"tree_steps": 1}, id="one step"),
        pytest.param({"rate": float("nan")}, id="a rate that is no number"),
        pytest.param({"rate": float("inf")}, id="a rate without end"),
        pytest.param({"dividends": (DividendOn(ex_ms=0, cash=0.0),)}, id="a dividend of nothing"),
        pytest.param({"dividends": (DividendOn(ex_ms=0, cash=float("nan")),)}, id="a dividend that is no number"),
    ],
)
def test_model_inputs_the_pricer_has_no_meaning_for_are_the_callers_mistake_not_a_fault_of_every_quote(
    wrong: dict,
) -> None:
    with pytest.raises(ValueError):
        replace(_MODEL, **wrong)


def test_a_selection_cannot_come_before_the_close_of_the_bar_it_was_decided_on() -> None:
    scene = _scene()

    with pytest.raises(ValueError, match="before the close of the bar"):
        replace(scene, decision_bar_close_ms=scene.selection_ms + 1).resolve(_one("call", _AT_THE_MONEY))
    assert replace(scene, decision_bar_close_ms=scene.selection_ms).resolve(_one("call", _AT_THE_MONEY)).picked


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


def test_the_evidence_holds_the_ladder_a_count_along_the_strikes_was_made_on() -> None:
    """A 585.50 call is listed and nobody watches it. Two strikes up from the money at 585 is 586, not 587, and
    only the chain's own list says why."""
    scene = _scene()
    between = Listing(_contract("call", "585.5"), multiplier=100, shares_delivered=100, exercise="american")
    not_standard = Listing(_contract("call", "586.5"), multiplier=10, shares_delivered=100, exercise="american")
    scene = replace(scene, chain=replace(scene.chain, listings=(*scene.chain.listings, between, not_standard)))

    resolution = scene.resolve(_one("call", {"selector": "atm_offset", "offset": 2}))

    (leg,) = resolution.evidence.legs
    assert resolution.picked["only"] == _contract("call", 586)
    assert leg.listed_strikes_thousandths == tuple(sorted([*range(560_000, 611_000, 1_000), 585_500]))
    assert leg.listed_expiries_ms == tuple(option_expiry_anchor_ms(day) for day in _EXPIRIES)
    # The candidates are the watched ones only: the 585.50 is on the ladder and not among them.
    assert Decimal("585.5") not in {c.contract.strike for c in leg.candidates}


def test_a_leg_that_asks_for_no_delta_is_not_told_the_model_could_not_solve_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first leg picks by delta, so the model is asked about every call, and cannot solve the 585. The
    second leg names the 590: to it the 585 is one more contract that was not the pick."""
    _deltas(monkeypatch, {584.0: 0.52, 585.0: AmericanIvStatus.BELOW_EXERCISE_VALUE, 590.0: 0.30})
    plan = _plan(
        _leg("by_delta", "call", {"selector": "delta", "target": 0.5}),
        _leg("named", "call", {"selector": "absolute", "strike": 590.0}, position="short"),
    )

    by_delta, named = _scene().resolve(plan).evidence.legs

    def unsolved(leg: resolver.LegEvidence) -> resolver.Candidate:
        (candidate,) = [c for c in leg.candidates if c.contract == _contract("call", 585)]
        return candidate

    assert (unsolved(by_delta).passed_over, unsolved(by_delta).priced.status) == (
        PassedOver.NOT_SOLVED,
        AmericanIvStatus.BELOW_EXERCISE_VALUE,
    )
    assert (unsolved(named).passed_over, unsolved(named).priced) == (PassedOver.NOT_THE_NEAREST, None)
    # The pick is priced whichever rule picked it.
    (picked,) = [c for c in named.candidates if c.passed_over is None]
    assert (picked.contract, picked.priced.delta) == (_contract("call", 590), 0.30)


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
        watched=scene.chain.watched,
        underlying=scene.underlying,
        feed=scene.feed,
        recheck_ms=scene.selection_ms + later_ms,
    )


def test_a_recheck_passes_while_the_picked_quotes_still_stand_and_keeps_what_it_saw() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())

    seen = _recheck(scene, resolution, 1_500)

    assert (seen.recheck_ms, seen.generation) == (scene.selection_ms + 1_500, _GENERATION)
    assert dict(seen.quotes) == {
        "long_call": scene.quote_of(_contract("call", 585)),
        "short_call": scene.quote_of(_contract("call", 590)),
    }
    assert seen.underlying_quote == scene.underlying.quote


def _received(quote: HeldQuote, received_ms: int, **changes: object) -> HeldQuote:
    """The same prices, both sides received again at ``received_ms``."""
    return replace(
        quote, bid=QuoteSide(quote.bid.price, received_ms), ask=QuoteSide(quote.ask.price, received_ms), **changes
    )


def _ticked_again(scene: _Scene, received_ms: int, *, but: tuple[OptionContract, ...] = (), **changes: object) -> _Scene:
    """The scene with the underlying's quote and every option's received again at ``received_ms``, but for ``but``."""
    watched = {
        symbol: held
        if symbol in {contract.occ_symbol for contract in but}
        else replace(held, quote=_received(held.quote, received_ms, **changes))
        for symbol, held in scene.chain.watched.items()
    }
    return replace(
        scene,
        chain=replace(scene.chain, watched=watched),
        underlying=replace(scene.underlying, quote=_received(scene.underlying.quote, received_ms, **changes)),
    )


def test_a_recheck_refuses_a_picked_leg_whose_quote_has_aged_past_the_limit_and_names_the_leg() -> None:
    """Everything else ticked again a moment before the recheck; the sold leg's quote is as it was at the selection."""
    scene = _scene()
    resolution = scene.resolve(_spread())
    sold = _contract("call", 590)

    def recheck_at(later_ms: int) -> resolver.Recheck:
        return _recheck(_ticked_again(scene, scene.selection_ms + later_ms - 300, but=(sold,)), resolution, later_ms)

    with pytest.raises(ContractsNotResolved) as refused:
        recheck_at(4_601)

    # The sold leg's quote was received 400 ms before the selection: 5,001 ms before this recheck.
    assert (refused.value.reason, refused.value.fault) == (ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.STALE)
    assert (refused.value.leg_ids, refused.value.contract) == (("short_call",), sold)
    assert refused.value.recheck.recheck_ms == scene.selection_ms + 4_601
    assert refused.value.evidence is resolution.evidence
    # A moment sooner it is as old as the limit and no older, and still within the window of the rest.
    with pytest.raises(ContractsNotResolved) as apart:
        recheck_at(4_600)
    assert apart.value.reason is ResolverRefusal.OBSERVATIONS_APART


def test_a_recheck_refuses_once_the_underlyings_quote_has_aged_past_the_limit() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(scene, resolution, 4_601)

    assert (refused.value.reason, refused.value.fault) == (ResolverRefusal.UNDERLYING_QUOTE_UNUSABLE, QuoteFault.STALE)
    assert (refused.value.leg_ids, refused.value.contract) == ((), None)


def test_a_recheck_asks_a_bid_of_a_leg_to_be_sold_and_not_of_one_to_be_bought() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())
    bought, sold = _contract("call", 585), _contract("call", 590)

    def without_a_bid(contract: OptionContract) -> _Scene:
        quote = scene.quote_of(contract)
        return scene.holding(contract, replace(quote, bid=QuoteSide(Decimal(0), quote.bid.received_ms)))

    assert resolution.sold == {"short_call"}
    assert _recheck(without_a_bid(bought), resolution, 1_500).quotes["long_call"].bid.price == 0
    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(without_a_bid(sold), resolution, 1_500)
    assert (refused.value.reason, refused.value.fault, refused.value.leg_ids) == (
        ResolverRefusal.NO_USABLE_QUOTE,
        QuoteFault.BID_NOT_ABOVE_ZERO,
        ("short_call",),
    )


def test_a_recheck_refuses_a_reconnect_since_the_selection_though_every_quote_has_arrived_again() -> None:
    """ADR 0067: a generation change between the selection and the recheck is refused even when the new
    generation is already healthy. Every quote here is fresh, live, and of the new generation."""
    scene = _scene()
    resolution = scene.resolve(_spread())
    reconnected = replace(
        _ticked_again(scene, scene.selection_ms + 1_400, generation=_GENERATION + 1),
        feed=FeedState(generation=_GENERATION + 1, unhealthy=None),
    )

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(reconnected, resolution, 1_500)

    assert (refused.value.reason, refused.value.fault, refused.value.leg_ids) == (ResolverRefusal.FEED_RECONNECTED, None, ())
    assert refused.value.recheck.generation == _GENERATION + 1
    # The same quotes on the generation the picks were made in pass.
    assert _recheck(_ticked_again(scene, scene.selection_ms + 1_400), resolution, 1_500).recheck_ms == scene.selection_ms + 1_500


@pytest.mark.parametrize(
    ("unhealthy", "reason"),
    [
        pytest.param(None, ResolverRefusal.FEED_RECONNECTED, id="a reconnect is named before the quotes it emptied"),
        pytest.param("data farm down", ResolverRefusal.FEED_UNHEALTHY, id="an unhealthy feed is named before its reconnect"),
    ],
)
def test_a_recheck_names_the_feed_before_the_quotes_a_reconnect_left_behind(
    unhealthy: str | None, reason: ResolverRefusal
) -> None:
    """Just after a reconnect nothing has arrived again: the underlying has no quote and every option's is of
    the generation gone. The reason is the reconnect, not any one of the quotes it left behind."""
    scene = _scene()
    resolution = scene.resolve(_spread())
    reconnected = replace(
        scene,
        underlying=replace(scene.underlying, quote=None),
        feed=FeedState(generation=_GENERATION + 1, unhealthy=unhealthy),
    )

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(reconnected, resolution, 1_500)

    assert (refused.value.reason, refused.value.fault, refused.value.leg_ids) == (reason, None, ())


@pytest.mark.parametrize(
    ("change", "reason", "fault"),
    [
        pytest.param("not held", ResolverRefusal.NO_USABLE_QUOTE, None, id="the sold leg's quote is no longer held"),
        pytest.param("one side", ResolverRefusal.NO_USABLE_QUOTE, QuoteFault.ONE_SIDED, id="the sold leg has lost its ask"),
        pytest.param(
            "another generation",
            ResolverRefusal.NO_USABLE_QUOTE,
            QuoteFault.OLD_GENERATION,
            id="the sold leg's quote is of a generation the feed is not in",
        ),
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
        "not held": lambda: scene.holding(sold, None),
        "one side": lambda: scene.holding(sold, replace(quote, ask=None)),
        "another generation": lambda: scene.holding(sold, replace(quote, generation=_GENERATION + 1)),
        "unhealthy": lambda: replace(scene, feed=FeedState(generation=_GENERATION, unhealthy="data farm down")),
        "apart": lambda: scene.holding(sold, _received(quote, scene.selection_ms + 1_900)),
    }[change]()

    with pytest.raises(ContractsNotResolved) as refused:
        _recheck(later, resolution, 2_000)

    assert (refused.value.reason, refused.value.fault) == (reason, fault)
    assert refused.value.recheck is not None


def test_a_recheck_cannot_come_before_the_selection_it_rechecks() -> None:
    scene = _scene()
    resolution = scene.resolve(_spread())

    with pytest.raises(ValueError, match="before the selection"):
        _recheck(scene, resolution, -1)
    assert _recheck(scene, resolution, 0).recheck_ms == scene.selection_ms
