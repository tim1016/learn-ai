"""Action-plan schema: what a bot opens when its signal says ENTER and closes when it says EXIT.

ADR 0012 fixes the invariants this schema encodes:
* every entry leg carries a stable ``leg_id`` (regex
  ``^[a-z0-9_]{1,32}$``) so the contract a leg resolved to can be kept
  under it (§3);
* every leg carries an explicit ``instrument.underlying`` — no implicit
  fallback from ``live_config.symbol`` (§5);
* ``qty_ratio`` is a declarative positive integer; composition against
  a sizing policy was deferred to Slice 4 (§4), and the
  ``live_config.sizing`` policy it named was deleted (#2602);
* exit entries are lifecycle actions — only ``close_leg``, which
  references an entry by ``entry_leg_id``. Exits NEVER redeclare
  selectors (§3);
* a fixed-date expiry is one ``int64`` ms UTC instant, the date's
  16:00 New York anchor (ADR 0022 (i)).

An **option structure** (#2913) is a plan with an option leg. It is held to
rules a stock plan is not, because it goes to the broker as one order and
its exit must close exactly what its entry opened: see
``ActionPlan._an_option_structure_is_one_coverable_order``.
"""

from __future__ import annotations

from collections import Counter
from math import gcd
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_validator

from app.options.contract import MAX_STRIKE_THOUSANDTHS, require_symbol_expiry, strike_thousandths
from app.options.expiry import OptionExpiryMs

# ``leg_id`` regex from ADR 0012 §3 — lowercase ASCII + digits + underscore,
# bounded to 32 chars so it embeds cleanly in logs, parquet column names,
# and future broker-side ``orderRef`` tags without escaping.
_LEG_ID_PATTERN = r"^[a-z0-9_]{1,32}$"


class StockInstrument(BaseModel):
    kind: Literal["stock"]
    underlying: Annotated[str, Field(min_length=1)]


class OptionInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["option"]
    underlying: Annotated[str, Field(min_length=1)]


# ---- Strike rules ---------------------------------------------------------
#
# "The money" is the listed strike nearest the underlying's price. Each rule
# that can miss carries its own tolerance, in its own unit: a pick farther
# from its target than that is no pick, and the entry is skipped.


class AtmStrike(BaseModel):
    """The strike at the money."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["atm"]


class AtmOffsetStrike(BaseModel):
    """A whole number of listed strikes above (positive) or below the money."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["atm_offset"]
    offset: int


class AbsoluteStrike(BaseModel):
    """One named strike, in dollars."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["absolute"]
    strike: float = Field(gt=0)

    @field_validator("strike")
    @classmethod
    def _a_strike_a_contract_can_carry(cls, strike: float) -> float:
        if strike_thousandths(strike) > MAX_STRIKE_THOUSANDTHS:
            raise ValueError(f"a contract symbol cannot write a strike of {strike}")
        return strike


class DeltaStrike(BaseModel):
    """The strike whose delta is nearest a target.

    ``target`` is the size of the delta, a positive number below one. The
    leg's call or put gives it its sign: 0.30 on a put means a delta of -0.30.
    """

    model_config = ConfigDict(extra="forbid")

    selector: Literal["delta"]
    target: Annotated[float, Field(gt=0, lt=1)]
    tolerance_delta: Annotated[float, Field(gt=0, lt=1)] = 0.05


class LegDistanceStrike(BaseModel):
    """A strike a signed dollar distance above (positive) or below another leg's strike."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["leg_distance"]
    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    dollars: Annotated[float, Field(allow_inf_nan=False)]
    # Zero: the strike that distance away must itself be listed.
    tolerance_dollars: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0

    @field_validator("dollars", "tolerance_dollars")
    @classmethod
    def _dollars_a_strike_can_differ_by(cls, dollars: float) -> float:
        # Strikes are listed to a thousandth of a dollar, so a distance is too.
        strike_thousandths(abs(dollars))
        # Zero has one spelling: a plan with -0.0 must not hash apart from the same plan with 0.0.
        return dollars + 0.0


class PercentStrike(BaseModel):
    """A strike a signed percent of the underlying's price above (positive) or below that price."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["percent"]
    percent: Annotated[float, Field(gt=-100, le=1000)]
    # In percent of the underlying's price. Above zero: no listed strike sits at an exact percent.
    tolerance_percent: Annotated[float, Field(gt=0, le=100)] = 0.5

    @field_validator("percent")
    @classmethod
    def _zero_has_one_spelling(cls, percent: float) -> float:
        return percent + 0.0


StrikeSelector = Annotated[
    AtmStrike | AtmOffsetStrike | AbsoluteStrike | DeltaStrike | LegDistanceStrike | PercentStrike,
    Field(discriminator="selector"),
]


# ---- Expiry rules ---------------------------------------------------------
#
# Days are counted on the New York calendar, from the decision's date.


class MinDteExpiry(BaseModel):
    """The nearest listed expiry at least ``days`` calendar days out."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["min_dte"]
    days: Annotated[int, Field(ge=1)]


class NearestWeeklyExpiry(BaseModel):
    """The nearest listed expiry after the decision's date that falls on the last session of its week.

    Never the decision date's own expiry, which is ``same_day``'s to pick: on
    a Friday this is the following week's. A monthly or quarterly expiry that
    falls on such a day counts.
    """

    model_config = ConfigDict(extra="forbid")

    selector: Literal["nearest_weekly"]


class SameDayExpiry(BaseModel):
    """The expiry on the decision's own date."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["same_day"]


class AbsoluteExpiry(BaseModel):
    """One named expiry date, as its 16:00 New York anchor."""

    model_config = ConfigDict(extra="forbid")

    selector: Literal["absolute"]
    expiration_ms: OptionExpiryMs

    @field_validator("expiration_ms")
    @classmethod
    def _an_expiry_a_contract_can_carry(cls, expiration_ms: int) -> int:
        return require_symbol_expiry(expiration_ms)


ExpirySelector = Annotated[
    MinDteExpiry | NearestWeeklyExpiry | SameDayExpiry | AbsoluteExpiry,
    Field(discriminator="selector"),
]


# ---- Entry-leg variants ---------------------------------------------------


class StockEntryLeg(BaseModel):
    """Stock ``ActionEntity`` — Slice 1B."""

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    instrument: StockInstrument
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1)]


class OptionEntryLeg(BaseModel):
    """Option ``ActionEntity`` — Slice 1C.

    Adds ``right``, ``strike``, ``expiry`` on top of the common entry-leg
    fields. ``leg_id`` is the same stable identity stock legs carry —
    each option leg of a multi-leg structure resolves to its own
    ``conId`` at consumption time, even when several legs share the same
    expiry-selector inputs.
    """

    model_config = ConfigDict(extra="forbid")

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    instrument: OptionInstrument
    position: Literal["long", "short"]
    qty_ratio: Annotated[int, Field(ge=1)]
    right: Literal["call", "put"]
    strike: StrikeSelector
    expiry: ExpirySelector


def _entry_leg_kind(v: object) -> str | None:
    """Pull the nested ``instrument.kind`` discriminator for the
    ``ActionEntity`` union. Pydantic returns a clear "unknown
    discriminator value" error when this returns an unexpected string,
    and a "missing discriminator field" error when it returns ``None``."""

    if isinstance(v, dict):
        instrument = v.get("instrument")
        if isinstance(instrument, dict):
            return instrument.get("kind")
        return None
    instrument = getattr(v, "instrument", None)
    return getattr(instrument, "kind", None)


# Discriminated on ``instrument.kind`` so a malformed option leg cannot
# silently fall back to the stock variant. Slice 1B = stock only;
# Slice 1C adds the option variant.
ActionEntity = Annotated[
    Annotated[StockEntryLeg, Tag("stock")] | Annotated[OptionEntryLeg, Tag("option")],
    Discriminator(_entry_leg_kind),
]


class CloseLegExit(BaseModel):
    """``close_leg`` ``ExitEntity`` — Slice 1B."""

    kind: Literal["close_leg"]
    entry_leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]


# The most option legs one order carries at the broker.
MAX_OPTION_LEGS = 4


class ActionPlan(BaseModel):
    """Operator-declared instrument plan, hashed into ``run_id``. Hard schema rejections live here."""

    model_config = ConfigDict(extra="forbid")

    on_enter: list[ActionEntity] = Field(default_factory=list)
    on_exit: list[CloseLegExit] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_referential_integrity(self) -> ActionPlan:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for leg in self.on_enter:
            if leg.leg_id in seen:
                duplicates.add(leg.leg_id)
            seen.add(leg.leg_id)
        if duplicates:
            raise ValueError(
                f"duplicate leg_id within action plan: {sorted(duplicates)}"
            )
        for exit_entry in self.on_exit:
            if exit_entry.entry_leg_id not in seen:
                raise ValueError(
                    f"close_leg references unknown entry_leg_id "
                    f"{exit_entry.entry_leg_id!r}; on_enter declares "
                    f"{sorted(seen)}"
                )
        return self

    @model_validator(mode="after")
    def _an_option_structure_is_one_coverable_order(self) -> ActionPlan:
        """Hold a plan with an option leg to what one order can carry and one exit can close (#2913).

        Whether the contracts the rules pick form the structure meant is a
        later question, asked of the resolved contracts. These are the rules
        the plan can break by itself.
        """
        legs = [leg for leg in self.on_enter if isinstance(leg, OptionEntryLeg)]
        if not legs:
            return self
        if len(legs) != len(self.on_enter):
            raise ValueError("an option structure has no stock leg beside its option legs")
        if len(legs) > MAX_OPTION_LEGS:
            raise ValueError(f"an option structure has one to {MAX_OPTION_LEGS} legs; this one has {len(legs)}")
        underlyings = sorted({leg.instrument.underlying.upper() for leg in legs})
        if len(underlyings) != 1:
            raise ValueError(f"every leg of an option structure is on one underlying; these name {underlyings}")
        _require_each_leg_closed_once(legs, self.on_exit)
        common = gcd(*(leg.qty_ratio for leg in legs))
        if common > 1:
            raise ValueError(
                f"the leg ratios share the divisor {common}; a structure is written in its lowest terms, "
                "and its size is how many of it the bot opens"
            )
        _require_sold_legs_coverable(legs)
        _require_strike_references_resolve(legs)
        return self


def _require_each_leg_closed_once(legs: list[OptionEntryLeg], exits: list[CloseLegExit]) -> None:
    """The exit closes exactly what the entry opened: every leg once, and nothing else."""
    closes = Counter(exit_entry.entry_leg_id for exit_entry in exits)
    wrong = {leg.leg_id: closes[leg.leg_id] for leg in legs if closes[leg.leg_id] != 1}
    if wrong:
        told = ", ".join(f"{leg_id!r} is closed {count} times" for leg_id, count in sorted(wrong.items()))
        raise ValueError(f"the exit of an option structure closes every entry leg exactly once; {told}")


def _require_sold_legs_coverable(legs: list[OptionEntryLeg]) -> None:
    """Each sold call needs a bought call and each sold put a bought put, one for one.

    Counted here; that the bought contract really covers the sold one waits
    for their strikes and expiries.
    """
    for right in ("call", "put"):
        sold = sum(leg.qty_ratio for leg in legs if leg.right == right and leg.position == "short")
        bought = sum(leg.qty_ratio for leg in legs if leg.right == right and leg.position == "long")
        if sold > bought:
            raise ValueError(
                f"the structure sells {sold} {right}(s) and buys {bought}; "
                f"every sold {right} needs a bought {right} to cover it"
            )


def _require_strike_references_resolve(legs: list[OptionEntryLeg]) -> None:
    """A strike measured from another leg names a leg of this plan, and following such references ends."""
    measured_from = {leg.leg_id: leg.strike.leg_id for leg in legs if isinstance(leg.strike, LegDistanceStrike)}
    known = {leg.leg_id for leg in legs}
    for leg_id, other in sorted(measured_from.items()):
        if other not in known:
            raise ValueError(f"leg {leg_id!r} measures its strike from {other!r}, which is not a leg of this plan")
    for start in sorted(measured_from):
        path = [start]
        while path[-1] in measured_from:
            following = measured_from[path[-1]]
            if following in path:
                loop = " -> ".join([*path[path.index(following) :], following])
                raise ValueError(
                    f"legs measure their strikes from each other in a loop ({loop}); "
                    "one of them must get its strike another way"
                )
            path.append(following)


def traded_instrument(action_plan: ActionPlan) -> StockInstrument | OptionInstrument:
    """What this plan trades: the one instrument its entry legs are in, by type and identity.

    The one answer to "what does this bot trade". A bot's binding symbol is
    its Signal stream; what it trades is read here, never guessed from that.
    """
    instruments = {(leg.instrument.kind, leg.instrument.underlying.upper()) for leg in action_plan.on_enter}
    if len(instruments) != 1:
        raise ValueError(
            "an Action Plan trades exactly one instrument; "
            f"its entry legs name {sorted(instruments)}"
        )
    return action_plan.on_enter[0].instrument


def trades_the_signals_shares(action_plan: ActionPlan, signal_symbol: str) -> bool:
    """Whether the plan trades shares of the symbol the bot listens to.

    Decided by the instrument's type and identity, not by comparing tickers:
    options on the signal's own symbol are something else.
    """
    instrument = traded_instrument(action_plan)
    return instrument.kind == "stock" and instrument.underlying.upper() == signal_symbol.upper()


def traded_market_symbol(action_plan: ActionPlan, signal_symbol: str) -> str | None:
    """The second symbol a bot needs live: the one its orders are in, when that is not the symbol it listens to.

    ``None`` when they are one symbol, which one check covers. Start admission
    and the running bot's ENTER gate both ask about exactly the symbol it
    listens to and this one, so a Start never admits a bot whose every ENTER
    would be refused over a symbol nobody asked about (#2912).
    """
    traded = traded_instrument(action_plan).underlying.upper()
    return None if traded == signal_symbol.upper() else traded


class TradedInstrumentView(BaseModel):
    """What a bot trades, when that is not shares of the symbol it listens to (#2912).

    A bot's ``symbol`` is its Signal stream: what it listens to. This is on
    its row only when it trades something else, so its presence is the
    server's one answer to "does this bot trade something else". The answer
    comes from the instrument's type and identity, never from a screen
    comparing tickers: options on the bot's own symbol are something else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stock", "option"]
    # The stock or ETF traded; for options, the symbol the contracts are on.
    symbol: str

    @classmethod
    def of(cls, action_plan: ActionPlan, *, signal_symbol: str) -> TradedInstrumentView | None:
        """The plan's traded instrument, or ``None`` when the bot trades the shares it listens to."""
        if trades_the_signals_shares(action_plan, signal_symbol):
            return None
        instrument = traded_instrument(action_plan)
        return cls(kind=instrument.kind, symbol=instrument.underlying.upper())


def order_symbol(symbol: str, traded: TradedInstrumentView | None) -> str:
    """The symbol a bot's orders and positions are in: what it trades, which is ``symbol`` unless it says otherwise."""
    return symbol if traded is None else traded.symbol
