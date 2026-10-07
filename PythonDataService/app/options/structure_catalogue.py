"""What the structure builder offers: where each named structure starts, the rules a leg may be given, and
the order terms a Deploy starts from.

Every value here is a starting point the owner may change before deploying,
and none is read after that: a deployed plan carries its own rules. What a
name fixes is in ``app.options.named_structures``; what its picked contracts
must look like is in ``app.options.structure_check``.

The rules and the order terms are not defined here. A rule is the Action
Plan's own type: its tolerance starts at that type's own default, and what
each of its numbers may be is read off that type's own fields. An order term
starts at the default the code that enforces it holds, and its range is
``app.options.order_terms``'s. Only the words a screen shows beside each are
written here, once, on the server.

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

from app.options import order_terms
from app.options.bounds import Bounds, bounds_of
from app.options.contract import MAX_STRIKE_THOUSANDTHS, OptionRight, symbol_expiry_range_ms
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


# A structure built leg by leg, under no name.
CUSTOM_LABEL = "Custom"
_CUSTOM_IN_A_SENTENCE = "a custom option structure"


def label(name: NamedStructure | None) -> str:
    """The name as a list writes it: "Bull call spread". A structure with no name is "Custom"."""
    return CUSTOM_LABEL if name is None else name.in_words.capitalize()


def with_its_article(name: NamedStructure) -> str:
    """The name as a sentence takes it: "a bull call spread", "an iron condor"."""
    return f"{'an' if name.in_words[0] in 'aeiou' else 'a'} {name.in_words}"


def in_a_sentence(name: NamedStructure | None) -> str:
    """The name as it stands before its underlying in a sentence: "a bull call spread on". The symbol follows."""
    return f"{_CUSTOM_IN_A_SENTENCE if name is None else with_its_article(name)} on"


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

    @property
    def value_bounds(self) -> Bounds | None:
        """What the number in the box beside the rule may be. None: the rule has no box."""
        return None if self.value_field is None else _held_to(self.rule, self.value_field)

    @property
    def tolerance_bounds(self) -> Bounds | None:
        """What the rule's "Within" may be. None: the rule has none."""
        return None if self.tolerance_field is None else _held_to(self.rule, self.tolerance_field)


_THOUSANDTH = Decimal("0.001")
# No two strikes a contract symbol can write are as far apart as the largest of them.
_FARTHEST_APART = Decimal(MAX_STRIKE_THOUSANDTHS) * _THOUSANDTH


def _held_to(rule: type[BaseModel], field: str) -> Bounds:
    """What the Action Plan holds a rule's number to.

    Most bounds sit on the field and are read off it. Two are held by a
    validator of the rule's own, and are said here from the constants that
    validator reads: a dollar distance is whole thousandths and under the
    largest strike a symbol writes, and a fixed date is one a symbol's year
    can write. A test sends a number each side of every bound through the
    route, so that what is said here cannot drift from what is enforced.
    """
    on_the_field = bounds_of(rule, field)
    if rule is LegDistanceStrike:
        if on_the_field.lowest is None:
            return Bounds(-_FARTHEST_APART, False, _FARTHEST_APART, False, _THOUSANDTH)
        return Bounds(on_the_field.lowest, on_the_field.lowest_included, _FARTHEST_APART, False, _THOUSANDTH)
    if rule is AbsoluteExpiry:
        first, last = symbol_expiry_range_ms()
        # An expiry is its date's 16:00 New York anchor: not every millisecond between the two is one.
        return Bounds(Decimal(first), True, Decimal(last), True, None)
    return on_the_field


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
    """One option order term as a Deploy first shows it: its words, the default the code holds for it, and
    what it may be."""

    label: str
    unit_label: str
    default: Decimal
    bounds: Bounds
    # What the term does, for the "(i)" beside it.
    help: str


def order_term_starts(terms: CloseTerms | None = None) -> dict[str, OrderTermStart]:
    """The four terms the owner sets for an option bot, by the name of each in its own unit."""
    terms = terms or CloseTerms()
    return {
        "gap_limit_percent": OrderTermStart(
            "Widest bid-ask gap, each leg",
            "% of mid",
            DEFAULT_LEG_GAP_LIMIT_PERCENT,
            order_terms.GAP_LIMIT_PERCENT,
            "An entry is skipped when any leg's bid and ask are further apart than this, as a percent of the "
            "price halfway between them.",
        ),
        "entry_wait_seconds": OrderTermStart(
            "Entry wait",
            "seconds",
            Decimal(order_terms.entry_wait_seconds(terms)),
            order_terms.ENTRY_WAIT_SECONDS,
            "How long an entry order waits for its fill. What has not filled by then is cancelled.",
        ),
        "cost_limit_percent": OrderTermStart(
            "Whole-structure cost",
            "% of cash set aside",
            DEFAULT_COST_LIMIT_PERCENT,
            order_terms.COST_LIMIT_PERCENT,
            "An entry is skipped when opening the whole structure and closing it at once, at the quoted prices, "
            "would cost more than this percent of what the entry sets aside.",
        ),
        "expiry_close_lead_minutes": OrderTermStart(
            "Expiry-day close",
            "min before close",
            Decimal(order_terms.expiry_close_lead_minutes(terms)),
            order_terms.EXPIRY_CLOSE_LEAD_MINUTES,
            "On the day a held contract expires, the position is closed this long before the session closes.",
        ),
    }


# What How says under the option order terms, for every option bot.
ENTRIES_SENTENCE = (
    "Entries are limit orders at the quoted price. An entry that has not filled when its wait is over is cancelled."
)
EXITS_SENTENCE = "Exits are market orders. A market order has no price limit."
HOURS_SENTENCE = "Regular hours only."
# How long before expiry Alpaca's Trading API page says it may act on a position itself. Not verified on the account.
ALPACA_SELL_OUT_WINDOW_MINUTES = 60
ALPACA_SENTENCE = (
    "Alpaca says that when an account cannot afford to exercise an expiring position it sells the position "
    "itself, within 1 hour before expiry."
)


def expiry_close_sentence(lead_minutes: int) -> str:
    """Where the owner's expiry-day close falls against the hour Alpaca names."""
    if lead_minutes > ALPACA_SELL_OUT_WINDOW_MINUTES:
        return f"A close {lead_minutes} minutes before the session closes comes before that hour begins."
    if lead_minutes == ALPACA_SELL_OUT_WINDOW_MINUTES:
        return f"A close {lead_minutes} minutes before the session closes comes as that hour begins."
    return f"A close {lead_minutes} minutes before the session closes is inside that hour."
