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
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import TRADEABLE_EXTENDED_PHASES, session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar


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
        if self.side is not None and side is not self.side:
            raise ValueError("this leg shape was priced for the other side")
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


@dataclass(frozen=True)
class LegRefusal:
    """One named reason a decision cannot become a program leg.

    Named values rather than a string literal at each raise site: Start and
    Resume admission refuse two of these conditions *before* a run streams,
    and this module refuses them again per decision. Both refusals used to be
    written out twice, with hand-varied wording, so an operator could read one
    explanation at the gate and a different one on the receipt.
    """

    reason_code: str
    explanation: str
    next_step: str


EXTENDED_HOURS_UNSUPPORTED = LegRefusal(
    reason_code="EXTENDED_HOURS_UNSUPPORTED",
    explanation=(
        "The active broker authority declares no extended session, so a run outside "
        "regular hours has no decision clock."
    ),
    next_step=(
        "Deploy with regular hours, or activate an authority whose capabilities "
        "declare an extended window."
    ),
)

EXTENDED_HOURS_ALLOWANCE_UNSET = LegRefusal(
    reason_code="EXTENDED_HOURS_ALLOWANCE_UNSET",
    explanation=(
        "ALPACA_LIVE_XH_ENTRY_BPS and ALPACA_LIVE_XH_EXIT_BPS are not both set, so no "
        "extended-session program leg can be priced."
    ),
    next_step="Set both allowances in the environment file and restart the data plane.",
)

EXTENDED_ANCHOR_UNAVAILABLE = LegRefusal(
    reason_code="EXTENDED_ANCHOR_UNAVAILABLE",
    explanation="No exact retained decision bar exists to anchor an extended-session leg.",
    next_step="Retain the decision bar (replay or ingest it), then let the program decide again.",
)


def session_closed_at_decision(phase: str) -> LegRefusal:
    """The decision instant is in no session that accepts a program leg."""
    return LegRefusal(
        reason_code="SESSION_CLOSED_AT_DECISION",
        explanation=f"The decision instant is {phase}; no session accepts a program leg now.",
        next_step="No action: the program decides again on the next bar inside a session.",
    )


class ProgramLegRefused(Exception):
    """This decision cannot become a program leg; the Clerk records why."""

    def __init__(self, refusal: LegRefusal) -> None:
        super().__init__(f"{refusal.reason_code}: {refusal.explanation}")
        self.refusal = refusal
        self.reason_code = refusal.reason_code
        self.explanation = refusal.explanation
        self.next_step = refusal.next_step


def shape_program_leg(
    *,
    side: OrderSide,
    purpose: EffectPurpose,
    use_rth: bool,
    decision_bar: RetainedSourceBar | None,
    policy: ProgramLegPolicy,
) -> LegShape:
    """The shape of the leg this decision submits (ADR 0059 D5.3).

    The decision bar is read by its **close** (``end_ms``): a bar is filtered
    into the run by the session it opened in
    (``RunDecisionSession.includes``), but the order it drives exists — and is
    therefore priced and placed — at the instant the bucket closed. The two
    instants are deliberately different.
    """
    session = RunDecisionSession.resolve(use_rth=use_rth, window=policy.window)
    if session is None:
        raise ProgramLegRefused(EXTENDED_HOURS_UNSUPPORTED)
    if session.kind == "rth":
        return REGULAR_SESSION_SHAPE
    if decision_bar is None:
        raise ProgramLegRefused(EXTENDED_ANCHOR_UNAVAILABLE)
    phase = session_state_at_ms(now_ms=decision_bar.end_ms, extended_window=session.window).phase
    if phase == "RTH":
        return REGULAR_SESSION_SHAPE
    if phase not in TRADEABLE_EXTENDED_PHASES:
        raise ProgramLegRefused(session_closed_at_decision(phase))
    if policy.allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
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
    "EXTENDED_ANCHOR_UNAVAILABLE",
    "EXTENDED_HOURS_ALLOWANCE_UNSET",
    "EXTENDED_HOURS_UNSUPPORTED",
    "REGULAR_SESSION_SHAPE",
    "LegRefusal",
    "LegShape",
    "ProgramLegPolicy",
    "ProgramLegRefused",
    "session_closed_at_decision",
    "shape_program_leg",
]
