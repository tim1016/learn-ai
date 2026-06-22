"""Tests for ``app.services.broker_activity_reconciler`` — ADR 0014 pure core.

This module IS the truthfulness contract. Every test asserts that a
deterministic input produces a specific verdict + template + narrative;
together they make it impossible to drift the operator-facing surface
without a visible test failure.

Coverage:

- ``parse_order_ref`` / ``match_identity`` — ADR-0008 §1 namespace
  equality (never startswith) round-trip.
- ``classify_verdict`` — the 11-rung ladder, one test per branch.
- ``select_template`` — every ``ReasonCode`` maps to a registered
  template (completeness invariant).
- ``author_row_from_event`` — exact output for canonical scenarios
  (this is what the publisher persists and emits).
- ``author_pending_row`` — the engine-only-pending shape.

Also covered: template completeness (every ``ReasonCode`` →
``select_template`` reachable template), and the
``UnauthorableEventError`` halt path.
"""

from __future__ import annotations

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.schemas.broker_activity import (
    ReasonCode,
    ReconciliationTimingPolicy,
    SizingProvenance,
    Verdict,
)
from app.services.broker_activity_reconciler import (
    EngineIntent,
    ReconciliationContext,
    UnauthorableEventError,
    author_pending_row,
    author_row_from_event,
    classify_verdict,
    compute_lag_breakdown,
    match_identity,
    parse_order_ref,
    select_template,
)
from app.services.broker_activity_templates import (
    registered_keys,
    render_template,
)


NS = "learn-ai/sid-a/v1"
INTENT_ID = "intent-x"
ORDER_REF = f"{NS}:{INTENT_ID}"


def _ctx(
    *,
    seq: int = 1,
    ts_ms: int = 1_700_000_000_000,
    timing_policy: ReconciliationTimingPolicy | None = None,
    seen: frozenset[str] = frozenset(),
    reconnect: bool = False,
) -> ReconciliationContext:
    return ReconciliationContext(
        seq=seq,
        ts_ms=ts_ms,
        bot_order_namespace=NS,
        timing_policy=timing_policy or ReconciliationTimingPolicy(),
        previously_seen_exec_ids=seen,
        reconnect_recovery_active=reconnect,
    )


def _intent(**over) -> EngineIntent:
    base = {
        "intent_id": INTENT_ID,
        "requested_qty": 100.0,
        "intent_created_ms": 1_700_000_000_000 - 500,
        "dispatched_ms": 1_700_000_000_000 - 400,
        "acked_ms": 1_700_000_000_000 - 300,
    }
    base.update(over)
    return EngineIntent(**base)


def _fill_event(**over) -> IbkrOrderEvent:
    base = {
        "account_id": "DU1234567",
        "order_id": 42,
        "perm_id": 999,
        "event_type": "fill",
        "status": "Filled",
        "order_ref": ORDER_REF,
        "symbol": "SPY",
        "side": "BUY",
        "order_type": "MKT",
        "exec_id": "exec-1",
        "fill_quantity": 100.0,
        "avg_fill_price": 450.0,
        "cumulative_filled": 100.0,
        "remaining": 0.0,
        "last_fill_price": 450.0,
        "exec_time_ms": 1_700_000_000_000 - 50,
        "fee": 1.0,
        "ts_ms": 1_700_000_000_000,
    }
    base.update(over)
    return IbkrOrderEvent(**base)


# ── parse_order_ref / match_identity (ADR-0008 §1 invariants) ─────────


def test_parse_order_ref_round_trips_namespace_and_intent_id() -> None:
    assert parse_order_ref(ORDER_REF) == (NS, INTENT_ID)


def test_parse_order_ref_splits_on_final_colon_only() -> None:
    """Namespaces don't contain ``:`` today; intent_ids don't either; but
    if a future intent_id format introduces a colon, ``rpartition`` keeps
    the namespace intact. Regression-guard the invariant."""
    assert parse_order_ref("learn-ai/sid-a/v1:weird:intent") == (
        "learn-ai/sid-a/v1:weird",
        "intent",
    )


def test_parse_order_ref_returns_none_for_malformed_or_empty() -> None:
    assert parse_order_ref(None) is None
    assert parse_order_ref("") is None
    assert parse_order_ref("no-colon-here") is None
    assert parse_order_ref(":intent-only") is None
    assert parse_order_ref("namespace-only:") is None


def test_match_identity_returns_intent_id_on_exact_namespace_match() -> None:
    event = _fill_event()
    intent_id = match_identity(
        event,
        submitted_orders={INTENT_ID: {"perm_id": 999}},
        bot_order_namespace=NS,
    )
    assert intent_id == INTENT_ID


def test_match_identity_rejects_startswith_match() -> None:
    """ADR-0008 §1 — namespace match is EXACT equality, never
    ``startswith``. A foreign namespace that happens to start with ours
    must be classified as foreign so we never adopt cross-version
    orders silently."""
    event = _fill_event(order_ref="learn-ai/sid-a/v10:intent-x")
    assert (
        match_identity(
            event,
            submitted_orders={INTENT_ID: {"perm_id": 999}},
            bot_order_namespace=NS,
        )
        is None
    )


def test_match_identity_rejects_missing_intent_in_submitted_orders() -> None:
    event = _fill_event()
    assert (
        match_identity(
            event,
            submitted_orders={"other-intent": {}},
            bot_order_namespace=NS,
        )
        is None
    )


# ── classify_verdict ladder ────────────────────────────────────────────


def test_classify_normal_fill_returns_expected() -> None:
    event = _fill_event()
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, divergence = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED
    assert reasons == (ReasonCode.NORMAL_FILL,)
    assert divergence is None


def test_classify_unmatched_execution_returns_unexpected() -> None:
    """A fill arriving for an order our engine never placed: identity is
    foreign by namespace. UNEXPECTED with UNMATCHED_EXECUTION reason and
    a divergence_facts.quantity_delta = the entire foreign quantity."""
    event = _fill_event(order_ref=None)
    lag = compute_lag_breakdown(event=event, intent=None, observed_ms=_ctx().ts_ms)
    verdict, reasons, divergence = classify_verdict(
        event=event, intent=None, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.UNEXPECTED
    assert reasons == (ReasonCode.UNMATCHED_EXECUTION,)
    assert divergence is not None
    assert divergence.quantity_delta == 100.0


def test_classify_duplicate_execution_returns_unexpected() -> None:
    event = _fill_event(exec_id="dup-1")
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    ctx = _ctx(seen=frozenset({"dup-1"}))
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=ctx
    )
    assert verdict == Verdict.UNEXPECTED
    assert reasons == (ReasonCode.DUPLICATE_EXECUTION,)


def test_classify_quantity_divergence_on_terminal_overfill_returns_unexpected() -> None:
    """The engine asked for 100; broker filled 150 and the order is
    terminal (remaining=0). Not a partial — an unexpected divergence."""
    event = _fill_event(fill_quantity=150.0, cumulative_filled=150.0, remaining=0.0)
    intent = _intent(requested_qty=100.0)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, divergence = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.UNEXPECTED
    assert ReasonCode.QUANTITY_DIVERGENCE in reasons
    assert divergence is not None
    assert divergence.quantity_delta == 50.0


def test_classify_partial_fill_with_remaining_returns_caveat() -> None:
    """Smaller fill than requested + order still working (remaining > 0)
    → PARTIAL_FILL, not QUANTITY_DIVERGENCE. EXPECTED_WITH_CAVEAT."""
    event = _fill_event(fill_quantity=40.0, cumulative_filled=40.0, remaining=60.0)
    intent = _intent(requested_qty=100.0)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert ReasonCode.PARTIAL_FILL in reasons
    assert ReasonCode.QUANTITY_DIVERGENCE not in reasons


def test_classify_price_divergence_returns_unexpected() -> None:
    event = _fill_event(last_fill_price=450.10)
    intent = _intent(requested_price=450.00)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, divergence = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.UNEXPECTED
    assert ReasonCode.PRICE_DIVERGENCE in reasons
    assert divergence is not None
    assert divergence.price_delta == pytest.approx(0.10, abs=1e-9)


def test_classify_excessive_lag_without_explanation_returns_unexpected() -> None:
    """intent_to_exec > excessive_lag_ms and no reconnect window =>
    UNEXPECTED with TIMING_CAVEAT reason."""
    intent = _intent(intent_created_ms=1_700_000_000_000 - 30_000)
    event = _fill_event(exec_time_ms=1_700_000_000_000 - 50)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, divergence = classify_verdict(
        event=event,
        intent=intent,
        lag=lag,
        ctx=_ctx(timing_policy=ReconciliationTimingPolicy()),
    )
    assert verdict == Verdict.UNEXPECTED
    assert ReasonCode.TIMING_CAVEAT in reasons
    assert divergence is not None
    assert divergence.lag_total_ms is not None
    assert divergence.lag_total_ms > 10_000


def test_classify_excessive_lag_with_reconnect_active_returns_caveat() -> None:
    """Same lag, but reconnect_recovery_active=True: the reconnect
    template explains it; verdict downgrades to EXPECTED_WITH_CAVEAT."""
    intent = _intent(intent_created_ms=1_700_000_000_000 - 30_000)
    event = _fill_event(exec_time_ms=1_700_000_000_000 - 50)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx(reconnect=True)
    )
    assert verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert ReasonCode.RECONNECT_RECOVERY in reasons
    assert ReasonCode.TIMING_CAVEAT not in reasons


def test_classify_caveat_lag_returns_expected_with_caveat() -> None:
    """Lag between caveat and excessive thresholds → caveat."""
    intent = _intent(intent_created_ms=1_700_000_000_000 - 3_000)  # 3s
    event = _fill_event(exec_time_ms=1_700_000_000_000 - 50)
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert ReasonCode.TIMING_CAVEAT in reasons


def test_classify_missing_commission_returns_caveat() -> None:
    event = _fill_event(fee=None)
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert ReasonCode.MISSING_COMMISSION in reasons


def test_classify_cancellation_event_returns_expected_cancellation() -> None:
    event = _fill_event(event_type="cancel", status="Cancelled", fill_quantity=0.0)
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED
    assert reasons == (ReasonCode.CANCELLATION,)


def test_classify_rejection_event_returns_expected_rejection() -> None:
    """IBKR signals rejections via ``event_type=error`` (or via
    ``status=ApiCancelled``). The OrderStatus literal does not include
    'Rejected' — the classifier keys on event_type and the ApiCancelled
    sentinel."""
    event = _fill_event(
        event_type="error",
        status=None,
        fill_quantity=0.0,
        error_code=201,
        error_message="Order rejected - reason:...",
    )
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    verdict, reasons, _ = classify_verdict(
        event=event, intent=intent, lag=lag, ctx=_ctx()
    )
    assert verdict == Verdict.EXPECTED
    assert reasons == (ReasonCode.REJECTION,)


def test_classify_intermediate_status_event_raises_unauthorable() -> None:
    """The publisher must filter Submitted/PreSubmitted before calling
    the reconciler. If one slips through, the reconciler raises rather
    than synthesising a row."""
    event = _fill_event(event_type="status", status="Submitted", fill_quantity=None)
    intent = _intent()
    lag = compute_lag_breakdown(event=event, intent=intent, observed_ms=_ctx().ts_ms)
    with pytest.raises(UnauthorableEventError):
        classify_verdict(event=event, intent=intent, lag=lag, ctx=_ctx())


# ── select_template completeness ────────────────────────────────────────


def test_every_reason_code_maps_to_a_registered_template() -> None:
    """Closed-vocabulary invariant: every ``ReasonCode`` must resolve via
    ``select_template`` to a key that is in the templates registry.
    Adding a reason without a template is a CI-caught contract bug."""
    keys = registered_keys()
    for reason in ReasonCode:
        template_key, _version = select_template((reason,))
        assert template_key in keys, (
            f"ReasonCode {reason.value!r} maps to {template_key!r} which "
            f"is not in the templates registry"
        )


def test_select_template_prefers_first_reason_in_list() -> None:
    """Priority is the order the classifier emits reasons. A row with
    [QUANTITY_DIVERGENCE, TIMING_CAVEAT] selects the quantity template,
    not the timing one."""
    template_key, _ = select_template(
        (ReasonCode.QUANTITY_DIVERGENCE, ReasonCode.TIMING_CAVEAT)
    )
    assert template_key == "quantity_divergence"


# ── author_row_from_event end-to-end (the truthfulness boundary) ───────


def test_author_normal_fill_renders_expected_headline_and_narrative() -> None:
    event = _fill_event()
    intent = _intent()
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())

    assert row.verdict == Verdict.EXPECTED
    assert row.template_key == "normal_fill"
    assert row.template_version == 1
    assert row.headline == "Filled 100 SPY at $450.00"
    assert row.narrative == "MKT order filled in full at $450.00; $1.00 commission."
    assert row.exec_id == "exec-1"
    assert row.perm_id == 999
    assert row.order_ref == ORDER_REF
    assert row.symbol == "SPY"
    assert row.side == "BUY"
    assert row.quantity == 100.0
    assert row.price == 450.0
    assert row.commission == 1.0
    assert row.engine_overlay is not None
    assert row.engine_overlay.intent_id == INTENT_ID
    assert row.engine_overlay.lag_breakdown.intent_to_exec_ms == 450


def test_author_unmatched_execution_renders_unmatched_template() -> None:
    event = _fill_event(order_ref=None, exec_id="foreign-1")
    row = author_row_from_event(event=event, intent=None, ctx=_ctx())

    assert row.verdict == Verdict.UNEXPECTED
    assert row.template_key == "unmatched_execution"
    assert "Unmatched execution" in row.headline
    assert row.engine_overlay is None


def test_author_price_divergence_renders_price_template() -> None:
    event = _fill_event(last_fill_price=450.10)
    intent = _intent(requested_price=450.00)
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())

    assert row.verdict == Verdict.UNEXPECTED
    assert row.template_key == "price_divergence"
    assert "$0.10 above requested $450.00" in row.headline
    assert row.divergence_facts is not None
    assert row.divergence_facts.price_delta == pytest.approx(0.10)


def test_author_cancellation_yields_expected_cancellation_row() -> None:
    event = _fill_event(
        event_type="cancel",
        status="Cancelled",
        fill_quantity=None,
        last_fill_price=None,
        fee=None,
        cumulative_filled=0.0,
        remaining=100.0,
    )
    intent = _intent()
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())

    assert row.verdict == Verdict.EXPECTED
    assert row.template_key == "cancellation"
    # quantity in the narrative comes from intent.requested_qty (100),
    # not from the broker-reported fill_quantity (0).
    assert "Cancelled buy of 100 SPY" == row.headline


def test_author_halts_when_event_missing_symbol_or_side() -> None:
    event = _fill_event(symbol=None)
    intent = _intent()
    with pytest.raises(UnauthorableEventError):
        author_row_from_event(event=event, intent=intent, ctx=_ctx())

    event = _fill_event(side=None)
    with pytest.raises(UnauthorableEventError):
        author_row_from_event(event=event, intent=intent, ctx=_ctx())

    event = _fill_event(order_type=None)
    with pytest.raises(UnauthorableEventError):
        author_row_from_event(event=event, intent=intent, ctx=_ctx())


# ── Net-amount sign convention ─────────────────────────────────────────


def test_net_amount_negative_for_buy_includes_commission() -> None:
    event = _fill_event(side="BUY", fill_quantity=100.0, last_fill_price=450.0, fee=1.0)
    intent = _intent()
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())
    # debit: -(gross + fee) = -(45000 + 1) = -45001
    assert row.net_amount == pytest.approx(-45001.0, abs=1e-9)


def test_net_amount_positive_for_sell_credits_minus_commission() -> None:
    event = _fill_event(side="SELL", fill_quantity=100.0, last_fill_price=450.0, fee=1.0)
    intent = _intent()
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())
    # credit: gross - fee = 45000 - 1 = 44999
    assert row.net_amount == pytest.approx(44999.0, abs=1e-9)


# ── author_pending_row ─────────────────────────────────────────────────


def test_author_pending_row_renders_pending_template() -> None:
    intent = _intent()
    row = author_pending_row(
        intent=intent,
        symbol="SPY",
        side="BUY",
        quantity=100.0,
        order_type="MKT",
        ctx=_ctx(),
    )
    assert row.verdict == Verdict.ENGINE_ONLY_PENDING
    assert row.template_key == "pending_acknowledgement"
    assert row.exec_id is None
    assert row.perm_id is None
    assert row.exec_ts_ms is None
    assert row.order_ref == f"{NS}:{INTENT_ID}"
    assert "Pending buy of 100 SPY" == row.headline
    assert row.reason_codes == (ReasonCode.PENDING_ACKNOWLEDGEMENT,)


# ── Engine-overlay survives through the row ────────────────────────────


def test_engine_overlay_carries_sizing_provenance() -> None:
    event = _fill_event()
    intent = _intent(
        sizing_provenance=SizingProvenance(
            policy="SetHoldings",
            requested_qty=100.0,
            provenance="reference_native",
            surface="policy_set_holdings",
        )
    )
    row = author_row_from_event(event=event, intent=intent, ctx=_ctx())
    assert row.engine_overlay is not None
    assert row.engine_overlay.sizing_provenance is not None
    assert row.engine_overlay.sizing_provenance.policy == "SetHoldings"
    assert row.engine_overlay.sizing_provenance.surface == "policy_set_holdings"


# ── Template rendering determinism (truthfulness property) ─────────────


def test_render_template_is_pure_function_of_facts() -> None:
    """Property: rendering the same template with the same facts twice
    must produce the same (headline, narrative). No clock, no global
    state, no randomness."""
    facts = {
        "quantity": 100.0,
        "symbol": "SPY",
        "price": 450.0,
        "order_type": "MKT",
        "commission": 1.0,
    }
    a = render_template("normal_fill", 1, facts)
    b = render_template("normal_fill", 1, facts)
    assert a == b
