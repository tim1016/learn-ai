"""Shape one program leg from the decision bar, the session, and the operator's allowances (ADR 0059 D5.3).

Inside the regular session a program leg is a market DAY order, exactly as
before this slice. Outside it — in the broker's declared PRE or POST window —
the leg is a marketable DAY limit flagged for extended hours, anchored to the
decision bar's close and widened by the allowance in force — the sealed
envelope's where an arming record exists, the configured one otherwise
(``ProgramLegPolicy.allowances_in_force``). Anything else (closed, no anchor,
no window, no allowance) is a typed refusal the Clerk turns into a rejected
receipt; a program leg is never guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerReadPort
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import TRADEABLE_EXTENDED_PHASES, session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar

if TYPE_CHECKING:
    # Type-only: ``clerk.live_envelope`` pulls in the SQLite package, whose
    # repository imports ``EnvelopeReservation`` straight back out of it. This
    # module is imported first by the composition root, so a runtime import
    # here would close that cycle.
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgramLegPolicy:
    """What the active authority knows about shaping extended-session legs.

    ``envelope`` is the live world's risk envelope, or ``None`` where the
    authority has none (paper, synthetic). It is held rather than copied from
    because the arming ledger is re-read on the envelope sync's cadence: the
    allowances a leg is priced from must be the ones sealed *now*, not the ones
    that happened to be sealed when this authority was composed.
    """

    window: ExtendedHoursWindow | None
    allowances: ExtendedHoursAllowances | None
    envelope: LiveEnvelopeGate | None = None

    @classmethod
    def regular_only(cls) -> ProgramLegPolicy:
        return cls(window=None, allowances=None)

    @classmethod
    def from_read_port(
        cls, read: BrokerReadPort, *, envelope: LiveEnvelopeGate | None = None
    ) -> ProgramLegPolicy:
        """The policy an activated authority's own capability read implies.

        Both activation paths — the real paper Clerk and the ``sim:`` synthetic
        authority — build the policy this same way; stating it once means the
        two cannot answer the question differently.
        """
        return cls(
            window=read.capabilities().extended_hours_window,
            allowances=ExtendedHoursAllowances.from_environment(),
            envelope=envelope,
        )

    def allowances_in_force(self) -> ExtendedHoursAllowances | None:
        """The allowances a leg is priced from: the sealed envelope's, else configured.

        ADR 0059 D3 seals ``xh_entry_bps`` and ``xh_exit_bps`` at arming with
        every other envelope value, so a staged environment edit reaches an
        entry or an exit only at the next re-arm (owner decision 2026-09-10).

        Falling back is never a refusal. An EXIT must leave whatever the
        environment says about seals — an account nobody has armed yet, a
        ledger this tick could not verify — so an unsealed envelope prices from
        the configured allowances and says so, rather than stranding a position
        the operator is trying to close.
        """
        envelope = self.envelope
        if envelope is None:
            return self.allowances
        sealed = envelope.sealed
        if sealed is None:
            logger.info(
                "extended-hours allowances fall back to the configured values; "
                "no arming record seals this account",
                extra={"action": "extended_hours_allowances_unsealed"},
            )
            return self.allowances
        return ExtendedHoursAllowances.from_bps(
            entry_bps=sealed.xh_entry_bps, exit_bps=sealed.xh_exit_bps
        )


@dataclass(frozen=True)
class LegShape:
    """The session-dependent part of a program leg, for the side it was priced for.

    ``side`` is mandatory. It was optional so the one market shape could be a
    module-level singleton, which made a discriminator hide inside an optional
    and forced two separate "is this the side we meant?" reconciliations —
    ``apply``'s and ``_create_reducing_order``'s. There is now exactly one, in
    ``exit_resolution._create_reducing_order`` (ruling R11), because that is the
    only place a shape can meet a side the deciding program did not expect.
    """

    order_type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None
    extended_hours: bool
    side: OrderSide

    def apply(self, *, symbol: str, quantity: float) -> BrokerOrderLeg:
        return BrokerOrderLeg(
            symbol=symbol,
            side=self.side,
            quantity=quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            time_in_force=self.time_in_force,
            extended_hours=self.extended_hours,
        )


def regular_session_shape(side: OrderSide) -> LegShape:
    """The market DAY leg every regular-session program order has always been."""
    return LegShape(
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        limit_price=None,
        extended_hours=False,
        side=side,
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

EXTENDED_ANCHOR_UNPRICEABLE = LegRefusal(
    reason_code="EXTENDED_ANCHOR_UNPRICEABLE",
    explanation=(
        "The decision bar's close and the configured allowance price this leg at or "
        "below zero, which is not a submittable limit."
    ),
    next_step=(
        "Lower ALPACA_LIVE_XH_ENTRY_BPS / ALPACA_LIVE_XH_EXIT_BPS, or keep this "
        "instrument out of extended-hours trading."
    ),
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
        return regular_session_shape(side)
    if decision_bar is None:
        raise ProgramLegRefused(EXTENDED_ANCHOR_UNAVAILABLE)
    phase = session_state_at_ms(now_ms=decision_bar.end_ms, extended_window=session.window).phase
    if phase == "RTH":
        return regular_session_shape(side)
    if phase not in TRADEABLE_EXTENDED_PHASES:
        raise ProgramLegRefused(session_closed_at_decision(phase))
    allowances = policy.allowances_in_force()
    if allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    allowance = allowances.entry_bps if purpose is EffectPurpose.ENTER else allowances.exit_bps
    try:
        price = marketable_limit_price(side=side, close=decision_bar.close, allowance_bps=allowance)
    except ValueError as exc:
        # A non-positive quantised anchor. `BrokerOrderLeg` would reject it too,
        # but as a pydantic ValidationError raised from `apply()` — outside the
        # `except ProgramLegRefused` in `runtime._execute_effect`, so it escaped
        # and killed the shielded effect task instead of writing a rejected
        # receipt. Refusals are this module's whole contract; make it one.
        raise ProgramLegRefused(EXTENDED_ANCHOR_UNPRICEABLE) from exc
    return LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=float(price),
        extended_hours=True,
        side=side,
    )


__all__ = [
    "EXTENDED_ANCHOR_UNAVAILABLE",
    "EXTENDED_ANCHOR_UNPRICEABLE",
    "EXTENDED_HOURS_ALLOWANCE_UNSET",
    "EXTENDED_HOURS_UNSUPPORTED",
    "LegRefusal",
    "LegShape",
    "ProgramLegPolicy",
    "ProgramLegRefused",
    "regular_session_shape",
    "session_closed_at_decision",
    "shape_program_leg",
]
