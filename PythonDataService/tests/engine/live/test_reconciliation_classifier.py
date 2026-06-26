"""Module E (reconciliation_classifier) unit tests — ADR-0008 §5 / PRD plan E.

continue on match; adopt an owned orphan (exact-namespace, unknown intent);
poison on outside mutation (unknown ns / no ref / foreign perm); order_id alone
never owns; the /v10-vs-/v1 collision poisons; prior-run in-flight tail
resolves; emergency-flatten audit adopted by namespace; ambiguous exposure
pauses; poison takes precedence over adoption.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_ledger import LedgerView, SubmittedOrderView
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)
from app.engine.live.reconciliation_classifier import (
    Adopt,
    BrokerExecutionView,
    BrokerOrderView,
    BrokerSnapshot,
    Continue,
    Poison,
    classify,
)

NS = build_bot_order_namespace("foo")  # learn-ai/foo/v1
ALLOWED = frozenset({NS})


def _view(
    *,
    submitted: Mapping[str, SubmittedOrderView] | None = None,
    perms: frozenset[int] = frozenset(),
    execs: frozenset[str] = frozenset(),
) -> LedgerView:
    return LedgerView(
        submitted_orders=submitted or {},
        known_perm_ids=perms,
        known_exec_ids=execs,
        last_seq=0,
        unresolved_intent_ids=frozenset(),
    )


def _known_order(intent_id: str, *, perm_id: int, order_id: int = 0) -> SubmittedOrderView:
    return SubmittedOrderView(
        intent_id=intent_id,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, intent_id),
        status=IntentEventType.SUBMITTED,
        order_id=order_id,
        perm_id=perm_id,
    )


def _pending_event(intent_id: str) -> IntentEvent:
    return IntentEvent(
        seq=1,
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=intent_id,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, intent_id),
    )


def test_continue_on_match() -> None:
    iid = mint_intent_id()
    view = _view(submitted={iid: _known_order(iid, perm_id=900)}, perms=frozenset({900}))
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=build_order_ref(NS, iid), perm_id=900, status="Filled"),
        )
    )
    assert isinstance(classify(projection=view, broker_snapshot=snap, allowed_namespaces=ALLOWED), Continue)


def test_adopt_owned_orphan() -> None:
    iid = mint_intent_id()  # unknown to the empty projection
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=build_order_ref(NS, iid), perm_id=7, status="Filled", remaining=0.0),
        )
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Adopt)
    assert len(verdict.orphans) == 1
    assert verdict.orphans[0].intent_id == iid
    assert verdict.pause is False  # filled, not active


def test_poison_unknown_namespace() -> None:
    snap = BrokerSnapshot(
        open_orders=(BrokerOrderView(order_ref=f"learn-ai/other/v1:{mint_intent_id()}"),)
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)
    assert verdict.reason == "unknown_namespace"


def test_prior_unknown_namespace_execution_can_be_covered_by_fleet_reset_baseline() -> None:
    snap = BrokerSnapshot(
        executions=(
            BrokerExecutionView(
                order_ref=f"learn-ai/retired/v1:{mint_intent_id()}",
                exec_time_ms=1_700_000_000_000,
            ),
        )
    )
    verdict = classify(
        projection=_view(),
        broker_snapshot=snap,
        allowed_namespaces=ALLOWED,
        ignore_unknown_namespaces_before_ms=1_700_000_000_001,
    )
    assert isinstance(verdict, Continue)


def test_unknown_namespace_after_fleet_reset_baseline_still_poisons() -> None:
    snap = BrokerSnapshot(
        executions=(
            BrokerExecutionView(
                order_ref=f"learn-ai/retired/v1:{mint_intent_id()}",
                exec_time_ms=1_700_000_000_002,
            ),
        )
    )
    verdict = classify(
        projection=_view(),
        broker_snapshot=snap,
        allowed_namespaces=ALLOWED,
        ignore_unknown_namespaces_before_ms=1_700_000_000_001,
    )
    assert isinstance(verdict, Poison)
    assert verdict.reason == "unknown_namespace"


def test_unknown_namespace_open_order_ignores_no_fleet_reset_baseline() -> None:
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(
                order_ref=f"learn-ai/retired/v1:{mint_intent_id()}",
                status="Submitted",
                remaining=1.0,
            ),
        )
    )
    verdict = classify(
        projection=_view(),
        broker_snapshot=snap,
        allowed_namespaces=ALLOWED,
        ignore_unknown_namespaces_before_ms=1_700_000_000_001,
    )
    assert isinstance(verdict, Poison)
    assert verdict.reason == "unknown_namespace"


def test_poison_no_order_ref() -> None:
    snap = BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=None, perm_id=None),))
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)
    assert verdict.reason == "no_order_ref"


def test_poison_foreign_perm_id() -> None:
    snap = BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=None, perm_id=555),))
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)
    assert verdict.reason == "foreign_perm_id"


def test_order_id_alone_never_proves_ownership() -> None:
    iid = mint_intent_id()
    view = _view(submitted={iid: _known_order(iid, perm_id=900, order_id=7)}, perms=frozenset({900}))
    # broker order shares only order_id=7 — no ref, no known perm.
    snap = BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=None, perm_id=None, order_id=7),))
    verdict = classify(projection=view, broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)  # order_id never rescues it


def test_v10_prefix_collision_poisons() -> None:
    # learn-ai/foo/v10 must NOT be claimed by the namespace learn-ai/foo/v1.
    snap = BrokerSnapshot(
        open_orders=(BrokerOrderView(order_ref=f"learn-ai/foo/v10:{mint_intent_id()}"),)
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)
    assert verdict.reason == "unknown_namespace"


def test_prior_run_inflight_tail_present_is_adopted() -> None:
    iid = mint_intent_id()
    tail = [_pending_event(iid)]
    snap = BrokerSnapshot(
        open_orders=(BrokerOrderView(order_ref=build_order_ref(NS, iid), perm_id=3, status="Submitted"),)
    )
    verdict = classify(
        projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED,
        prior_run_unacked_tail=tail,
    )
    assert isinstance(verdict, Adopt)
    assert verdict.orphans[0].source == "prior_run_tail"
    assert verdict.pause is True  # still active → ambiguous exposure


def test_prior_run_inflight_tail_absent_at_broker_is_continue() -> None:
    # We attempted it last run but the broker shows nothing — it never landed.
    tail = [_pending_event(mint_intent_id())]
    verdict = classify(
        projection=_view(), broker_snapshot=BrokerSnapshot(), allowed_namespaces=ALLOWED,
        prior_run_unacked_tail=tail,
    )
    assert isinstance(verdict, Continue)


def test_emergency_flatten_audit_adopted_by_namespace() -> None:
    iid = mint_intent_id()
    audit = [_pending_event(iid)]  # an order_ref-stamped emergency record
    snap = BrokerSnapshot(
        executions=(BrokerExecutionView(order_ref=build_order_ref(NS, iid), perm_id=9, exec_id="x1"),)
    )
    verdict = classify(
        projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED,
        emergency_audit=audit,
    )
    assert isinstance(verdict, Adopt)
    assert verdict.orphans[0].source == "emergency_flatten"


def test_adopt_pause_on_active_exposure() -> None:
    iid = mint_intent_id()
    snap = BrokerSnapshot(
        open_orders=(BrokerOrderView(order_ref=build_order_ref(NS, iid), status="Submitted", remaining=5.0),)
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Adopt)
    assert verdict.pause is True
    assert verdict.pause_reason == "ambiguous_exposure"


def test_poison_takes_precedence_over_adopt() -> None:
    own = build_order_ref(NS, mint_intent_id())
    foreign = f"learn-ai/other/v1:{mint_intent_id()}"
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=own, status="Filled"),
            BrokerOrderView(order_ref=foreign),
        )
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)


def test_unparseable_order_ref_poisons() -> None:
    snap = BrokerSnapshot(open_orders=(BrokerOrderView(order_ref="garbled-no-colon"),))
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Poison)
    assert verdict.reason == "unparseable_order_ref"


def test_foreign_ref_rescued_by_known_perm_is_continue() -> None:
    # A known perm_id proves ownership even if the echoed ref looks foreign
    # (ADR-0008 §1 rung 3) — it must NOT poison.
    view = _view(perms=frozenset({900}))
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=f"learn-ai/other/v1:{mint_intent_id()}", perm_id=900),
        )
    )
    assert isinstance(
        classify(projection=view, broker_snapshot=snap, allowed_namespaces=ALLOWED), Continue
    )


def test_known_but_unresolved_intent_live_at_broker_is_recovered_not_continued() -> None:
    # Codex P2 regression: a PENDING_INTENT folded from this run's WAL is IN
    # submitted_orders but UNRESOLVED. A broker order carrying its order_ref must
    # be recovered (Adopt), not silently Continue'd — else we resume with the
    # in-flight order unresolved and reopen the double-submit window.
    iid = mint_intent_id()
    view = LedgerView(
        submitted_orders={
            iid: SubmittedOrderView(
                intent_id=iid,
                bot_order_namespace=NS,
                order_ref=build_order_ref(NS, iid),
                status=IntentEventType.PENDING_INTENT,
            )
        },
        known_perm_ids=frozenset(),
        known_exec_ids=frozenset(),
        last_seq=1,
        unresolved_intent_ids=frozenset({iid}),
    )
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=build_order_ref(NS, iid), status="Submitted", remaining=10.0),
        )
    )
    verdict = classify(projection=view, broker_snapshot=snap, allowed_namespaces=ALLOWED)
    assert isinstance(verdict, Adopt)
    assert verdict.orphans[0].intent_id == iid
    assert verdict.orphans[0].source == "this_run_unresolved"
    assert verdict.pause is True  # still active → ambiguous exposure


def test_known_resolved_intent_live_at_broker_continues() -> None:
    # Contrast: a RESOLVED known intent (SUBMITTED, perm recorded) present at the
    # broker is a true match → Continue.
    iid = mint_intent_id()
    view = LedgerView(
        submitted_orders={
            iid: SubmittedOrderView(
                intent_id=iid,
                bot_order_namespace=NS,
                order_ref=build_order_ref(NS, iid),
                status=IntentEventType.SUBMITTED,
                perm_id=900,
            )
        },
        known_perm_ids=frozenset({900}),
        known_exec_ids=frozenset(),
        last_seq=2,
        unresolved_intent_ids=frozenset(),
    )
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=build_order_ref(NS, iid), perm_id=900, status="Filled"),
        )
    )
    assert isinstance(
        classify(projection=view, broker_snapshot=snap, allowed_namespaces=ALLOWED), Continue
    )


def test_dual_read_window_adopts_both_versions() -> None:
    allowed = frozenset({NS, build_bot_order_namespace("foo").replace("/v1", "/v2")})
    iid1, iid2 = mint_intent_id(), mint_intent_id()
    snap = BrokerSnapshot(
        open_orders=(
            BrokerOrderView(order_ref=f"learn-ai/foo/v1:{iid1}", status="Filled"),
            BrokerOrderView(order_ref=f"learn-ai/foo/v2:{iid2}", status="Filled"),
        )
    )
    verdict = classify(projection=_view(), broker_snapshot=snap, allowed_namespaces=allowed)
    assert isinstance(verdict, Adopt)
    assert {o.intent_id for o in verdict.orphans} == {iid1, iid2}
