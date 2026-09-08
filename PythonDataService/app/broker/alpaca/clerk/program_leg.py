"""Shape one program leg from the decision bar, the session, and the operator's allowances (ADR 0059 D5.3).

Inside the regular session a program leg is a market DAY order, exactly as
before this slice. Outside it — in the broker's declared PRE or POST window —
the leg is a marketable DAY limit flagged for extended hours, anchored to the
decision bar's close. Anything else (closed, no anchor, no window, no
allowance) is a typed refusal the Clerk turns into a rejected receipt; a
program leg is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.marketdata.feed import DecisionSession
from app.services.session_authority import session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar

_EXTENDED_PHASES: Final = frozenset({"PRE", "POST"})


@dataclass(frozen=True)
class ProgramLegPolicy:
    """What the active authority knows about shaping extended-session legs."""

    window: ExtendedHoursWindow | None
    allowances: ExtendedHoursAllowances | None

    @classmethod
    def regular_only(cls) -> ProgramLegPolicy:
        return cls(window=None, allowances=None)


@dataclass(frozen=True)
class LegShape:
    """The session-dependent part of a program leg; ``side`` names the side a limit was priced for."""

    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None
    extended_hours: bool
    side: OrderSide | None

    def apply(self, *, symbol: str, side: OrderSide, quantity: float) -> BrokerOrderLeg:
        return BrokerOrderLeg(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            extended_hours=self.extended_hours,
        )


REGULAR_SESSION_SHAPE: Final = LegShape(
    order_type=OrderType.MARKET,
    time_in_force=TimeInForce.DAY,
    limit_price=None,
    extended_hours=False,
    side=None,
)


class ProgramLegRefused(Exception):
    """This decision cannot become a program leg; the Clerk records why."""

    def __init__(self, *, reason_code: str, explanation: str, next_step: str) -> None:
        super().__init__(f"{reason_code}: {explanation}")
        self.reason_code = reason_code
        self.explanation = explanation
        self.next_step = next_step


def shape_program_leg(
    *,
    side: OrderSide,
    purpose: EffectPurpose,
    decision_session: DecisionSession,
    decision_bar: RetainedSourceBar | None,
    policy: ProgramLegPolicy,
) -> LegShape:
    """The shape of the leg this decision submits (ADR 0059 D5.3)."""
    if decision_session == "rth":
        return REGULAR_SESSION_SHAPE
    if policy.window is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_HOURS_UNSUPPORTED",
            explanation="The active broker authority declares no extended session.",
            next_step=(
                "Deploy with regular hours, or activate an authority whose "
                "capabilities declare an extended window."
            ),
        )
    if decision_bar is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_ANCHOR_UNAVAILABLE",
            explanation="No exact retained decision bar exists to anchor an extended-session leg.",
            next_step="Retain the decision bar (replay or ingest it), then let the program decide again.",
        )
    phase = session_state_at_ms(now_ms=decision_bar.end_ms, extended_window=policy.window).phase
    if phase == "RTH":
        return REGULAR_SESSION_SHAPE
    if phase not in _EXTENDED_PHASES:
        raise ProgramLegRefused(
            reason_code="SESSION_CLOSED_AT_DECISION",
            explanation=f"The decision instant is {phase}; no session accepts a program leg now.",
            next_step="No action: the program decides again on the next bar inside a session.",
        )
    if policy.allowances is None:
        raise ProgramLegRefused(
            reason_code="EXTENDED_HOURS_ALLOWANCE_UNSET",
            explanation=(
                "ALPACA_LIVE_XH_ENTRY_BPS and ALPACA_LIVE_XH_EXIT_BPS are not both "
                "set; no extended-session leg can be priced."
            ),
            next_step="Set both allowances in the environment file and restart the data plane.",
        )
    allowance = policy.allowances.entry_bps if purpose is EffectPurpose.ENTER else policy.allowances.exit_bps
    price = marketable_limit_price(side=side, close=decision_bar.close, allowance_bps=allowance)
    return LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(price),
        extended_hours=True,
        side=side,
    )


__all__ = [
    "REGULAR_SESSION_SHAPE",
    "LegShape",
    "ProgramLegPolicy",
    "ProgramLegRefused",
    "shape_program_leg",
]
