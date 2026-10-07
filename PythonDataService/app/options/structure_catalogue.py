"""What the structure builder offers: where each named structure starts, the rules a leg may be given, and
the order terms a Deploy starts from.

Every value here is a starting point the owner may change before deploying,
and none is read after that: a deployed plan carries its own rules. What a
name fixes is in ``app.options.named_structures``; what its picked contracts
must look like is in ``app.options.structure_check``.

The rules and the order terms are not defined here. A rule is the Action
Plan's own type, and its tolerance starts at that type's own default; an
order term starts at the default the code that enforces it holds. Only the
words a screen shows beside each are written here, once, on the server.

Most structures hang on one leg picked by delta and place the others a
dollar distance from it, so the shape holds wherever the price is. The
strangle and the iron condor each pick two legs by delta, a put and a call
out of the money, and their shape holds while those two deltas land on
either side of the price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, get_args

from pydantic import BaseModel

from app.options.contract import OptionRight
from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT
from app.options.named_structures import NamedStructure
from app.options.structure import LegPosition
from app.schemas.action_plan import (
    AbsoluteExpiry,
    ActionPlan,
    AtmOffsetStrike,
    CloseLegExit,
    DeltaStrike,
    LegDistanceStrike,
    MinDteExpiry,
    NearestWeeklyExpiry,
    OptionEntryLeg,
    OptionInstrument,
    PercentStrike,
    SameDayExpiry,
    StrikeSelector,
)

# The starting delta of a leg at the money, and of one out of it.
_AT_THE_MONEY = 0.50
_OUT_OF_THE_MONEY = 0.30
# The starting distance between neighbouring strikes, in dollars.
_WIDTH = 5.0


@dataclass(frozen=True, slots=True)
class _Start:
    leg_id: str
    position: LegPosition
    right: OptionRight
    strike: StrikeSelector
    ratio: int = 1


def _by_delta(target: float) -> DeltaStrike:
    return DeltaStrike(selector="delta", target=target)


def _from(leg_id: str, dollars: float) -> LegDistanceStrike:
    return LegDistanceStrike(selector="leg_distance", leg_id=leg_id, dollars=dollars)


def _butterfly(right: OptionRight) -> tuple[_Start, ...]:
    return (
        _Start("lower_wing", "long", right, _from("body", -_WIDTH)),
        _Start("body", "short", right, _by_delta(_AT_THE_MONEY), ratio=2),
        _Start("upper_wing", "long", right, _from("body", _WIDTH)),
    )


_STARTS: dict[NamedStructure, tuple[_Start, ...]] = {
    NamedStructure.LONG_CALL: (_Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),),
    NamedStructure.LONG_PUT: (_Start("long_put", "long", "put", _by_delta(_AT_THE_MONEY)),),
    NamedStructure.BULL_CALL_SPREAD: (
        _Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),
        _Start("short_call", "short", "call", _from("long_call", _WIDTH)),
    ),
    NamedStructure.BEAR_PUT_SPREAD: (
        _Start("long_put", "long", "put", _by_delta(_AT_THE_MONEY)),
        _Start("short_put", "short", "put", _from("long_put", -_WIDTH)),
    ),
    NamedStructure.BULL_PUT_SPREAD: (
        _Start("short_put", "short", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_put", "long", "put", _from("short_put", -_WIDTH)),
    ),
    NamedStructure.BEAR_CALL_SPREAD: (
        _Start("short_call", "short", "call", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.LONG_STRADDLE: (
        _Start("long_call", "long", "call", _by_delta(_AT_THE_MONEY)),
        _Start("long_put", "long", "put", _from("long_call", 0.0)),
    ),
    NamedStructure.LONG_STRANGLE: (
        _Start("long_put", "long", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _by_delta(_OUT_OF_THE_MONEY)),
    ),
    NamedStructure.IRON_CONDOR: (
        _Start("long_put", "long", "put", _from("short_put", -_WIDTH)),
        _Start("short_put", "short", "put", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("short_call", "short", "call", _by_delta(_OUT_OF_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.IRON_BUTTERFLY: (
        _Start("long_put", "long", "put", _from("short_call", -_WIDTH)),
        _Start("short_put", "short", "put", _from("short_call", 0.0)),
        _Start("short_call", "short", "call", _by_delta(_AT_THE_MONEY)),
        _Start("long_call", "long", "call", _from("short_call", _WIDTH)),
    ),
    NamedStructure.CALL_BUTTERFLY: _butterfly("call"),
    NamedStructure.PUT_BUTTERFLY: _butterfly("put"),
}


def starting_plan(name: NamedStructure, underlying: str) -> ActionPlan:
    """The plan the builder shows when the owner picks ``name`` for ``underlying``: every leg, closed by the exit."""
    starts = _STARTS[name]
    return ActionPlan(
        structure=name,
        on_enter=[
            OptionEntryLeg(
                leg_id=start.leg_id,
                instrument=OptionInstrument(kind="option", underlying=underlying),
                position=start.position,
                qty_ratio=start.ratio,
                right=start.right,
                strike=start.strike,
                expiry=NearestWeeklyExpiry(selector="nearest_weekly"),
            )
            for start in starts
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id=start.leg_id) for start in starts],
    )


# How the builder lays the names out, and the one line it says of each, in the owner's words.
GROUPS: tuple[tuple[str, tuple[NamedStructure, ...]], ...] = (
    ("One leg", (NamedStructure.LONG_CALL, NamedStructure.LONG_PUT)),
    (
        "Two legs",
        (
            NamedStructure.BULL_CALL_SPREAD,
            NamedStructure.BEAR_PUT_SPREAD,
            NamedStructure.BULL_PUT_SPREAD,
            NamedStructure.BEAR_CALL_SPREAD,
            NamedStructure.LONG_STRADDLE,
            NamedStructure.LONG_STRANGLE,
        ),
    ),
    (
        "Three or four",
        (
            NamedStructure.CALL_BUTTERFLY,
            NamedStructure.PUT_BUTTERFLY,
            NamedStructure.IRON_CONDOR,
            NamedStructure.IRON_BUTTERFLY,
        ),
    ),
)

_DESCRIPTIONS: dict[NamedStructure, str] = {
    NamedStructure.LONG_CALL: "Buy a call. You pay up front.",
    NamedStructure.LONG_PUT: "Buy a put. You pay up front.",
    NamedStructure.BULL_CALL_SPREAD: "Buy a call, sell a higher call. You pay up front.",
    NamedStructure.BEAR_PUT_SPREAD: "Buy a put, sell a lower put. You pay up front.",
    NamedStructure.BULL_PUT_SPREAD: "Sell a put, buy a lower put. You are paid up front.",
    NamedStructure.BEAR_CALL_SPREAD: "Sell a call, buy a higher call. You are paid up front.",
    NamedStructure.LONG_STRADDLE: "Buy a call and a put at one strike. You pay up front.",
    NamedStructure.LONG_STRANGLE: "Buy a put, and a call at a higher strike. You pay up front.",
    NamedStructure.CALL_BUTTERFLY: "Buy a call, sell two at a higher strike, buy one the same distance higher again.",
    NamedStructure.PUT_BUTTERFLY: "Buy a put, sell two at a higher strike, buy one the same distance higher again.",
    NamedStructure.IRON_CONDOR: "Sell a put and a higher call, and buy one further out on each side. You are paid up front.",
    NamedStructure.IRON_BUTTERFLY: "Sell a put and a call at one strike, and buy one further out on each side. You are paid up front.",
}


def label(name: NamedStructure) -> str:
    """The name as a screen writes it: "Bull call spread"."""
    return name.in_words.capitalize()


def with_its_article(name: NamedStructure) -> str:
    """The name as a sentence takes it: "a bull call spread", "an iron condor"."""
    return f"{'an' if name.in_words[0] in 'aeiou' else 'a'} {name.in_words}"


def description(name: NamedStructure) -> str:
    """What the named structure is, in one line."""
    return _DESCRIPTIONS[name]


# ---- The rules a leg may be given ---------------------------------------------------

RuleUnit = Literal["delta", "strikes", "dollars", "percent", "days", "date"]


@dataclass(frozen=True, slots=True)
class RuleOffered:
    """One strike or expiry rule as the builder offers it: the Action Plan's own rule type, and the words for it."""

    rule: type[BaseModel]
    label: str
    # The field of the rule that the box beside it edits, with its unit and the word a screen writes for
    # the unit. None: the rule needs nothing beside it.
    value_field: str | None = None
    value_unit: RuleUnit | None = None
    value_label: str | None = None
    # The field that says how far from its target a pick may be: the builder's "Within". None: the rule
    # names its pick exactly.
    tolerance_field: str | None = None
    tolerance_unit: RuleUnit | None = None
    tolerance_label: str | None = None
    # The field that names the other leg the rule is measured from, by its ``leg_id``, and the word a screen
    # writes for it: the builder's "From". None: the rule is measured from no other leg.
    other_leg_field: str | None = None
    other_leg_label: str | None = None

    @property
    def selector(self) -> str:
        """The rule's own name on the wire: what its ``selector`` field must say."""
        (name,) = get_args(self.rule.model_fields["selector"].annotation)
        return name

    @property
    def tolerance_default(self) -> float | None:
        """The tolerance a leg starts with: the Action Plan's own default for the rule."""
        return None if self.tolerance_field is None else self.rule.model_fields[self.tolerance_field].default


# In the order the builder lists them.
STRIKE_RULES: tuple[RuleOffered, ...] = (
    RuleOffered(DeltaStrike, "Delta target", "target", "delta", "delta", "tolerance_delta", "delta", "delta"),
    RuleOffered(AtmOffsetStrike, "Steps from the money", "offset", "strikes", "strikes"),
    RuleOffered(
        LegDistanceStrike,
        "Distance from another leg",
        "dollars",
        "dollars",
        "dollars",
        "tolerance_dollars",
        "dollars",
        "dollars",
        other_leg_field="leg_id",
        other_leg_label="leg",
    ),
    RuleOffered(PercentStrike, "Percent from price", "percent", "percent", "percent", "tolerance_percent", "percent", "percent"),
)
EXPIRY_RULES: tuple[RuleOffered, ...] = (
    RuleOffered(MinDteExpiry, "At least N days out", "days", "days", "days"),
    RuleOffered(NearestWeeklyExpiry, "Nearest weekly"),
    RuleOffered(SameDayExpiry, "Same day"),
    RuleOffered(AbsoluteExpiry, "Fixed date", "expiration_ms", "date", "date"),
)


# ---- The order terms a Deploy starts from -------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderTermStart:
    """One option order term as a Deploy first shows it: its words, and the default the code holds for it."""

    label: str
    unit_label: str
    default: Decimal


def _whole(milliseconds: int, *, in_one: int, what: str) -> Decimal:
    """A close term in the unit a screen shows it in. A default that is no whole number of it is not shown rounded."""
    whole, left_over = divmod(milliseconds, in_one)
    if left_over:
        raise ValueError(f"{what} of {milliseconds} ms is not a whole number of its unit")
    return Decimal(whole)


def order_term_starts(terms: CloseTerms | None = None) -> dict[str, OrderTermStart]:
    """The four terms the owner sets for an option bot, by the name of each in its own unit."""
    terms = terms or CloseTerms()
    return {
        "gap_limit_percent": OrderTermStart("Widest bid-ask gap, each leg", "% of mid", DEFAULT_LEG_GAP_LIMIT_PERCENT),
        "entry_wait_seconds": OrderTermStart(
            "Entry wait", "seconds", _whole(terms.entry_wait_ms, in_one=1_000, what="an entry wait")
        ),
        "cost_limit_percent": OrderTermStart("Whole-structure cost", "% of cash set aside", DEFAULT_COST_LIMIT_PERCENT),
        "expiry_close_lead_minutes": OrderTermStart(
            "Expiry-day close",
            "min before close",
            _whole(terms.expiry_close_lead_ms, in_one=60_000, what="an expiry-day close lead"),
        ),
    }
