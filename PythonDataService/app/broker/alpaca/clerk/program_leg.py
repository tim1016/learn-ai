"""Shape one program leg from the decision bar, the session, and the operator's allowances (ADR 0059 D5.3).

Inside the regular session a program leg is a market DAY order, exactly as
before this slice. Outside it — in the broker's declared PRE or POST window,
after-hours ending at the calendar's scheduled close on an early-close day —
the leg is a marketable DAY limit flagged for extended hours, anchored to the
decision bar's close and widened by the policy's allowance. For an
extended-hours run anything else (closed, no anchor, no window, no allowance)
is a typed refusal the Clerk turns into a rejected receipt; a program leg is
never guessed.

A regular-hours run's EXIT takes the same shape when its decision bar closes
at the regular close (#2440, owner decision #2431): the day's last bar closes
*at* 16:00 — 13:00 on an early-close day, both from the canonical calendar —
so its EXIT reaches the broker after the session it was decided in, where a
market DAY order would be queued for the next open. Where that EXIT cannot be
priced (no retained decision bar, no declared window, no allowance, an
unpriceable anchor) it is not refused — an EXIT refused here would never
reduce — but keeps the market leg and says why on ``ProgramLeg.unpriced``,
which the Clerk logs. Whether a leg may still go out when it is actually sent
is decided again, for every EXIT, by the send-time rule in
``sqlite/exit_resolution.py``, which re-prices that market leg off the live
touch after the close, or folds it loudly for the operator.

Which allowance the policy carries is decided by the authority, not here.
Entry keeps the existing profile/arming policy; EXIT reads the owning bot's
immutable terms through ``exit_policy_for_instance`` (ADR 0045). This module
is a pure function of the policy it is handed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances, marketable_limit_price
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerReadPort
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import TRADEABLE_EXTENDED_PHASES, order_session_state_at_ms
from app.services.source_bar_ledger import RetainedSourceBar

if TYPE_CHECKING:
    from app.broker.alpaca.profile.runtime_context import AlpacaRuntimeContext

logger = logging.getLogger(__name__)

# How a policy learns its allowances. Injected as a callable -- the
# ``roster_symbols`` / ``instance_seals`` pattern -- so a test can state the
# resolved document directly instead of building a Clerk volume, and so the one
# production resolver below is named in exactly one place.
type AllowanceResolver = Callable[[], ExtendedHoursAllowances | LegRefusal]

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


def _sealed_allowances(context: AlpacaRuntimeContext, strategy_instance_id: str | None = None) -> ExtendedHoursAllowances | None:
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
        armed = latest_arming(ledger.records() if strategy_instance_id is None else ledger.records_for(strategy_instance_id))
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


def _settings_allowances() -> ExtendedHoursAllowances | LegRefusal:
    """The resolved binding's own settings: a paper revision's allowances.

    For a worker bound from a broker profile, these settings carry the applied
    paper revision's own pair (``paper_xh_allowances``, #2440), bound by
    ``profile/runtime_context.py::resolve_runtime_context``. A live revision's
    pair is its envelope, which the caller reads first; a lane bound without a
    profile still reads its environment here, as every read did before ADR 0060.

    Three ways there are none, and none of them is an exception a caller must
    handle: a synthetic (``sim:``) authority composed in a process with no
    Alpaca credentials at all, a paper revision that sets no extended-hours
    offsets, and a binding that was *attempted and refused*. The last is why
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
        return LegRefusal(
            reason_code=exc.reason,
            explanation=(
                "This account's entry allowance comes from the Alpaca paper settings, "
                f"which could not be loaded. {exc.unbound.message}"
            ),
            next_step=f"Fix the Alpaca connection. {exc.unbound.next_step}",
        )
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
        return LegRefusal(
            reason_code="ALPACA_CONFIGURATION_UNAVAILABLE",
            explanation=(
                "This account's entry allowance comes from the Alpaca paper settings, "
                "which could not be loaded. " + alpaca_configuration_error_detail(exc)
            ),
            next_step="Fix the Alpaca connection and its settings, then restart the clerk.",
        )
    return ExtendedHoursAllowances.from_settings(settings) or EXTENDED_HOURS_ALLOWANCE_UNSET


def _parsed_deploy_knob(
    raw: str | None, *, name: str, low: Decimal, high: Decimal, default: Decimal
) -> Decimal:
    """Parse one deploy-time recovery knob leniently (PR #2230 review).

    The knob is fail-open deployment configuration: unparseable, non-finite,
    or out-of-[low, high] values log at error and answer the declared default
    — never an exception, because the same string failing ``AlpacaSettings``
    construction would disable the whole authority for a recovery-knob typo.
    """
    if raw is None:
        return default
    try:
        value = Decimal(raw.strip())
    except ArithmeticError:
        logger.error(
            "%s is not a number; the declared default %s applies",
            name,
            default,
            extra={"action": "deploy_recovery_knob_unparseable", "setting": name, "raw": raw},
        )
        return default
    if not value.is_finite() or not low <= value <= high:
        logger.error(
            "%s is outside [%s, %s]; the declared default %s applies",
            name,
            low,
            high,
            default,
            extra={"action": "deploy_recovery_knob_out_of_range", "setting": name, "raw": raw},
        )
        return default
    return value


def _exit_band_multiple_from(raw: str | None) -> Decimal:
    """The band multiple one settings read answers, or the declared default."""
    from app.broker.alpaca.marketable_limit import DEFAULT_EXIT_BAND_MULTIPLE

    return _parsed_deploy_knob(
        raw,
        name="ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE",
        low=Decimal(1),
        high=Decimal(10),
        default=DEFAULT_EXIT_BAND_MULTIPLE,
    )


def _exit_spread_cap_from(raw: str | None) -> Decimal:
    """The spread cap one settings read answers, or the declared default."""
    from app.broker.alpaca.marketable_limit import DEFAULT_EXIT_SPREAD_CAP_BPS

    return _parsed_deploy_knob(
        raw,
        name="ALPACA_LIVE_XH_EXIT_SPREAD_CAP_BPS",
        low=Decimal(1),
        high=Decimal(1000),
        default=DEFAULT_EXIT_SPREAD_CAP_BPS,
    )


def legacy_recovery_pricing(allowances: ExtendedHoursAllowances) -> ExtendedHoursAllowances:
    """One-time upgrade reader for removed environment knobs; never used to price a registered bot."""
    import os

    return replace(allowances,
                   exit_band_multiple=_exit_band_multiple_from(os.environ.get("ALPACA_LIVE_XH_EXIT_BAND_MULTIPLE")),
                   exit_spread_cap_bps=_exit_spread_cap_from(os.environ.get("ALPACA_LIVE_XH_EXIT_SPREAD_CAP_BPS")))


def resolve_extended_hours_allowances() -> ExtendedHoursAllowances | LegRefusal:
    """Entry composition keeps the confirmed envelope precedence.

    Registered EXITs replace these defaults with their immutable instance terms.
    The upgrade path reads only that instance's seal or the effective revision.
    """
    from app.broker.alpaca.active_binding import get_active_alpaca_binding

    context = get_active_alpaca_binding()
    sealed = None if context is None else _sealed_allowances(context)
    if sealed is not None:
        return sealed
    if context is not None and context.live_envelope is not None:
        return ExtendedHoursAllowances.from_envelope(context.live_envelope)
    stamped = _settings_allowances()
    return stamped


@dataclass(frozen=True)
class ProgramLegPolicy:
    """What the active authority knows about shaping extended-session legs.

    ``allowances`` is the pair this policy prices from — a plain value, so
    shaping a leg stays a pure function of its arguments. On the live world the
    authority re-resolves it against the sealed envelope per decision
    (``sqlite/runtime.py::SqliteAlpacaClerkFacade.program_leg_policy``); the
    pair resolved here from the applied profile revision -- a paper revision's
    own pair, or a live revision's envelope -- is what a paper, ``sim:`` or
    never-armed authority prices from.
    """

    window: ExtendedHoursWindow | None
    allowances: ExtendedHoursAllowances | None
    # Captured at composition, present exactly when the pair is unavailable.
    allowance_refusal: LegRefusal | None = None

    def __post_init__(self) -> None:
        if self.allowances is None:
            if self.allowance_refusal is None:
                object.__setattr__(self, "allowance_refusal", EXTENDED_HOURS_ALLOWANCE_UNSET)
        elif self.allowance_refusal is not None:
            raise ValueError("A priced policy cannot also carry an allowance refusal")

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
        resolved = allowances()
        return cls(
            window=read.capabilities().extended_hours_window,
            allowances=resolved if isinstance(resolved, ExtendedHoursAllowances) else None,
            allowance_refusal=resolved if isinstance(resolved, LegRefusal) else None,
        )


@dataclass(frozen=True)
class LegShape:
    """The session-dependent part of a program leg, for the side it was priced for.

    ``side`` is mandatory. It was optional so the one market shape could be a
    module-level singleton, which made a discriminator hide inside an optional
    and forced two separate "is this the side we meant?" reconciliations —
    ``apply``'s and the reducing order's. There is now exactly one, in
    ``exit_resolution._resolved_reducing_shape`` (ruling R11), because that is
    the only place a shape can meet a side the deciding program did not expect.
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


@dataclass(frozen=True)
class ProgramLeg:
    """One decision's leg shape, and until when that shape may be sent.

    ``valid_until_ms`` is the end of the session an extended-hours leg was
    priced for — the regular open for PRE, the declared window's close for
    POST — and is durable on the EXIT's acceptance
    (``ExitAcceptedFacts.reducing_valid_until_ms``) exactly as a recovery
    limit's is: ``exit_resolution`` never sends the leg past it. ``None`` for
    the regular-session leg, which may be sent only while that session is open.

    The two travel as one value from the decision to the acceptance: an
    extended-hours shape without its bound would be read as priced for no
    session — expired on arrival — so the pair is refused here instead.

    ``unpriced`` is why a regular-hours EXIT kept its market leg when it may
    have needed the after-hours one: this module is pure, so the Clerk that
    shaped the leg logs it with the strategy and account it belongs to.
    """

    shape: LegShape
    valid_until_ms: int | None = None
    unpriced: LegRefusal | None = None

    def __post_init__(self) -> None:
        if (self.valid_until_ms is not None) != self.shape.extended_hours:
            raise ValueError(
                "an extended-hours program leg carries the end of the session it was "
                "priced for, and a regular-session leg carries none"
            )
        if self.unpriced is not None and self.shape.extended_hours:
            raise ValueError("only a regular-session leg stands in for an unpriced one")


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
    # When the refused leg becomes possible, for a refusal that is only about
    # the clock: an ``int64 ms UTC`` value the UI renders, never prose (#2007).
    available_at_ms: int | None = None


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
        "entry and exit allowances, so no extended-session leg can be priced — including "
        "a regular-hours run's exit on the day's last bar, which reaches the broker after "
        "the close and goes out as an after-hours limit."
    ),
    # Under ADR 0060 the allowances come from the newest sealed arming, and
    # otherwise from the applied profile revision — not from the environment
    # file this used to name. Telling an operator to edit `.env` and restart
    # would now send them somewhere that changes nothing. Start, and a flat
    # Resume, of a regular-hours run refuse with this too (owner decisions 2026-09-25,
    # #2440): the exit allowance is what prices that run's after-close exit.
    next_step=(
        "On the broker configuration page, save a revision of this account's profile with "
        "both extended-hours offsets (entry and exit, in bps) — a paper revision has its "
        "own two fields, a live revision carries them in its envelope — then stage, apply "
        "and restart; on a live account, re-arm so the sealed envelope carries them."
    ),
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
        "Lower ALPACA_LIVE_XH_ENTRY_BPS / ALPACA_LIVE_XH_EXIT_BPS and re-arm — on "
        "the live world the allowance in force is the one sealed at arming, so an "
        "edit alone changes nothing — or keep this instrument out of "
        "extended-hours trading."
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
) -> ProgramLeg:
    """The leg this decision submits (ADR 0059 D5.3), and until when it may be sent.

    The decision bar is read by its **close** (``end_ms``): a bar is filtered
    into the run by the session it opened in
    (``RunDecisionSession.includes``), but the order it drives exists — and is
    therefore priced and placed — at the instant the bucket closed. The two
    instants are deliberately different.

    A regular-hours run never needed its bar to shape an ENTER, and still does
    not. Its EXIT is shaped by the bar's close exactly as an extended run's is
    (#2440): inside the regular session that is the market DAY leg it always
    was, and the day's last bar — which closes *at* the regular close, POST in
    the broker's declared window — gets the extended shape instead of a market
    DAY order Alpaca would queue for the next open. Where that shape cannot be
    priced (no retained decision bar, no declared window, no allowance, an
    unpriceable anchor) the EXIT keeps the regular leg with the refusal on
    ``unpriced`` — never refused here, since a refused EXIT never reduces —
    and the send-time rule in ``exit_resolution`` re-prices it off the live
    touch after the close, or folds it loudly through ``EXIT_NOT_FLAT``.
    """
    session = RunDecisionSession.resolve(use_rth=use_rth, window=policy.window)
    if session is None:
        raise ProgramLegRefused(EXTENDED_HOURS_UNSUPPORTED)
    if session.kind == "extended":
        return _leg_at_decision_close(
            side=side, purpose=purpose, decision_bar=decision_bar, policy=policy
        )
    if purpose is EffectPurpose.ENTER:
        return ProgramLeg(regular_session_shape(side))
    try:
        return _leg_at_decision_close(
            side=side, purpose=purpose, decision_bar=decision_bar, policy=policy
        )
    except ProgramLegRefused as exc:
        return ProgramLeg(regular_session_shape(side), unpriced=exc.refusal)


def _leg_at_decision_close(
    *,
    side: OrderSide,
    purpose: EffectPurpose,
    decision_bar: RetainedSourceBar | None,
    policy: ProgramLegPolicy,
) -> ProgramLeg:
    """The leg for the session the decision bar closed in, or a typed refusal."""
    if decision_bar is None:
        raise ProgramLegRefused(EXTENDED_ANCHOR_UNAVAILABLE)
    state = order_session_state_at_ms(now_ms=decision_bar.end_ms, extended_window=policy.window)
    if state.phase == "RTH":
        return ProgramLeg(regular_session_shape(side))
    if state.phase not in TRADEABLE_EXTENDED_PHASES or state.next_transition_ms is None:
        # A tradeable extended phase always names its end (the regular open
        # after PRE, the declared close after POST); one that did not would
        # bound the leg by no session at all.
        raise ProgramLegRefused(session_closed_at_decision(state.phase))
    if policy.allowances is None:
        assert policy.allowance_refusal is not None
        raise ProgramLegRefused(policy.allowance_refusal)
    allowance = policy.allowances.entry_bps if purpose is EffectPurpose.ENTER else policy.allowances.exit_bps
    if allowance is None:
        raise ProgramLegRefused(EXTENDED_HOURS_ALLOWANCE_UNSET)
    try:
        price = marketable_limit_price(side=side, anchor=decision_bar.close, allowance_bps=allowance)
    except ValueError as exc:
        # A non-positive quantised anchor. `BrokerOrderLeg` would reject it too,
        # but as a pydantic ValidationError raised from `apply()` — outside the
        # `except ProgramLegRefused` in `runtime._execute_effect`, so it escaped
        # and killed the shielded effect task instead of writing a rejected
        # receipt. Refusals are this module's whole contract; make it one.
        raise ProgramLegRefused(EXTENDED_ANCHOR_UNPRICEABLE) from exc
    return ProgramLeg(
        LegShape(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=float(price),
            extended_hours=True,
            side=side,
        ),
        valid_until_ms=state.next_transition_ms,
    )


__all__ = [
    "EXTENDED_ANCHOR_UNAVAILABLE",
    "EXTENDED_ANCHOR_UNPRICEABLE",
    "EXTENDED_HOURS_ALLOWANCE_UNSET",
    "EXTENDED_HOURS_UNSUPPORTED",
    "AllowanceResolver",
    "LegRefusal",
    "LegShape",
    "ProgramLeg",
    "ProgramLegPolicy",
    "ProgramLegRefused",
    "regular_session_shape",
    "resolve_extended_hours_allowances",
    "session_closed_at_decision",
    "shape_program_leg",
]
