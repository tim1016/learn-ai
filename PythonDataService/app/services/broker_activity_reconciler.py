"""Pure reconciliation functions for broker-activity rows (ADR 0014).

The publisher (``broker_activity_publisher``) holds state — subscriber
sets, WAL sequence, dedupe cache. This module is the *deterministic
pure* core it calls:

- ``parse_order_ref`` — extract ``(namespace, intent_id)`` from the
  ADR-0008 ``order_ref`` token.
- ``match_identity`` — given an event and the engine's submitted
  intents, return the matched intent (or ``None`` for a foreign exec).
- ``classify_verdict`` — given the inputs, decide
  ``(Verdict, reason_codes, divergence_facts)``.
- ``select_template`` — map ``(verdict, reason_codes)`` to a
  ``(template_key, template_version)`` pair.
- ``author_row_from_event`` — orchestrate the above into a
  ``BrokerActivityRow`` for a broker-side event.
- ``author_pending_row`` — author a ``ENGINE_ONLY_PENDING`` row when the
  publisher detects an intent with no broker ack yet.

These functions never touch the WAL, the SSE channels, the broker
client, or the filesystem. Every output is a deterministic function of
the explicit inputs; the test suite asserts that property directly so
the truthfulness contract cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.broker.ibkr.models import IbkrOrderEvent
from app.schemas.broker_activity import (
    BrokerActivityRow,
    DivergenceFacts,
    EngineOverlay,
    LagBreakdown,
    ReasonCode,
    ReconciliationTimingPolicy,
    SizingProvenance,
    Verdict,
)
from app.services.broker_activity_templates import (
    current_version,
    render_template,
)


# ── Inputs the publisher must assemble ────────────────────────────────


@dataclass(frozen=True)
class EngineIntent:
    """The subset of engine-side state the reconciler consumes per row.

    The publisher assembles this from ``LiveStateEnvelope.submitted_orders``
    (the durable projection) plus whatever richer sources are available
    (sizing resolutions, mutation attempt log, intent WAL). The reconciler
    treats every field as authoritative for the row it produces — it does
    not consult any other engine state.

    All-``None`` fields are acceptable; the reconciler degrades by reading
    only what is present. For a foreign exec (no namespace match), the
    publisher passes ``None`` as the intent rather than an empty
    ``EngineIntent`` — the two have different semantics.
    """

    intent_id: str
    mutation_attempt_id: str | None = None
    requested_qty: float | None = None
    requested_price: float | None = None  # ``None`` for market orders
    intent_created_ms: int | None = None
    dispatched_ms: int | None = None
    acked_ms: int | None = None
    sizing_provenance: SizingProvenance | None = None


@dataclass(frozen=True)
class ReconciliationContext:
    """Per-event publisher state the reconciler reads, never writes.

    ``seq`` and ``ts_ms`` are publisher-assigned. ``previously_seen_exec_ids``
    is the publisher's dedupe set; the reconciler classifies a duplicate
    via ``REASON_CODE.DUPLICATE_EXECUTION`` without consulting the WAL.
    Reconnect-window awareness flows in via
    ``reconnect_recovery_active`` (slice-3 wiring); slice-1 callers may
    leave it ``False``.
    """

    seq: int
    ts_ms: int
    bot_order_namespace: str
    timing_policy: ReconciliationTimingPolicy
    previously_seen_exec_ids: frozenset[str] = field(default_factory=frozenset)
    reconnect_recovery_active: bool = False


# ── Pure helpers ───────────────────────────────────────────────────────


def parse_order_ref(order_ref: str | None) -> tuple[str, str] | None:
    """Parse the ADR-0008 ``order_ref`` token into ``(namespace, intent_id)``.

    Returns ``None`` for any of: missing token, malformed token (no ``:``),
    empty intent_id, empty namespace. Identity matching consumes this
    helper; ADR-0008 §1 mandates equality on the namespace (the part before
    the final ``:``), never ``startswith``.
    """
    if not order_ref:
        return None
    namespace, sep, intent_id = order_ref.rpartition(":")
    if not sep or not namespace or not intent_id:
        return None
    return namespace, intent_id


def match_identity(
    event: IbkrOrderEvent,
    *,
    submitted_orders: Mapping[str, Any],
    bot_order_namespace: str,
) -> str | None:
    """Return the engine ``intent_id`` for this event, or ``None`` if foreign.

    A match requires: (a) the event carries a parseable ``order_ref``,
    (b) the namespace exactly equals this instance's
    ``bot_order_namespace`` (ADR-0008 §1 — equality, never startswith),
    (c) the parsed ``intent_id`` is present in ``submitted_orders``.
    Any other state is foreign (no match).
    """
    parsed = parse_order_ref(event.order_ref)
    if parsed is None:
        return None
    namespace, intent_id = parsed
    if namespace != bot_order_namespace:
        return None
    if intent_id not in submitted_orders:
        return None
    return intent_id


def compute_lag_breakdown(
    *,
    event: IbkrOrderEvent,
    intent: EngineIntent | None,
    observed_ms: int,
) -> LagBreakdown:
    """Build the four-phase lag breakdown from event + intent timestamps.

    Returns an all-``None`` breakdown when the intent is missing; the
    operator-facing chip (``intent_to_exec_ms``) is the publisher's only
    summary number, computed once here so the row is self-describing.
    """
    if intent is None:
        return LagBreakdown(exec_to_observed_ms=_subtract(observed_ms, event.exec_time_ms))

    intent_to_dispatch = _subtract(intent.dispatched_ms, intent.intent_created_ms)
    dispatch_to_ack = _subtract(intent.acked_ms, intent.dispatched_ms)
    ack_to_exec = _subtract(event.exec_time_ms, intent.acked_ms)
    exec_to_observed = _subtract(observed_ms, event.exec_time_ms)
    intent_to_exec = _subtract(event.exec_time_ms, intent.intent_created_ms)

    return LagBreakdown(
        intent_to_dispatch_ms=intent_to_dispatch,
        dispatch_to_ack_ms=dispatch_to_ack,
        ack_to_exec_ms=ack_to_exec,
        exec_to_observed_ms=exec_to_observed,
        intent_to_exec_ms=intent_to_exec,
    )


def _subtract(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return a - b


# ── Verdict classification ─────────────────────────────────────────────


def classify_verdict(
    *,
    event: IbkrOrderEvent,
    intent: EngineIntent | None,
    lag: LagBreakdown,
    ctx: ReconciliationContext,
) -> tuple[Verdict, tuple[ReasonCode, ...], DivergenceFacts | None]:
    """Decide the row's verdict, reasons, and structured divergence facts.

    The classification ladder (highest priority first):

    1. **Duplicate execution** — ``REASON.DUPLICATE_EXECUTION`` →
       ``UNEXPECTED`` (publisher should suppress the SSE emit; see
       publisher logic). Returned here so the row can be written to the
       WAL for audit.
    2. **Cancelled / rejected status events** — terminal lifecycle
       transitions that are not fills. ``EXPECTED`` (cancellation /
       rejection are expected outcomes when they happen).
    3. **Foreign execution (no intent match)** — ``UNEXPECTED`` with
       ``REASON.UNMATCHED_EXECUTION``. The operator must investigate
       (manual TWS click, stale prior-run order, foreign client_id).
    4. **Quantity divergence** — broker filled a different qty than the
       engine requested. ``UNEXPECTED``.
    5. **Price divergence** — broker price differs from the engine's
       requested limit (only meaningful for LMT orders).
       ``UNEXPECTED``.
    6. **Reconnect recovery active** — the publisher is currently
       sweeping a reconnect window; mark the row as a caveat and let
       the reconnect-recovery template explain.
    7. **Excessive lag without explanation** — over the policy's
       ``excessive_lag_ms`` and no known reason → ``UNEXPECTED``.
    8. **Caveat-level lag** — over ``caveat_lag_ms`` →
       ``EXPECTED_WITH_CAVEAT`` with ``REASON.TIMING_CAVEAT``.
    9. **Missing commission** — fill arrived but IBKR has not yet
       reported the fee. ``EXPECTED_WITH_CAVEAT`` with
       ``REASON.MISSING_COMMISSION``.
    10. **Partial fill** — filled less than requested but the order is
        still working (or terminal with partial). ``EXPECTED_WITH_CAVEAT``.
    11. **Default fill path** — ``EXPECTED`` with ``REASON.NORMAL_FILL``.
    """
    # 1 — duplicate
    if event.exec_id and event.exec_id in ctx.previously_seen_exec_ids:
        return Verdict.UNEXPECTED, (ReasonCode.DUPLICATE_EXECUTION,), None

    # 2 — terminal non-fill lifecycle
    if event.event_type == "cancel" or (event.status or "").lower() == "cancelled":
        return Verdict.EXPECTED, (ReasonCode.CANCELLATION,), None
    if event.event_type == "error" or (event.status or "").lower() in {
        "rejected",
        "apicancelled",
    }:
        return Verdict.EXPECTED, (ReasonCode.REJECTION,), None

    # 3 — foreign exec
    if intent is None:
        facts = DivergenceFacts(
            quantity_delta=event.fill_quantity,
        )
        return Verdict.UNEXPECTED, (ReasonCode.UNMATCHED_EXECUTION,), facts

    # Non-fill, non-terminal status events (Submitted, PreSubmitted)
    # are intermediate lifecycle transitions, not rows. The publisher
    # filters these out before calling the reconciler — if one reaches
    # here, the publisher's routing logic is wrong and we must halt
    # rather than synthesise a row.
    if event.event_type != "fill":
        raise UnauthorableEventError(
            f"event {event.order_id} has type {event.event_type!r} and "
            f"status {event.status!r}; only fills, cancellations, and "
            "rejections author broker-activity rows"
        )

    reasons: list[ReasonCode] = []
    qty_delta: float | None = None
    price_delta: float | None = None

    # 4 — quantity divergence (only meaningful when intent declares a qty)
    if intent.requested_qty is not None and event.fill_quantity is not None:
        delta = event.fill_quantity - intent.requested_qty
        if abs(delta) > 1e-9:
            qty_delta = delta
            # Partial-fill vs unexpected-qty distinction: a *smaller*
            # fill that left ``remaining > 0`` is a partial fill the
            # broker is still working on, not a qty divergence. A
            # smaller fill on a terminal order, or a larger fill, is
            # the unexpected case.
            if (
                event.remaining is not None
                and event.remaining > 1e-9
                and delta < 0
            ):
                reasons.append(ReasonCode.PARTIAL_FILL)
            else:
                reasons.append(ReasonCode.QUANTITY_DIVERGENCE)

    # 5 — price divergence (only for LMT orders with a requested price)
    if (
        intent.requested_price is not None
        and event.last_fill_price is not None
        and abs(event.last_fill_price - intent.requested_price) > 1e-9
    ):
        price_delta = event.last_fill_price - intent.requested_price
        reasons.append(ReasonCode.PRICE_DIVERGENCE)

    # 6 — reconnect recovery
    if ctx.reconnect_recovery_active:
        reasons.append(ReasonCode.RECONNECT_RECOVERY)

    # 7, 8 — lag-driven reasons
    lag_total_ms = lag.intent_to_exec_ms
    if lag_total_ms is not None:
        if lag_total_ms > ctx.timing_policy.excessive_lag_ms:
            # Excessive lag without a known explanation is unexpected;
            # WITH an explanation (reconnect window already on the
            # reasons list), the explanation supersedes — operator sees
            # the reconnect caveat, not a raw timing-attention chip.
            if ReasonCode.RECONNECT_RECOVERY not in reasons:
                reasons.append(ReasonCode.TIMING_CAVEAT)
                verdict = _verdict_from_reasons(reasons, override=Verdict.UNEXPECTED)
                facts = DivergenceFacts(
                    price_delta=price_delta,
                    quantity_delta=qty_delta,
                    lag_total_ms=lag_total_ms,
                )
                return verdict, tuple(reasons), facts
        elif lag_total_ms > ctx.timing_policy.caveat_lag_ms:
            reasons.append(ReasonCode.TIMING_CAVEAT)

    # 9 — missing commission
    if event.fee is None:
        reasons.append(ReasonCode.MISSING_COMMISSION)

    # 10, 11 — verdict from collected reasons
    if not reasons:
        reasons.append(ReasonCode.NORMAL_FILL)
    verdict = _verdict_from_reasons(reasons)

    facts = (
        DivergenceFacts(
            price_delta=price_delta,
            quantity_delta=qty_delta,
            lag_total_ms=lag_total_ms,
        )
        if verdict != Verdict.EXPECTED
        else None
    )
    return verdict, tuple(reasons), facts


_DIVERGENCE_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.QUANTITY_DIVERGENCE,
        ReasonCode.PRICE_DIVERGENCE,
        ReasonCode.UNMATCHED_EXECUTION,
        ReasonCode.DUPLICATE_EXECUTION,
    }
)

_CAVEAT_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.PARTIAL_FILL,
        ReasonCode.TIMING_CAVEAT,
        ReasonCode.RECONNECT_RECOVERY,
        ReasonCode.MISSING_COMMISSION,
    }
)


def _verdict_from_reasons(
    reasons: list[ReasonCode],
    *,
    override: Verdict | None = None,
) -> Verdict:
    """Collapse a list of reason codes into the row's verdict.

    Any divergence reason → ``UNEXPECTED``. Any caveat reason (no
    divergence) → ``EXPECTED_WITH_CAVEAT``. Otherwise ``EXPECTED``.
    The optional ``override`` lets the classifier force a verdict when
    the reason list alone would underweight (e.g. excessive lag).
    """
    if override is not None:
        return override
    if any(r in _DIVERGENCE_REASONS for r in reasons):
        return Verdict.UNEXPECTED
    if any(r in _CAVEAT_REASONS for r in reasons):
        return Verdict.EXPECTED_WITH_CAVEAT
    return Verdict.EXPECTED


# ── Template selection ────────────────────────────────────────────────


# Priority-ordered mapping from a reason code to its template. The first
# reason on the row's list that has an entry here wins. ``select_template``
# walks the row's reasons in order, so the publisher controls priority by
# the order in which ``classify_verdict`` emits reasons.
_REASON_TO_TEMPLATE: dict[ReasonCode, str] = {
    ReasonCode.UNMATCHED_EXECUTION: "unmatched_execution",
    ReasonCode.DUPLICATE_EXECUTION: "duplicate_execution",
    ReasonCode.QUANTITY_DIVERGENCE: "quantity_divergence",
    ReasonCode.PRICE_DIVERGENCE: "price_divergence",
    ReasonCode.PARTIAL_FILL: "partial_fill",
    ReasonCode.RECONNECT_RECOVERY: "reconnect_recovery",
    ReasonCode.TIMING_CAVEAT: "timing_caveat",
    ReasonCode.MISSING_COMMISSION: "missing_commission",
    ReasonCode.CANCELLATION: "cancellation",
    ReasonCode.REJECTION: "rejection",
    ReasonCode.PENDING_ACKNOWLEDGEMENT: "pending_acknowledgement",
    ReasonCode.NORMAL_FILL: "normal_fill",
}


def select_template(reasons: tuple[ReasonCode, ...]) -> tuple[str, int]:
    """Map the row's reasons to ``(template_key, current_version)``.

    Walks the reasons in order; the first reason that resolves to a
    registered template wins. Raises ``KeyError`` if no reason maps to a
    template (an unreachable state given the closed ``ReasonCode`` enum
    and the registry's completeness — enforced by the test suite).
    """
    for reason in reasons:
        template_key = _REASON_TO_TEMPLATE.get(reason)
        if template_key is not None:
            return template_key, current_version(template_key)
    raise KeyError(
        f"no template registered for any reason in {reasons!r} — every "
        f"ReasonCode must map; this is a templates-registry bug"
    )


# ── Row authoring (the publisher's entry points) ──────────────────────


def author_row_from_event(
    *,
    event: IbkrOrderEvent,
    intent: EngineIntent | None,
    ctx: ReconciliationContext,
) -> BrokerActivityRow:
    """Author a ``BrokerActivityRow`` from a broker-side event.

    Composes: identity matching (already done by caller, passed as
    ``intent``), lag computation, verdict classification, template
    selection, and template rendering. The row is fully populated and
    immediately writable to the WAL.

    The publisher is responsible for calling ``match_identity`` first
    and passing the result as ``intent``; this function does not look
    up the intent itself because the publisher already has the
    ``submitted_orders`` dict in hand.
    """
    lag = compute_lag_breakdown(
        event=event, intent=intent, observed_ms=ctx.ts_ms
    )
    verdict, reasons, divergence_facts = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=ctx
    )
    template_key, template_version = select_template(reasons)

    facts = _facts_for_template(
        template_key=template_key,
        event=event,
        intent=intent,
        lag=lag,
        ctx=ctx,
        divergence_facts=divergence_facts,
    )
    headline, narrative = render_template(template_key, template_version, facts)

    engine_overlay = _engine_overlay_or_none(intent, lag)

    return BrokerActivityRow(
        seq=ctx.seq,
        ts_ms=ctx.ts_ms,
        exec_id=event.exec_id,
        perm_id=event.perm_id,
        order_ref=event.order_ref,
        symbol=facts["symbol"],
        side=facts["side"],
        # Use the same quantity the template renders (``_facts_quantity``)
        # so the row's stored ``quantity`` matches its own narrative. For
        # fills this is ``event.fill_quantity``; for cancels / rejects
        # (where the broker reports ``fill_quantity=0``) it falls back to
        # the engine's requested qty, matching the "Cancelled buy of N"
        # headline. The truthfulness contract requires the row alone to
        # reproduce its rendered text — quantity is part of that.
        quantity=facts["quantity"],
        price=event.last_fill_price,
        commission=event.fee,
        net_amount=_compute_net_amount(event),
        order_type=facts["order_type"],
        exec_ts_ms=event.exec_time_ms,
        verdict=verdict,
        template_key=template_key,
        template_version=template_version,
        headline=headline,
        narrative=narrative,
        reason_codes=reasons,
        engine_overlay=engine_overlay,
        divergence_facts=divergence_facts,
    )


def author_pending_row(
    *,
    intent: EngineIntent,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str,
    ctx: ReconciliationContext,
) -> BrokerActivityRow:
    """Author a ``ENGINE_ONLY_PENDING`` row for an unacked engine intent.

    The publisher walks ``pending_intents`` and authors one of these per
    intent that has neither a broker ack (in ``submitted_orders``) nor a
    terminal status. The result is surfaced on the SSE channel and the
    WAL so the cockpit's "Working / Pending Orders" panel can display
    it; transitions out happen when a broker event arrives and a
    fill-or-cancel row supersedes the pending row.
    """
    facts = {
        "side": side,
        "quantity": quantity,
        "symbol": symbol,
        "order_type": order_type,
    }
    template_key = "pending_acknowledgement"
    template_version = current_version(template_key)
    headline, narrative = render_template(template_key, template_version, facts)

    return BrokerActivityRow(
        seq=ctx.seq,
        ts_ms=ctx.ts_ms,
        exec_id=None,
        perm_id=None,
        order_ref=f"{ctx.bot_order_namespace}:{intent.intent_id}",
        symbol=symbol,
        side=side,  # type: ignore[arg-type]  # OrderSide validated by Pydantic
        quantity=quantity,
        price=intent.requested_price,
        commission=None,
        net_amount=None,
        order_type=order_type,
        exec_ts_ms=None,
        verdict=Verdict.ENGINE_ONLY_PENDING,
        template_key=template_key,
        template_version=template_version,
        headline=headline,
        narrative=narrative,
        reason_codes=(ReasonCode.PENDING_ACKNOWLEDGEMENT,),
        engine_overlay=_engine_overlay_or_none(intent, LagBreakdown()),
        divergence_facts=None,
    )


# ── Fact assembly for templates ───────────────────────────────────────


def _facts_for_template(
    *,
    template_key: str,
    event: IbkrOrderEvent,
    intent: EngineIntent | None,
    lag: LagBreakdown,
    ctx: ReconciliationContext,
    divergence_facts: DivergenceFacts | None,
) -> dict[str, Any]:
    """Build the facts dict the template renderer consumes.

    Each template's ``required_fact_keys`` is the union of what its
    renderer accesses; this function provides every key any v1
    template may need. Templates the publisher does NOT route to (e.g.
    no quantity_divergence template selected) silently ignore the
    irrelevant fact keys — the template's ``required_fact_keys`` is
    what governs the contract, not this assembly.

    The values must be derivable from ``(event, intent, lag, ctx,
    divergence_facts)`` alone — no speculation, no additional state
    lookup. This is the truthfulness contract enforcement point.
    """
    side = _require_side(event)
    order_type = _require_order_type(event)
    symbol = _require_symbol(event)
    facts: dict[str, Any] = {
        "side": side,
        "symbol": symbol,
        "quantity": _facts_quantity(event, intent),
        "order_type": order_type,
        "exec_id": event.exec_id,
    }
    if event.last_fill_price is not None:
        facts["price"] = event.last_fill_price
    if event.fee is not None:
        facts["commission"] = event.fee
    if intent is not None and intent.requested_qty is not None:
        facts["requested_qty"] = intent.requested_qty
    if intent is not None and intent.requested_price is not None:
        facts["requested_price"] = intent.requested_price
    if divergence_facts is not None and divergence_facts.price_delta is not None:
        facts["price_delta"] = divergence_facts.price_delta
    if lag.intent_to_exec_ms is not None:
        facts["lag_ms"] = lag.intent_to_exec_ms
        facts["caveat_lag_ms"] = ctx.timing_policy.caveat_lag_ms
    return facts


def _engine_overlay_or_none(
    intent: EngineIntent | None, lag: LagBreakdown
) -> EngineOverlay | None:
    if intent is None:
        return None
    return EngineOverlay(
        intent_id=intent.intent_id,
        mutation_attempt_id=intent.mutation_attempt_id,
        requested_qty=intent.requested_qty,
        requested_price=intent.requested_price,
        sizing_provenance=intent.sizing_provenance,
        lag_breakdown=lag,
    )


def _facts_quantity(event: IbkrOrderEvent, intent: EngineIntent | None) -> float:
    """Best-truth quantity for templates.

    For a fill event, ``event.fill_quantity`` is the per-execution share
    count and is the right number to render. For a cancellation or
    rejection (no fill happened), use the engine's requested quantity if
    we have it; otherwise the original order total derived from
    ``cumulative_filled + remaining``. Returns ``0.0`` only when nothing
    is known — that surfaces in the rendered string but is preferable to
    raising on a truthful 'we don't know' state.
    """
    if event.event_type == "fill" and event.fill_quantity is not None:
        return event.fill_quantity
    if intent is not None and intent.requested_qty is not None:
        return intent.requested_qty
    filled = event.cumulative_filled or 0.0
    remaining = event.remaining or 0.0
    return filled + remaining


class UnauthorableEventError(ValueError):
    """Raised when an event lacks fields the publisher must surface
    truthfully (symbol, side, order_type). The publisher halts and logs
    rather than authoring a row with placeholder values."""


def _require_side(event: IbkrOrderEvent) -> str:
    if event.side is None:
        raise UnauthorableEventError(
            f"event {event.exec_id or event.order_id} has no side; "
            "cannot author a truthful broker-activity row"
        )
    return event.side


def _require_symbol(event: IbkrOrderEvent) -> str:
    if not event.symbol:
        raise UnauthorableEventError(
            f"event {event.exec_id or event.order_id} has no symbol; "
            "cannot author a truthful broker-activity row"
        )
    return event.symbol


def _require_order_type(event: IbkrOrderEvent) -> str:
    if not event.order_type:
        raise UnauthorableEventError(
            f"event {event.exec_id or event.order_id} has no order_type; "
            "cannot author a truthful broker-activity row"
        )
    return event.order_type


def _compute_net_amount(event: IbkrOrderEvent) -> float | None:
    """Net cash impact of the fill, signed (negative for buys, positive
    for sells). Mirrors the IBKR Client Portal ``Net Amount`` column.
    ``None`` when price, quantity, or side is absent.

    Assumes equity-style sizing: ``price * quantity ± fee``. Multipliers
    for options/futures are not yet modeled — the publisher's
    symbol-enrichment step will provide the contract multiplier when the
    row is for a derivative.
    """
    if (
        event.last_fill_price is None
        or event.fill_quantity is None
        or event.side is None
    ):
        return None
    gross = event.last_fill_price * event.fill_quantity
    fee = event.fee or 0.0
    if event.side == "SELL":
        return gross - fee
    return -(gross + fee)
