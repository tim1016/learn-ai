"""The check of a structure once its rules have picked contracts, and where each named structure starts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.options.contract import OptionContract
from app.options.named_structures import NamedStructure
from app.options.structure_catalogue import starting_plan
from app.options.structure_check import StructureRefusal, StructureRefused, require_named_shape, resolved_structure
from app.schemas.action_plan import ActionPlan, DeltaStrike

_EXPIRY = date(2026, 12, 18)
_A_MONTH_LATER = date(2027, 1, 15)


def _contract(spelled: str, *, expiry: date = _EXPIRY, root: str = "SPY") -> OptionContract:
    """``"C585"`` is the 585 call, ``"P572.5"`` the 572.50 put."""
    return OptionContract.of(
        root=root, expiry_date=expiry, right="call" if spelled[0] == "C" else "put", strike=Decimal(spelled[1:])
    )


def _picked(**legs: str) -> dict[str, OptionContract]:
    return {leg_id: _contract(spelled) for leg_id, spelled in legs.items()}


# Each named structure with contracts that have its shape, and a price per share of the right sign.
_AS_NAMED: dict[NamedStructure, tuple[dict[str, str], str]] = {
    NamedStructure.LONG_CALL: ({"long_call": "C585"}, "3.10"),
    NamedStructure.LONG_PUT: ({"long_put": "P585"}, "2.90"),
    NamedStructure.BULL_CALL_SPREAD: ({"long_call": "C585", "short_call": "C590"}, "1.94"),
    NamedStructure.BEAR_PUT_SPREAD: ({"long_put": "P585", "short_put": "P580"}, "1.80"),
    NamedStructure.BULL_PUT_SPREAD: ({"short_put": "P575", "long_put": "P570"}, "-1.20"),
    NamedStructure.BEAR_CALL_SPREAD: ({"short_call": "C595", "long_call": "C600"}, "-1.10"),
    NamedStructure.LONG_STRADDLE: ({"long_call": "C585", "long_put": "P585"}, "6.00"),
    NamedStructure.LONG_STRANGLE: ({"long_put": "P575", "long_call": "C595"}, "3.00"),
    NamedStructure.IRON_CONDOR: (
        {"long_put": "P570", "short_put": "P575", "short_call": "C595", "long_call": "C600"},
        "-2.30",
    ),
    NamedStructure.IRON_BUTTERFLY: (
        {"long_put": "P580", "short_put": "P585", "short_call": "C585", "long_call": "C590"},
        "-3.80",
    ),
    NamedStructure.CALL_BUTTERFLY: ({"lower_wing": "C580", "body": "C585", "upper_wing": "C590"}, "1.10"),
    NamedStructure.PUT_BUTTERFLY: ({"lower_wing": "P580", "body": "P585", "upper_wing": "P590"}, "1.10"),
}


def _check(name: NamedStructure, *, net_price: str | None = None, **changed_picks: str) -> None:
    """Resolve the named structure's starting plan to its as-named contracts, with some changed, and check it."""
    picks, as_named_price = _AS_NAMED[name]
    structure = resolved_structure(starting_plan(name, "SPY"), _picked(**(picks | changed_picks)))
    require_named_shape(name, structure, net_price=Decimal(net_price or as_named_price))


@pytest.mark.parametrize("name", list(NamedStructure))
def test_contracts_with_a_named_structures_shape_pass_as_it(name: NamedStructure) -> None:
    picks, _ = _AS_NAMED[name]
    plan = starting_plan(name, "SPY")

    structure = resolved_structure(plan, _picked(**picks))

    _check(name)
    assert [(leg.leg_id, leg.position, leg.ratio) for leg in structure.legs] == [
        (leg.leg_id, leg.position, leg.qty_ratio) for leg in plan.on_enter
    ]


@pytest.mark.parametrize(
    ("name", "changes", "refusal", "names_legs"),
    [
        pytest.param(
            NamedStructure.BULL_CALL_SPREAD,
            {"long_call": "C590", "short_call": "C585"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_call", "short_call"),
            id="a bull call spread whose strikes come out reversed",
        ),
        pytest.param(
            NamedStructure.BULL_CALL_SPREAD,
            {"short_call": "C585"},
            StructureRefusal.SAME_CONTRACT,
            ("long_call", "short_call"),
            id="a bull call spread whose strikes come out equal: two legs on one contract",
        ),
        pytest.param(
            NamedStructure.BULL_CALL_SPREAD,
            {"net_price": "-0.10"},
            StructureRefusal.PREMIUM_NOT_PAID,
            ("long_call", "short_call"),
            id="a bull call spread that would be paid to open",
        ),
        pytest.param(
            NamedStructure.BEAR_PUT_SPREAD,
            {"net_price": "0"},
            StructureRefusal.PREMIUM_NOT_PAID,
            ("long_put", "short_put"),
            id="a bear put spread that would cost nothing",
        ),
        pytest.param(
            NamedStructure.BEAR_PUT_SPREAD,
            {"short_put": "P590"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_put", "short_put"),
            id="a bear put spread that sells the higher put",
        ),
        pytest.param(
            NamedStructure.BULL_PUT_SPREAD,
            {"long_put": "P580"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("short_put", "long_put"),
            id="a bull put spread that buys the higher put",
        ),
        pytest.param(
            NamedStructure.BULL_PUT_SPREAD,
            {"net_price": "0.05"},
            StructureRefusal.PREMIUM_NOT_RECEIVED,
            ("short_put", "long_put"),
            id="a bull put spread that would cost money to open",
        ),
        pytest.param(
            NamedStructure.BULL_PUT_SPREAD,
            {"net_price": "-5.00"},
            StructureRefusal.CREDIT_NOT_UNDER_WIDTH,
            ("long_put", "short_put"),
            id="a five-dollar put spread sold for five dollars",
        ),
        pytest.param(
            NamedStructure.BEAR_CALL_SPREAD,
            {"net_price": "-5.01"},
            StructureRefusal.CREDIT_NOT_UNDER_WIDTH,
            ("short_call", "long_call"),
            id="a five-dollar call spread sold for more than five dollars",
        ),
        pytest.param(
            NamedStructure.BEAR_CALL_SPREAD,
            {"long_call": "C590"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("short_call", "long_call"),
            id="a bear call spread that buys the lower call",
        ),
        pytest.param(
            NamedStructure.LONG_STRADDLE,
            {"long_put": "P584"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_put", "long_call"),
            id="a straddle on two strikes",
        ),
        pytest.param(
            NamedStructure.LONG_STRANGLE,
            {"long_put": "P595"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_put", "long_call"),
            id="a strangle on one strike, which is a straddle",
        ),
        pytest.param(
            NamedStructure.LONG_STRANGLE,
            {"long_put": "P600"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_put", "long_call"),
            id="a strangle whose put is above its call",
        ),
        pytest.param(
            NamedStructure.IRON_CONDOR,
            {"short_put": "P595", "long_put": "P590"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("short_put", "short_call"),
            id="an iron condor whose sold legs share a strike, which is an iron butterfly",
        ),
        pytest.param(
            NamedStructure.IRON_CONDOR,
            {"long_call": "C590"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("short_call", "long_call"),
            id="an iron condor whose bought call is inside its sold one",
        ),
        pytest.param(
            NamedStructure.IRON_CONDOR,
            {"net_price": "0.40"},
            StructureRefusal.PREMIUM_NOT_RECEIVED,
            ("long_put", "short_put", "short_call", "long_call"),
            id="an iron condor that would cost money to open",
        ),
        pytest.param(
            NamedStructure.IRON_BUTTERFLY,
            {"short_put": "P584"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("short_put", "short_call"),
            id="an iron butterfly whose sold legs are a dollar apart",
        ),
        pytest.param(
            NamedStructure.IRON_BUTTERFLY,
            {"long_put": "P590"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("long_put", "short_put"),
            id="an iron butterfly whose bought put is above the middle",
        ),
        pytest.param(
            NamedStructure.CALL_BUTTERFLY,
            {"upper_wing": "C595"},
            StructureRefusal.WINGS_UNEVEN,
            ("lower_wing", "upper_wing"),
            id="a call butterfly with one wing five wide and the other ten",
        ),
        pytest.param(
            NamedStructure.PUT_BUTTERFLY,
            {"lower_wing": "P590", "upper_wing": "P595"},
            StructureRefusal.STRIKES_NOT_AS_NAMED,
            ("lower_wing", "body"),
            id="a put butterfly with both wings above its body",
        ),
    ],
)
def test_contracts_without_the_named_shape_are_refused_by_rule_and_leg(
    name: NamedStructure, changes: dict[str, str], refusal: StructureRefusal, names_legs: tuple[str, ...]
) -> None:
    with pytest.raises(StructureRefused) as refused:
        _check(name, **changes)

    assert refused.value.refusal is refusal
    assert sorted(refused.value.leg_ids) == sorted(names_legs)
    assert name.in_words in str(refused.value) or refusal is StructureRefusal.SAME_CONTRACT


def test_a_named_structure_has_one_expiry() -> None:
    """A bought call that outlives the sold one covers it, so the legs are a structure; they are not a bull call spread."""
    picks = _picked(short_call="C590") | {"long_call": _contract("C585", expiry=_A_MONTH_LATER)}
    structure = resolved_structure(starting_plan(NamedStructure.BULL_CALL_SPREAD, "SPY"), picks)

    with pytest.raises(StructureRefused) as refused:
        require_named_shape(NamedStructure.BULL_CALL_SPREAD, structure, net_price=Decimal("4.00"))

    assert refused.value.refusal is StructureRefusal.MORE_THAN_ONE_EXPIRY


def test_a_covering_leg_that_expires_first_does_not_cover() -> None:
    picks = _picked(long_call="C585") | {"short_call": _contract("C590", expiry=_A_MONTH_LATER)}

    with pytest.raises(StructureRefused) as refused:
        resolved_structure(starting_plan(NamedStructure.BULL_CALL_SPREAD, "SPY"), picks)

    assert refused.value.refusal is StructureRefusal.SOLD_LEG_NOT_COVERED
    assert refused.value.leg_ids == ("short_call",)


def test_cover_is_counted_contract_for_contract_among_those_that_last() -> None:
    """Two calls sold for January; of the two bought, only one lasts until then."""
    plan = ActionPlan.model_validate(
        {
            "on_enter": [
                {
                    "leg_id": leg_id,
                    "instrument": {"kind": "option", "underlying": "SPY"},
                    "position": position,
                    "qty_ratio": ratio,
                    "right": "call",
                    "strike": {"selector": "atm_offset", "offset": offset},
                    "expiry": {"selector": "min_dte", "days": days},
                }
                for leg_id, position, ratio, offset, days in (
                    ("sold", "short", 2, 5, 30),
                    ("lasting", "long", 1, 0, 30),
                    ("short_lived", "long", 1, 10, 7),
                )
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": leg_id} for leg_id in ("sold", "lasting", "short_lived")],
        }
    )
    january = {"sold": _contract("C590", expiry=_A_MONTH_LATER), "lasting": _contract("C585", expiry=_A_MONTH_LATER)}

    with pytest.raises(StructureRefused) as refused:
        resolved_structure(plan, january | {"short_lived": _contract("C595")})
    covered = resolved_structure(plan, january | {"short_lived": _contract("C595", expiry=_A_MONTH_LATER)})

    assert refused.value.refusal is StructureRefusal.SOLD_LEG_NOT_COVERED
    assert refused.value.leg_ids == ("sold",)
    assert len(covered.legs) == 3


@pytest.mark.parametrize(
    ("picks", "names_legs"),
    [
        pytest.param({"long_call": _contract("C585")}, ("short_call",), id="a leg nothing was picked for"),
        pytest.param(
            _picked(long_call="C585", short_call="C590", stray="C600"), ("stray",), id="a contract for a leg the plan does not have"
        ),
        pytest.param(_picked(long_call="P585", short_call="C590"), ("long_call",), id="a put picked for a call leg"),
        pytest.param(
            _picked(long_call="C585") | {"short_call": _contract("C590", root="QQQ")},
            ("short_call",),
            id="a contract on another underlying",
        ),
    ],
)
def test_picks_that_are_not_the_plans_legs_are_refused(picks: dict[str, OptionContract], names_legs: tuple[str, ...]) -> None:
    with pytest.raises(StructureRefused) as refused:
        resolved_structure(starting_plan(NamedStructure.BULL_CALL_SPREAD, "spy"), picks)

    assert refused.value.refusal is StructureRefusal.NOT_THE_PLANS_LEGS
    assert refused.value.leg_ids == names_legs


def test_a_structure_is_not_checked_against_a_name_it_is_not_made_as() -> None:
    structure = resolved_structure(starting_plan(NamedStructure.LONG_CALL, "SPY"), _picked(long_call="C585"))

    with pytest.raises(StructureRefused) as refused:
        require_named_shape(NamedStructure.BULL_CALL_SPREAD, structure, net_price=Decimal("3.10"))

    assert refused.value.refusal is StructureRefusal.NOT_MADE_AS_NAMED


def test_a_shape_is_not_judged_on_a_price_it_cannot_be_exact_about() -> None:
    structure = resolved_structure(starting_plan(NamedStructure.LONG_CALL, "SPY"), _picked(long_call="C585"))

    with pytest.raises(ValueError, match="exact decimal"):
        require_named_shape(NamedStructure.LONG_CALL, structure, net_price=3.10)  # type: ignore[arg-type]


# Where a rule by delta lands in these tests: at the money for 0.50, ten dollars out of it for 0.30.
_STRIKE_AT_DELTA = {("call", 0.5): 585, ("put", 0.5): 585, ("call", 0.3): 595, ("put", 0.3): 575}
_RECEIVES_PREMIUM = {
    NamedStructure.BULL_PUT_SPREAD,
    NamedStructure.BEAR_CALL_SPREAD,
    NamedStructure.IRON_CONDOR,
    NamedStructure.IRON_BUTTERFLY,
}


@pytest.mark.parametrize("name", list(NamedStructure))
def test_every_starting_plan_comes_to_the_structure_it_is_named_for(name: NamedStructure) -> None:
    """Follow each starting plan's own rules on a ladder of strikes: the distances and their signs
    must land on the named shape, or the builder would open on a plan that fails its own check."""
    plan = starting_plan(name, "SPY")
    strikes: dict[str, Decimal] = {}
    waiting = list(plan.on_enter)
    while waiting:
        leg = waiting.pop(0)
        if isinstance(leg.strike, DeltaStrike):
            strikes[leg.leg_id] = Decimal(_STRIKE_AT_DELTA[(leg.right, leg.strike.target)])
        elif leg.strike.leg_id in strikes:
            strikes[leg.leg_id] = strikes[leg.strike.leg_id] + Decimal(str(leg.strike.dollars))
        else:
            waiting.append(leg)
    picked = {
        leg.leg_id: OptionContract.of(root="SPY", expiry_date=_EXPIRY, right=leg.right, strike=strikes[leg.leg_id])
        for leg in plan.on_enter
    }

    structure = resolved_structure(plan, picked)

    assert plan.structure is name
    require_named_shape(name, structure, net_price=Decimal("-1.00" if name in _RECEIVES_PREMIUM else "1.00"))
