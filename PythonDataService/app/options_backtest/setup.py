"""What an owner sets up for one options backtest (#3030).

A setup is a trading window, one to eight bought option legs, and the cost
assumptions the run is priced under. Each leg names how its contract is picked
and may carry three rules of its own: a stop loss, a momentum entry and a
re-entry after a stop.

The setup says nothing about dates: the same setup is replayed over whatever
sessions a run asks for. Its times are distances from a session's open or its
close, so a half day keeps them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, model_validator

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
# An OCC root: the letters a contract symbol writes for its underlying.
_UNDERLYING_PATTERN = r"^[A-Z]{1,6}$"

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
    """Sell a leg once its premium has fallen this far below what was paid for it."""

    model_config = _Model

    kind: Literal["percent", "points"]
    value: _Positive

    @model_validator(mode="after")
    def _a_percent_leaves_something(self) -> StopLoss:
        if self.kind == "percent" and self.value >= 100:
            raise ValueError("a stop loss in percent must be under 100: a premium cannot fall below nothing")
        return self


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


class LegSetup(BaseModel):
    """One bought option leg: how its contract is picked and the rules it carries."""

    model_config = _Model

    leg_id: Annotated[str, Field(pattern=_LEG_ID_PATTERN)]
    right: OptionRight
    contracts: Annotated[int, Field(strict=True, ge=1, le=MAX_CONTRACTS)]
    expiry: ExpiryRule
    strike: StrikeRule
    stop_loss: StopLoss | None = None
    momentum: Momentum | None = None
    reentry_on_stop: ReentryOnStop | None = None

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
        minutes after the open. Whether a time counted from one end falls before a time counted from the
        other depends on the session's length, which is the calendar's to say: ``window_fits`` asks it of
        each session when it is replayed.
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
        return self
