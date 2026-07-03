"""Account-scoped lifecycle classifier tests."""

from __future__ import annotations

from app.engine.live.account_artifacts import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
)
from app.engine.live.account_classifier import (
    AccountBaselineEvidence,
    AccountBrokerEvidence,
    AccountDurableIntent,
    AccountOperatorOverride,
    classify_account,
)
from app.engine.live.order_identity import build_order_ref, mint_intent_id
from app.engine.live.reconciliation_classifier import (
    BrokerExecutionView,
    BrokerOrderView,
    BrokerSnapshot,
)

ACCOUNT = "DU123456"
SID = "spy_ema_paper"
RUN_ID = "run-alpha"
NS = bot_order_namespace_for_instance(SID)
NOW_MS = 1_700_000_100_000


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=ACCOUNT,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_700_000_000_000,
        source="test",
    )


def _intent(intent_id: str) -> AccountDurableIntent:
    return AccountDurableIntent(
        account_id=ACCOUNT,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        intent_id=intent_id,
        order_ref=build_order_ref(NS, intent_id),
        status="SUBMITTED",
        recorded_at_ms=1_700_000_000_100,
    )


def test_account_classifier_continues_when_broker_matches_registry_and_intent() -> None:
    intent_id = mint_intent_id()
    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=build_order_ref(NS, intent_id)),))
        ),
        registry_bindings=(_binding(),),
        durable_intents=(_intent(intent_id),),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "continue"
    assert decision.reason == "ACCOUNT_STATE_MATCHES_REGISTRY"
    assert decision.strategy_instance_id == SID
    assert decision.run_id == RUN_ID
    assert decision.to_gate_result().status == "pass"


def test_account_classifier_adopts_registered_namespace_without_durable_intent() -> None:
    intent_id = mint_intent_id()
    order_ref = build_order_ref(NS, intent_id)

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(
                open_orders=(BrokerOrderView(order_ref=order_ref, status="Submitted", remaining=1.0),)
            )
        ),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "adopt"
    assert decision.reason == "REGISTERED_NAMESPACE_BROKER_ORPHAN"
    assert decision.affected_order_refs == (order_ref,)
    assert decision.to_gate_result().operator_next_step == "ADOPT_BROKER_EVIDENCE"


def test_account_classifier_ignores_baseline_covered_historical_execution() -> None:
    baseline = AccountBaselineEvidence(
        baseline_id="baseline-1",
        cutoff_ms=1_700_000_000_000,
        source="fleet_reset_baseline",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(
                executions=(
                    BrokerExecutionView(
                        order_ref=f"learn-ai/retired/v1:{mint_intent_id()}",
                        exec_time_ms=1_699_999_999_999,
                    ),
                )
            )
        ),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=baseline,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "ignore_baseline"
    assert decision.baseline_id == "baseline-1"
    assert decision.override_id is None
    assert decision.to_gate_result().status == "pass"


def test_account_classifier_retry_records_operator_override_separately_from_baseline() -> None:
    override = AccountOperatorOverride(
        override_id="override-1",
        decision="retry",
        reason="broker maintenance window",
        approved_at_ms=1_700_000_050_000,
        approved_by="operator",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(status="retryable_unavailable", detail="gateway reconnecting"),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=AccountBaselineEvidence(
            baseline_id="baseline-1",
            cutoff_ms=1,
            source="fleet_reset_baseline",
        ),
        operator_override=override,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "retry"
    assert decision.override_id == "override-1"
    assert decision.baseline_id is None
    assert decision.to_gate_result().status == "unknown"


def test_account_classifier_honors_explicit_freeze_override_before_broker_snapshot() -> None:
    override = AccountOperatorOverride(
        override_id="override-freeze",
        decision="freeze",
        reason="operator froze account after manual review",
        approved_at_ms=1_700_000_050_000,
        approved_by="operator",
        account_id=ACCOUNT,
        valid_until_ms=1_700_000_150_000,
        prior_evidence={"manual_review": "unsafe"},
        next_reconciliation_step="CHECK_IBKR",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(snapshot=BrokerSnapshot()),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=override,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "freeze"
    assert decision.reason == "OPERATOR_OVERRIDE_FREEZE"
    assert decision.override_id == "override-freeze"


def test_account_classifier_allows_fresh_audited_continue_override_for_unprovable_broker() -> None:
    override = AccountOperatorOverride(
        override_id="override-continue",
        decision="continue",
        reason="operator verified broker state out of band",
        approved_at_ms=1_700_000_050_000,
        approved_by="operator",
        account_id=ACCOUNT,
        valid_until_ms=1_700_000_150_000,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(status="unprovable", detail="gateway unreachable"),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=override,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "continue"
    assert decision.reason == "OPERATOR_OVERRIDE_CONTINUE"
    assert decision.override_id == "override-continue"
    assert decision.to_gate_result().status == "pass"


def test_account_classifier_rejects_stale_operator_override_as_freeze() -> None:
    override = AccountOperatorOverride(
        override_id="override-stale",
        decision="continue",
        reason="operator verified broker state out of band",
        approved_at_ms=1_700_000_050_000,
        approved_by="operator",
        account_id=ACCOUNT,
        valid_until_ms=NOW_MS - 1,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(status="unprovable", detail="gateway unreachable"),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=override,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "freeze"
    assert decision.reason == "OPERATOR_OVERRIDE_STALE"
    assert decision.override_id == "override-stale"


def test_account_classifier_refreezes_contradicted_continue_override() -> None:
    override = AccountOperatorOverride(
        override_id="override-continue",
        decision="continue",
        reason="operator believed broker state was clean",
        approved_at_ms=1_700_000_050_000,
        approved_by="operator",
        account_id=ACCOUNT,
        valid_until_ms=1_700_000_150_000,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(
                open_orders=(
                    BrokerOrderView(
                        order_ref=f"learn-ai/unknown/v1:{mint_intent_id()}",
                        status="Submitted",
                        remaining=1.0,
                    ),
                )
            )
        ),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=override,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "freeze"
    assert decision.reason == "OPERATOR_OVERRIDE_CONTRADICTED"
    assert decision.override_id == "override-continue"


def test_account_classifier_freezes_unprovable_broker_state() -> None:
    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(status="unprovable", detail="broker snapshot incomplete"),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "freeze"
    assert decision.reason == "BROKER_STATE_UNPROVABLE"
    assert decision.to_gate_result().status == "freeze"


def test_account_classifier_duplicate_namespace_uses_latest_binding_per_instance() -> None:
    deployed = _binding().model_copy(update={"lifecycle_state": "DEPLOYED", "recorded_at_ms": NOW_MS - 1000})
    active = _binding().model_copy(update={"lifecycle_state": "ACTIVE", "recorded_at_ms": NOW_MS})

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(snapshot=BrokerSnapshot()),
        registry_bindings=(deployed, active),
        durable_intents=(),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "continue"
    assert decision.reason == "ACCOUNT_STATE_MATCHES_REGISTRY"


def test_account_classifier_uses_case_insensitive_account_registry_filter() -> None:
    intent_id = mint_intent_id()
    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=build_order_ref(NS, intent_id)),))
        ),
        registry_bindings=(_binding().model_copy(update={"account_id": ACCOUNT.lower()}),),
        durable_intents=(_intent(intent_id),),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "continue"
    assert decision.reason == "ACCOUNT_STATE_MATCHES_REGISTRY"
    assert decision.strategy_instance_id == SID


def test_account_classifier_uses_timestamp_latest_registry_fold_for_submit_guard() -> None:
    intent_id = mint_intent_id()
    active = _binding().model_copy(
        update={
            "lifecycle_state": "ACTIVE",
            "recorded_at_ms": NOW_MS,
        }
    )
    older_retired_appended_later = _binding().model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": NOW_MS - 1,
            "source": "host_daemon.stop_exited",
        }
    )

    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(
            snapshot=BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=build_order_ref(NS, intent_id)),))
        ),
        registry_bindings=(active, older_retired_appended_later),
        durable_intents=(_intent(intent_id),),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "continue"
    assert decision.reason == "ACCOUNT_STATE_MATCHES_REGISTRY"
    assert decision.run_id == RUN_ID


def test_account_classifier_poisons_run_for_no_order_ref() -> None:
    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(snapshot=BrokerSnapshot(open_orders=(BrokerOrderView(order_ref=None),))),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "poison_run"
    assert decision.reason == "NO_ORDER_REF"
    assert decision.to_gate_result().status == "poison"


def test_account_classifier_unknown_snapshot_is_freeze_gate_not_continue() -> None:
    decision = classify_account(
        account_id=ACCOUNT,
        broker=AccountBrokerEvidence(status="unknown", detail="no broker evidence yet"),
        registry_bindings=(_binding(),),
        durable_intents=(),
        baseline=None,
        operator_override=None,
        now_ms=NOW_MS,
    )

    assert decision.outcome == "unknown"
    assert decision.reason == "BROKER_STATE_UNKNOWN"
    assert decision.to_gate_result().status == "freeze"
