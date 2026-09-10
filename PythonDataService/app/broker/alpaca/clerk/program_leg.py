"""Shape one program leg from the decision bar, the session, and the operator's allowances (ADR 0059 D5.3).

Inside the regular session a program leg is a market DAY order, exactly as
before this slice. Outside it — in the broker's declared PRE or POST window —
the leg is a marketable DAY limit flagged for extended hours, anchored to the
decision bar's close. Anything else (closed, no anchor, no window, no
allowance) is a typed refusal the Clerk turns into a rejected receipt; a
program leg is never guessed.

Where the allowance itself comes from is :func:`resolve_extended_hours_allowances`
(ADR 0060; plan §0 D3): the newest *armed* record's sealed envelope first, the
effective revision's envelope next, the resolved binding's settings last. Under
ADR 0059 it was a bare environment read, which meant raising
``ALPACA_LIVE_XH_EXIT_BPS`` and restarting changed **exit** pricing without the
arming ceremony that is the only thing allowed to make a new live limit binding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerReadPort
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import TRADEABLE_EXTENDED_PHASES, session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar

if TYPE_CHECKING:
    from app.broker.alpaca.profile.runtime_context import AlpacaRuntimeContext

logger = logging.getLogger(__name__)

# How a policy learns its allowances. Injected as a callable -- the
# ``roster_symbols`` / ``instance_seals`` pattern -- so a test can state the
# resolved document directly instead of building a Clerk volume, and so the one
# production resolver below is named in exactly one place.
type AllowanceResolver = Callable[[], ExtendedHoursAllowances | None]

# Why the two resolution steps below import inside their function bodies, and
# why the context above is a ``TYPE_CHECKING`` name. ``active_binding``, the
# arming ledger and ``AlpacaRuntimeContext`` all reach
# ``clerk.live_envelope``, which reaches the ``clerk.sqlite`` package, whose
# ``repository`` imports ``clerk.live_envelope`` straight back -- and
# ``active_runtime`` imports *this* module before it imports anything under
# ``clerk.sqlite``. A module-level import here therefore lands on a
# half-initialised ``live_envelope``. The arming ledger separately drags
# ``app.lean_sidecar.trading_calendar`` and the whole market calendar with it,
# which ``profile/runtime_context.py`` already declined to put on a
# configuration-resolution path. Both resolvers run at authority composition,
# never at import, so the deferral costs one dict lookup.


def _sealed_allowances(context: AlpacaRuntimeContext) -> ExtendedHoursAllowances | None:
    """The newest **armed** record's allowances, or ``None`` when there are none to read.

    Reuses the ledger's own reader and ``live_arming.latest_arming`` -- the
    canonical "newest arming, ignoring revocations" (R10) the live authority's
    arming refresh already reads every tick. A disarm row carries no envelope,
    which is exactly why ``latest_arming`` skips it.

    The account comes from the binding's ``account_pin``: the revision's own
    observed account, never a value composed here. Absent (a paper or
    unverified revision), there is no account whose seal to read.

    Nothing raises out of here. A pin that is not a real account id, a ledger
    that will not verify and a store that will not read are each "not this
    source" -- logged at error level, as ``LiveArmingLedger.discover`` logs a
    damaged sibling, because an EXIT must never be blocked by a
    broker-configuration problem (plan §0 D3).
    """
    from app.broker.alpaca.clerk.live_arming import latest_arming
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger

    account_id = context.account_pin
    if account_id is None:
        return None
    try:
        ledger = LiveArmingLedger(context.settings.clerk_dir, live_account_id=account_id)
        armed = latest_arming(ledger.records())
    except (ValueError, OSError):
        # ``LiveArmingInvalid`` is a ``ValueError``, and so are the account-id
        # and path-containment refusals the ledger's constructor raises.
        logger.error(
            "the arming ledger cannot seal an extended-hours allowance; "
            "the effective revision decides instead",
            extra={
                "action": "extended_hours_allowances_seal_unreadable",
                "live_account_id": account_id,
            },
            exc_info=True,
        )
        return None
    return None if armed is None else ExtendedHoursAllowances.from_envelope(armed.envelope)


def _settings_allowances() -> ExtendedHoursAllowances | None:
    """The resolved binding's own settings -- what this read was before ADR 0060.

    Three ways there are none, and none of them is an exception a caller must
    handle: a synthetic (``sim:``) authority composed in a process with no
    Alpaca credentials at all, a paper revision that declares no live envelope,
    and a binding that was *attempted and refused*. The last is why
    ``BrokerUnbound`` is caught rather than propagated -- an EXIT is never
    blocked by a broker-configuration refusal (plan §0 D3). Both are logged so
    an operator can see why an extended-hours leg was refused.
    """
    from app.broker.alpaca.active_binding import BrokerUnbound, resolved_alpaca_settings

    try:
        settings = resolved_alpaca_settings()
    except BrokerUnbound as exc:
        logger.info(
            "Extended-hours allowances are unavailable: this worker has no broker binding",
            extra={"action": "extended_hours_allowances_unbound", "reason": exc.reason},
        )
        return None
    except ValidationError as exc:
        # ``str(exc)`` would echo a plaintext credential fragment: Pydantic
        # renders ``input_value`` for a model-level error, and for a
        # ``BaseSettings`` that input is the raw settings-source dict — before
        # ``SecretStr`` wrapping. ``alpaca_configuration_error_detail`` keeps
        # only Pydantic's ``msg`` text, which never echoes the input.
        from app.broker.alpaca.config import alpaca_configuration_error_detail

        logger.info(
            "Extended-hours allowances are unavailable: Alpaca settings did not load",
            extra={
                "action": "extended_hours_allowances_unavailable",
                "detail": alpaca_configuration_error_detail(exc),
            },
        )
        return None
    return ExtendedHoursAllowances.from_settings(settings)


def resolve_extended_hours_allowances() -> ExtendedHoursAllowances | None:
    """The allowances an extended-session leg prices from (ADR 0060; plan §0 D3).

    One order, used for **both** the ENTER and the EXIT allowance, because the
    two numbers live in one document and the question "which document?" has one
    answer:

    1. the newest **armed** record's sealed envelope -- the six numbers the
       operator confirmed at the arming ceremony, which is the only act allowed
       to make a new live limit binding (D3);
    2. the effective revision's envelope, when no armed record is readable;
    3. the resolved binding's settings, which is what a paper or ``sim:``
       authority has and all any authority had before ADR 0060.

    **Nothing raises.** A refused binding, an unpinned account, an unreadable
    ledger and an absent envelope are each "not this source", so no exit
    pricing can fail because of a configuration problem.

    Genuinely no source at all still answers ``None``, and
    :func:`shape_program_leg` refuses that as ``EXTENDED_HOURS_ALLOWANCE_UNSET``
    -- for an EXIT as much as an ENTER. "Never blocked *for lack of a seal*"
    means falling back to the effective revision, not inventing a number: a
    number nobody chose must never bound real money (ADR 0059 D4).
    """
    from app.broker.alpaca.active_binding import get_active_alpaca_binding

    context = get_active_alpaca_binding()
    if context is not None:
        sealed = _sealed_allowances(context)
        if sealed is not None:
            return sealed
        if context.live_envelope is not None:
            return ExtendedHoursAllowances.from_envelope(context.live_envelope)
    return _settings_allowances()


@dataclass(frozen=True)
class ProgramLegPolicy:
    """What the active authority knows about shaping extended-session legs."""

    window: ExtendedHoursWindow | None
    allowances: ExtendedHoursAllowances | None

    @classmethod
    def regular_only(cls) -> ProgramLegPolicy:
        return cls(window=None, allowances=None)

    @classmethod
    def from_read_port(
        cls,
        read: BrokerReadPort,
        *,
        allowances: AllowanceResolver = resolve_extended_hours_allowances,
    ) -> ProgramLegPolicy:
        """The policy an activated authority's own capability read implies.

        Both activation paths — the real paper Clerk and the ``sim:`` synthetic
        authority — build the policy this same way; stating it once means the
        two cannot answer the question differently.

        Resolved once here, at composition, and not per decision: the sealed
        envelope only changes when an arming ceremony runs, and the effective
        revision only changes at a controlled restart (D5), so a per-tick read
        of the Clerk volume would buy nothing and put file I/O on the decision
        path.
        """
        return cls(
            window=read.capabilities().extended_hours_window,
            allowances=allowances(),
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
        "No sealed arming and no applied broker configuration carry the extended-session "
        "entry and exit allowances, so no extended-session program leg can be priced."
    ),
    # Under ADR 0060 the allowances come from the newest sealed arming, and
    # otherwise from the applied profile revision — not from the environment
    # file this used to name. Telling an operator to edit `.env` and restart
    # would now send them somewhere that changes nothing.
    next_step="Apply a broker configuration that carries both allowances, then re-arm.",
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
    if policy.allowances is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    allowance = policy.allowances.entry_bps if purpose is EffectPurpose.ENTER else policy.allowances.exit_bps
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
    "AllowanceResolver",
    "LegRefusal",
    "LegShape",
    "ProgramLegPolicy",
    "ProgramLegRefused",
    "regular_session_shape",
    "resolve_extended_hours_allowances",
    "session_closed_at_decision",
    "shape_program_leg",
]
