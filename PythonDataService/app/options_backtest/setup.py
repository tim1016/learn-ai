"""What an owner sets up for one options backtest (#3030).

A setup is a trading window, one to eight option legs, and the cost
assumptions the run is priced under. Each leg is bought or sold (#3105, part of
#3095), names how its contract is picked and may carry three rules of its own: a
stop loss, a momentum entry and a re-entry after a stop.

The setup says nothing about dates: the same setup is replayed over whatever
sessions a run asks for. Its times are distances from a session's open or its
close, so a half day keeps them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from app.options.contract import OptionRight

SETUP_VERSION: Literal[1] = 1
MAX_LEGS = 8
MAX_REENTRIES = 20
MAX_CONTRACTS = 1000
MAX_STRIKE_STEPS = 50
MAX_EXPIRY_DAYS = 730

MS_PER_MINUTE = 60_000
_MS_PER_DAY = 24 * 60 * MS_PER_MINUTE

# The same shape a bot's leg id has, so a leg reads alike in a log on either side.
_LEG_ID_PATTERN = r"^[a-z0-9_]{1,32}$"
# The same pattern under a name other modules may import: a request that names a leg takes the shape the setup gives it.
LEG_ID_PATTERN = _LEG_ID_PATTERN
# An OCC root: the letters a contract symbol writes for its underlying. One to five, not the six an OCC root can have:
# US stock and ETF tickers are at most five letters, and a six-letter root makes a contract symbol of 21 characters,
# one more than the lake's catalog stores, so every contract of such an underlying would be refused.
_UNDERLYING_PATTERN = r"^[A-Z]{1,5}$"

_Model = ConfigDict(frozen=True, extra="forbid")

_Positive = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]
_Percent = Annotated[Decimal, Field(ge=0, lt=100, max_digits=7, decimal_places=4)]


class SessionTime(BaseModel):
    """A minute of a session, as its distance after the open or before the close."""

    model_config = _Model

    anchor: Literal["open", "close"]
    offset_ms: Annotated[int, Field(strict=True, ge=0, lt=_MS_PER_DAY, multiple_of=MS_PER_MINUTE)]

    def at(self, *, session_open_ms: int, session_close_ms: int) -> int:
        """The instant this names in a session with that open and close."""
        if self.anchor == "open":
            return session_open_ms + self.offset_ms
        return session_close_ms - self.offset_ms


class StopLoss(BaseModel):
    """Close a leg once its premium has moved this far against it: a fall for a bought leg, a rise for a sold one."""

    model_config = _Model

    kind: Literal["percent", "points"]
    # A percent of 100 or more is refused on a bought leg alone, which is the leg's rule to hold:
    # this model cannot see the side.
    value: _Positive


class Momentum(BaseModel):
    """Buy a leg only once its premium has moved this far from where it stood at the entry time."""

    model_config = _Model

    kind: Literal["percent_up", "percent_down", "points_up", "points_down"]
    value: _Positive

    @model_validator(mode="after")
    def _a_fall_in_percent_leaves_something(self) -> Momentum:
        if self.kind == "percent_down" and self.value >= 100:
            raise ValueError("a momentum fall in percent must be under 100: a premium cannot fall to nothing or below")
        return self


class ReentryOnStop(BaseModel):
    """How a leg comes back after a stop, and how many times in one session."""

    model_config = _Model

    # at_once: a fresh contract, bought with no wait. on_momentum: a fresh contract, a new starting price,
    # and the leg's momentum rule asked again.
    kind: Literal["at_once", "on_momentum"]
    count: Annotated[int, Field(strict=True, ge=1, le=MAX_REENTRIES)]


# The three expiry rules are named apart from a bot plan's (``app/schemas/action_plan.py``): two served
# models of one name make FastAPI rename both in the published contract.
class SameDayExpiryRule(BaseModel):
    """The expiry on the trading date itself."""

    model_config = _Model

    kind: Literal["same_day"]


class ThisWeekExpiryRule(BaseModel):
    """The nearest listed expiry, today's included, that falls on the last session of its week."""

    model_config = _Model

    kind: Literal["this_week"]


class MinDaysExpiryRule(BaseModel):
    """The nearest listed expiry at least this many calendar days after the trading date."""

    model_config = _Model

    kind: Literal["min_days"]
    days: Annotated[int, Field(strict=True, ge=0, le=MAX_EXPIRY_DAYS)]


ExpiryRule = Annotated[SameDayExpiryRule | ThisWeekExpiryRule | MinDaysExpiryRule, Discriminator("kind")]


class AtTheMoney(BaseModel):
    """The listed strike nearest the underlying's price; the lower one when two are as near."""

    model_config = _Model

    kind: Literal["atm"]


class StepsFromTheMoney(BaseModel):
    """A listed strike this many listed strikes above (positive) or below (negative) the money."""

    model_config = _Model

    kind: Literal["atm_offset"]
    steps: Annotated[int, Field(strict=True, ge=-MAX_STRIKE_STEPS, le=MAX_STRIKE_STEPS)]

    @model_validator(mode="after")
    def _a_step_goes_somewhere(self) -> StepsFromTheMoney:
        if self.steps == 0:
            raise ValueError("no steps from the money is the money itself: use the at-the-money rule")
        return self


StrikeRule = Annotated[AtTheMoney | StepsFromTheMoney, Discriminator("kind")]

# Whether a leg opens by buying its contract (and is sold to close) or by selling it (and is bought back to close).
LegSide = Literal["buy", "sell"]


class LegSetup(BaseModel):
    """One option leg, bought or sold: how its contract is picked and the rules it carries."""

    model_config = _Model

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    # A leg with no side is bought, and a bought leg writes none (``_omit_the_side_of_a_bought_leg``), so a setup of
    # bought legs is stored and compared byte for byte as it was before a leg could be sold (#3105).
    side: LegSide = "buy"
    right: OptionRight
    contracts: Annotated[int, Field(strict=True, ge=1, le=MAX_CONTRACTS)]
    expiry: ExpiryRule
    strike: StrikeRule
    stop_loss: StopLoss | None = None
    momentum: Momentum | None = None
    reentry_on_stop: ReentryOnStop | None = None

    # No return annotation: the leg's published schema stays this model's own fields (as ``ActionPlan``'s does).
    @model_serializer(mode="wrap")
    def _omit_the_side_of_a_bought_leg(self, handler: SerializerFunctionWrapHandler):
        """Leave a bought leg's side out, and so out of every stored setup and every identity hashed from one."""
        leg = handler(self)
        if leg.get("side") == "buy":
            leg.pop("side", None)
        return leg

    @field_validator("stop_loss")
    @classmethod
    def _a_bought_percent_stop_leaves_something(cls, rule: StopLoss | None, info: ValidationInfo) -> StopLoss | None:
        """A bought leg's premium cannot fall to nothing or below, so its stop in percent is under 100.

        A sold leg's premium can rise without limit, so its stop in percent has no ceiling. This is asked at the field,
        after ``side`` is read, so the refusal stays located at ``stop_loss``.
        """
        bought = info.data.get("side", "buy") == "buy"
        if bought and rule is not None and rule.kind == "percent" and rule.value >= 100:
            raise ValueError("a stop loss in percent must be under 100: a premium cannot fall below nothing")
        return rule

    @model_validator(mode="after")
    def _a_momentum_reentry_has_a_momentum_rule(self) -> LegSetup:
        if self.reentry_on_stop is not None and self.reentry_on_stop.kind == "on_momentum" and self.momentum is None:
            raise ValueError(
                f"leg {self.leg_id!r} re-enters on momentum and has no momentum rule to ask again: "
                "switch momentum on, or re-enter at once"
            )
        return self


class FeePolicy(BaseModel):
    """How a run charges fees. An unknown fee is never a zero: ``not_charged`` says so in the result."""

    model_config = _Model

    kind: Literal["estimated", "per_contract", "not_charged"]
    per_contract_usd: Annotated[Decimal, Field(ge=0, max_digits=8, decimal_places=4)] | None = None

    @model_validator(mode="after")
    def _a_per_contract_fee_is_named(self) -> FeePolicy:
        if (self.kind == "per_contract") != (self.per_contract_usd is not None):
            raise ValueError("a per-contract fee is given for the per-contract policy, and for no other")
        return self


class OptionsBacktestSetup(BaseModel):
    """One options backtest setup, as it is saved, replayed and compared."""

    model_config = _Model

    version: Literal[1] = SETUP_VERSION
    underlying: Annotated[str, Field(pattern=_UNDERLYING_PATTERN)]
    entry: SessionTime
    exit: SessionTime
    # A stop hit after this instant earns no re-entry. First entries are not affected.
    no_reentry_after: SessionTime | None = None
    square_off: Literal["partial", "complete"]
    legs: Annotated[tuple[LegSetup, ...], Field(min_length=1, max_length=MAX_LEGS)]
    # Each fill is moved against the trader by this much of its price, then rounded against the trader
    # to the price step.
    slippage_percent: _Percent = Decimal("0.3")
    price_step_usd: _Positive = Decimal("0.01")
    fees: FeePolicy
    starting_cash_usd: Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]

    def window_fits(self, *, session_open_ms: int, session_close_ms: int) -> bool:
        """Whether a session with that open and close can trade this setup.

        An entry is read on a minute's close, so the entry time is at least one minute after the open.
        The exit's sales fill in the minute after the exit time, so it is at least one minute before the
        close. And the exit is after the entry. A half day can fail this where a full day passes.
        """
        entry_ms = self.entry.at(session_open_ms=session_open_ms, session_close_ms=session_close_ms)
        exit_ms = self.exit.at(session_open_ms=session_open_ms, session_close_ms=session_close_ms)
        return session_open_ms + MS_PER_MINUTE <= entry_ms < exit_ms <= session_close_ms - MS_PER_MINUTE

    @model_validator(mode="after")
    def _every_leg_has_its_own_id(self) -> OptionsBacktestSetup:
        ids = [leg.leg_id for leg in self.legs]
        repeated = sorted({leg_id for leg_id in ids if ids.count(leg_id) > 1})
        if repeated:
            raise ValueError(f"every leg needs its own id; repeated: {', '.join(repeated)}")
        return self

    @model_validator(mode="after")
    def _the_window_can_hold_a_trade(self) -> OptionsBacktestSetup:
        """Refuse the windows that can be seen to fit no session without knowing how long a session is.

        An entry is read on a minute's close and fills in the next minute, so the first entry time is one
        minute after the open and the last exit time one minute before the close; the exit comes after
        the entry, so the last entry time is two minutes before the close and the first exit time two
        minutes after the open. An entry counted from the open and an exit counted from the close whose two
        distances add up to a day or more fit no session, whatever its length, since none lasts a day. Whether
        a time counted from one end falls before a time counted from the other, below that, depends on the
        session's length, which is the calendar's to say: ``window_fits`` asks it of each session when it is
        replayed.
        """
        entry, exit_ = self.entry, self.exit
        if entry.anchor == "open" and entry.offset_ms < MS_PER_MINUTE:
            raise ValueError("the entry time must be at least one minute after the open")
        if entry.anchor == "close" and entry.offset_ms < 2 * MS_PER_MINUTE:
            raise ValueError("the entry time must be at least two minutes before the close, to leave room for the exit")
        if exit_.anchor == "close" and exit_.offset_ms < MS_PER_MINUTE:
            raise ValueError("the exit time must be at least one minute before the close")
        if exit_.anchor == "open" and exit_.offset_ms < 2 * MS_PER_MINUTE:
            raise ValueError("the exit time must be at least two minutes after the open, to come after an entry")
        if entry.anchor == exit_.anchor:
            exit_is_later = (
                exit_.offset_ms > entry.offset_ms if entry.anchor == "open" else exit_.offset_ms < entry.offset_ms
            )
            if not exit_is_later:
                raise ValueError("the exit time must be after the entry time")
        elif entry.anchor == "open" and entry.offset_ms + exit_.offset_ms >= _MS_PER_DAY:
            raise ValueError(
                "the entry time, counted from the open, and the exit time, counted from the close, add up to a day "
                "or more, which no session is long enough to hold: bring them closer to their own ends"
            )
        return self

    def stop_can_close(self, leg: LegSetup) -> bool:
        """Whether some stop loss can close ``leg``, which is what earns it a re-entry.

        A stop closes the leg that carries it. Under a complete square-off it closes every open leg. And a sold leg
        also leaves with a bought leg of its kind when that one is stopped, since protection is never let go first.
        """
        stopped = [each for each in self.legs if each.stop_loss is not None]
        if leg.stop_loss is not None:
            return True
        if self.square_off == "complete" and stopped:
            return True
        return leg.side == "sell" and any(each.side == "buy" and each.right == leg.right for each in stopped)

    @model_validator(mode="after")
    def _a_reentry_rule_can_be_reached(self) -> OptionsBacktestSetup:
        """Refuse a re-entry rule that no stop could ever trigger, since the leg would carry it for nothing.

        A leg earns a re-entry when it is closed by a stop, and it is closed by a stop when its own stop loss is hit.
        Under a complete square-off a leg is also closed when another leg's stop is hit, and that close earns it its
        re-entries too. So a leg with a re-entry rule needs a stop loss of its own, or, under a complete square-off,
        some other leg with one. A sold leg can also be reached through a stop on a bought leg of its kind
        (``stop_can_close``).
        """
        for leg in self.legs:
            if leg.reentry_on_stop is None or self.stop_can_close(leg):
                continue
            # A sold leg's sentence is the longest the setup writes (see ``saved_setups._REASON_PROBLEM_CHARS``).
            own = f"give it, or a bought {leg.right}, a stop loss" if leg.side == "sell" else "give it a stop loss"
            raise ValueError(
                f"leg {leg.leg_id!r} has a re-entry rule and nothing can stop it out: {own}, or "
                "(when the square-off is complete) give another leg one, or take the re-entry rule off"
            )
        return self

    @model_validator(mode="after")
    def _a_sold_contract_is_covered_by_a_bought_one(self) -> OptionsBacktestSetup:
        """Refuse, before any data is read, a setup that sells more contracts of a kind than it buys.

        Nothing is sold that was not bought: a sold call is covered by a bought call, and a sold put by a bought put,
        one contract for one. This is the early refusal only. Whether the contracts picked on a session cover each
        other is judged on those contracts when the session is replayed.
        """
        for right in ("call", "put"):
            sold = sum(leg.contracts for leg in self.legs if leg.right == right and leg.side == "sell")
            bought = sum(leg.contracts for leg in self.legs if leg.right == right and leg.side == "buy")
            if sold > bought:
                raise ValueError(
                    f"the legs sell more {right} contracts than they buy ({sold} sold, {bought} bought): "
                    f"a sold {right} needs a bought {right} to cover it, so buy at least as many {right} contracts "
                    "as are sold"
                )
        return self

    @model_validator(mode="after")
    def _a_setup_that_sells_has_one_expiry_rule(self) -> OptionsBacktestSetup:
        """Refuse a setup with a sold leg whose legs do not all carry the same expiry rule.

        A sold leg and what covers it expire together. Rules that are alike can still come to different dates, which
        the picked contracts settle on a session; this catches the plain mismatch before anything runs.
        """
        if all(leg.side == "buy" for leg in self.legs):
            return self
        first = self.legs[0]
        for leg in self.legs[1:]:
            if leg.expiry != first.expiry:
                raise ValueError(
                    "a setup with a sold leg needs every leg on one expiry rule, so that what is sold and what "
                    f"covers it expire together; leg {leg.leg_id!r} differs from leg {first.leg_id!r}"
                )
        return self
