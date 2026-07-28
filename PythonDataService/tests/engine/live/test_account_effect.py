"""Regression matrix for the server-derived suspended-account effect lane."""

from __future__ import annotations

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_clerk_journal_models import AccountClerkAsyncCustodyConfig, AccountClerkIntentRejected
from app.engine.live.account_effect import (
    AccountEffectClass,
    AccountEffectClassifier,
    AccountEffectEvidence,
    AccountEffectProjection,
    AccountEffectPurpose,
    AccountEffectRequest,
    classify_account_effect,
)
from app.engine.live.account_epoch import AccountEpoch, AccountEpochState
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import AccountInstanceBinding
from app.engine.live.account_safety import AccountSafetyAuthority, RetiredOwnerCustody
from app.services.account_reconciliation import AccountReconciliationService

_NOW_MS = 1_780_000_010_000
_ACCOUNT_ID = "DU1234567"
_NAMESPACE = "learn-ai/bot-a/v1"


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id=_ACCOUNT_ID,
        is_paper=True,
        fetched_at_ms=_NOW_MS - 100,
        connection_state="connected",
        last_transition_ms=_NOW_MS - 100,
    )


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=_ACCOUNT_ID,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=_NAMESPACE,
        lifecycle_state="ACTIVE",
        recorded_at_ms=_NOW_MS - 100,
        source="test",
    )


def _projection(
    *,
    positions: list[IbkrPosition] | None = None,
    open_orders: list[IbkrOpenOrder] | None = None,
    observed_at_ms: int = _NOW_MS - 100,
    expires_at_ms: int = _NOW_MS + 10_000,
    epoch_observed_at_ms: int = _NOW_MS - 200,
) -> AccountEffectProjection:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account_recovery_state=AccountRecoveryState.clear(_ACCOUNT_ID),
        account=IbkrAccountSummary(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            base_currency="USD",
            net_liquidation=100_000.0,
            buying_power=50_000.0,
            fetched_at_ms=_NOW_MS - 100,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            positions=positions or [],
            fetched_at_ms=_NOW_MS - 100,
        ),
        open_orders=open_orders or [],
        completed_orders=[],
        executions=[],
        evidence_gaps=[],
        generated_at_ms=observed_at_ms,
    )
    return AccountEffectProjection(
        account_id=_ACCOUNT_ID,
        receipt_id="receipt-1",
        observed_at_ms=observed_at_ms,
        expires_at_ms=expires_at_ms,
        epoch=AccountEpoch(clerk_boot_id="clerk-a", epoch_seq=2),
        epoch_observed_at_ms=epoch_observed_at_ms,
        truth=truth,
    )


def _intent(
    *,
    effect_request: AccountEffectRequest | None = None,
    action: str = "SELL",
    quantity: float = 1.0,
) -> AccountOwnerSubmitIntent:
    return AccountOwnerSubmitIntent(
        trace_id="trace-a",
        account_id=_ACCOUNT_ID,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=_NAMESPACE,
        intent_id="intent-a",
        order_ref=f"{_NAMESPACE}:intent-a",
        intent_kind="STRATEGY",
        order_spec={
            "symbol": "SPY",
            "sec_type": "STK",
            "con_id": 756733,
            "action": action,
            "quantity": quantity,
            "order_type": "MKT",
            "confirm_paper": True,
            "order_ref": f"{_NAMESPACE}:intent-a",
        },
        effect_request=effect_request,
        owner_generation=1,
        created_at_ms=_NOW_MS - 300,
    )


def _position(*, quantity: float = 2.0) -> IbkrPosition:
    return IbkrPosition(
        account_id=_ACCOUNT_ID,
        con_id=756733,
        symbol="SPY",
        sec_type="STK",
        quantity=quantity,
        avg_cost=500.0,
        fetched_at_ms=_NOW_MS - 100,
    )


def _open_order() -> IbkrOpenOrder:
    return IbkrOpenOrder(
        account_id=_ACCOUNT_ID,
        order_id=42,
        perm_id=9001,
        client_id=7,
        con_id=756733,
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=1.0,
        order_type="MKT",
        limit_price=None,
        time_in_force="DAY",
        status="Submitted",
        cumulative_filled=0.0,
        remaining=1.0,
        avg_fill_price=None,
        order_ref=f"{_NAMESPACE}:open-intent",
        fetched_at_ms=_NOW_MS - 100,
    )


def test_classify_exact_current_order_cancel_ignores_client_risk_flag() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CANCEL,
            target_order_id=42,
            target_order_ref=f"{_NAMESPACE}:open-intent",
        )
    )

    evidence = classify_account_effect(intent, projection=_projection(open_orders=[_open_order()]), now_ms=_NOW_MS)

    assert evidence.effect_class is AccountEffectClass.EXACT_CANCEL
    assert evidence.target_order_id == 42
    assert evidence.target_order_ref == f"{_NAMESPACE}:open-intent"


def test_classify_exact_close_allows_only_non_flipping_current_position() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        ),
        action="SELL",
        quantity=2.0,
    )

    evidence = classify_account_effect(intent, projection=_projection(positions=[_position()]), now_ms=_NOW_MS)

    assert evidence.effect_class is AccountEffectClass.EXACT_CLOSE
    assert evidence.target_signed_quantity == 2.0
    assert evidence.requested_quantity == 2.0


def test_classify_exact_close_requires_the_proven_contract_and_order_ref() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        ),
        action="SELL",
        quantity=2.0,
    )
    projection = _projection(positions=[_position()])

    missing_con_id = classify_account_effect(
        intent.model_copy(update={"order_spec": {**intent.order_spec, "con_id": None}}),
        projection=projection,
        now_ms=_NOW_MS,
    )
    wrong_con_id = classify_account_effect(
        intent.model_copy(update={"order_spec": {**intent.order_spec, "con_id": 123456}}),
        projection=projection,
        now_ms=_NOW_MS,
    )
    wrong_order_ref = classify_account_effect(
        intent.model_copy(update={"order_spec": {**intent.order_spec, "order_ref": "other:order"}}),
        projection=projection,
        now_ms=_NOW_MS,
    )

    assert missing_con_id.reason_code == "EXACT_CLOSE_EXECUTION_IDENTITY_MISMATCH"
    assert wrong_con_id.reason_code == "EXACT_CLOSE_EXECUTION_IDENTITY_MISMATCH"
    assert wrong_order_ref.reason_code == "EXACT_CLOSE_ORDER_REF_MISMATCH"


def test_classify_exact_close_blocks_symbol_only_flip_and_target_drift() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        ),
        action="SELL",
        quantity=3.0,
    )

    oversized = classify_account_effect(intent, projection=_projection(positions=[_position()]), now_ms=_NOW_MS)
    drifted = classify_account_effect(
        intent.model_copy(update={"order_spec": {**intent.order_spec, "quantity": 2.0}}),
        projection=_projection(positions=[_position(quantity=1.0)]),
        now_ms=_NOW_MS,
    )

    assert oversized.effect_class is AccountEffectClass.AMBIGUOUS
    assert oversized.reason_code == "EXACT_CLOSE_WORST_CASE_NOT_REDUCING"
    assert drifted.effect_class is AccountEffectClass.AMBIGUOUS
    assert drifted.reason_code == "EXACT_CLOSE_POSITION_DRIFTED"


def test_classify_stale_or_pre_epoch_projection_fails_closed() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        ),
        quantity=1.0,
    )

    expired = classify_account_effect(
        intent,
        projection=_projection(positions=[_position()], expires_at_ms=_NOW_MS - 1),
        now_ms=_NOW_MS,
    )
    pre_epoch = classify_account_effect(
        intent,
        projection=_projection(positions=[_position()], epoch_observed_at_ms=_NOW_MS),
        now_ms=_NOW_MS,
    )
    same_millisecond_as_epoch = classify_account_effect(
        intent,
        projection=_projection(
            positions=[_position()],
            observed_at_ms=_NOW_MS - 100,
            epoch_observed_at_ms=_NOW_MS - 100,
        ),
        now_ms=_NOW_MS,
    )

    assert expired.reason_code == "EFFECT_PROJECTION_EXPIRED"
    assert pre_epoch.reason_code == "EFFECT_PROJECTION_EPOCH_UNPROVEN"
    assert same_millisecond_as_epoch.reason_code == "EFFECT_PROJECTION_EPOCH_UNPROVEN"


def test_equivalent_semantic_inputs_have_stable_effect_fingerprint() -> None:
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        )
    )
    projection = _projection(positions=[_position()])

    first = classify_account_effect(intent, projection=projection, now_ms=_NOW_MS)
    second = classify_account_effect(intent, projection=projection, now_ms=_NOW_MS + 1)

    assert first.classification_id == second.classification_id
    assert first.evaluated_at_ms != second.evaluated_at_ms


def test_classifier_reads_cached_reconciliation_without_a_broker_call(tmp_path) -> None:
    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=20_000)
    service.write_receipt(
        requested_account_id=_ACCOUNT_ID,
        account_truth=_projection(positions=[_position()]).truth,
        now_ms=_NOW_MS,
    )
    classifier = AccountEffectClassifier(artifacts_root=tmp_path, account_id=_ACCOUNT_ID)
    epoch_state = AccountEpochState(
        account_id=_ACCOUNT_ID,
        current_epoch=AccountEpoch(clerk_boot_id="clerk-a", epoch_seq=2),
        clerk_generation=1,
        status="INVALID",
        would_block_reason="RETIRED_OWNER_EXPOSURE",
        required_reconciliation_depth="full",
        reconciliation_verdict="RECONCILING",
        reconciliation_id="epoch-reconciliation-1",
        updated_at_ms=_NOW_MS - 200,
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        )
    )

    evidence = classifier.classify(intent, epoch_state=epoch_state, now_ms=_NOW_MS)

    assert evidence.effect_class is AccountEffectClass.EXACT_CLOSE
    assert evidence.projection_observed_at_ms == _NOW_MS - 100


class _PaperSettings:
    mode = "paper"


class _PaperClient:
    settings = _PaperSettings()


class _Broker:
    def __init__(self) -> None:
        self._client = _PaperClient()
        self.calls = 0
        self.cancelled_exact: list[tuple[int, str]] = []

    async def place_order(self, _spec: object) -> object:
        self.calls += 1
        return type("Ack", (), {"order_id": 91, "perm_id": 190, "exec_id": None})()

    async def cancel_exact_open_order(self, *, order_id: int, order_ref: str) -> list[int]:
        self.cancelled_exact.append((order_id, order_ref))
        return [42]


class _CountingEffectClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        epoch_state: object,
        now_ms: int,
    ) -> AccountEffectEvidence:
        del epoch_state
        self.calls += 1
        is_cancel = intent.effect_request is not None and intent.effect_request.purpose is AccountEffectPurpose.EXACT_CANCEL
        return AccountEffectEvidence(
            classification_id="a" * 64,
            effect_class=(AccountEffectClass.EXACT_CANCEL if is_cancel else AccountEffectClass.EXACT_CLOSE),
            reason_code=("EXACT_CURRENT_ORDER_CANCEL" if is_cancel else "EXACT_CURRENT_POSITION_CLOSE"),
            account_id=intent.account_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            projection_receipt_id="receipt-1",
            projection_observed_at_ms=now_ms,
            target_order_id=(42 if is_cancel else None),
            target_order_ref=(intent.effect_request.target_order_ref if is_cancel else None),
            target_con_id=(None if is_cancel else 756733),
            target_signed_quantity=(None if is_cancel else 2.0),
            requested_quantity=(None if is_cancel else 1.0),
            evaluated_at_ms=now_ms,
        )


async def test_suspended_clerk_rejects_exact_close_without_a_broker_atomic_reduce_only_guard(tmp_path) -> None:
    from app.engine.live.account_registry import write_account_instance_binding

    write_account_instance_binding(tmp_path, _binding())
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: _NOW_MS,
    )
    safety.suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )
    classifier = _CountingEffectClassifier()
    broker = _Broker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        broker=broker,
        safety_authority=safety,
        effect_classifier=classifier,  # type: ignore[arg-type]
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        )
    )

    await clerk.start_async_custody()
    try:
        with pytest.raises(AccountClerkIntentRejected) as exc:
            await clerk.submit_async_custody(intent)
    finally:
        await clerk.stop_async_custody()

    assert exc.value.reason == "CLERK_ATOMIC_REDUCE_ONLY_UNAVAILABLE"
    assert exc.value.diagnostics["capability_blocker"] == "BROKER_ATOMIC_REDUCE_ONLY_UNAVAILABLE"
    assert broker.calls == 0
    assert classifier.calls == 1
    assert await clerk.reconciliation_snapshot() == []


async def test_suspended_clerk_cancels_only_the_server_proved_exact_order(tmp_path) -> None:
    from app.engine.live.account_registry import write_account_instance_binding

    write_account_instance_binding(tmp_path, _binding())
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: _NOW_MS,
    )
    safety.suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )
    broker = _Broker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        broker=broker,
        safety_authority=safety,
        effect_classifier=_CountingEffectClassifier(),  # type: ignore[arg-type]
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CANCEL,
            target_order_id=42,
            target_order_ref=f"{_NAMESPACE}:open-intent",
        )
    )

    receipt = await clerk.cancel_exact_order(intent)
    entries = await clerk.reconciliation_snapshot()
    submitting = next(entry for entry in entries if entry.entry_kind == "cancel_submitting")

    assert receipt.cancelled_order_ids == (42,)
    assert broker.cancelled_exact == [(42, f"{_NAMESPACE}:open-intent")]
    assert submitting.effect_evidence is not None
    assert submitting.effect_evidence.effect_class is AccountEffectClass.EXACT_CANCEL


async def test_suspended_async_custody_never_treats_exact_cancel_as_an_order(tmp_path) -> None:
    from app.engine.live.account_registry import write_account_instance_binding

    write_account_instance_binding(tmp_path, _binding())
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: _NOW_MS,
    )
    safety.suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )
    broker = _Broker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        broker=broker,
        safety_authority=safety,
        effect_classifier=_CountingEffectClassifier(),  # type: ignore[arg-type]
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CANCEL,
            target_order_id=42,
            target_order_ref=f"{_NAMESPACE}:open-intent",
        ),
        action="BUY",
        quantity=100.0,
    )

    await clerk.start_async_custody()
    try:
        try:
            await clerk.submit_async_custody(intent)
        except AccountClerkIntentRejected as exc:
            assert exc.reason == "CLERK_EXACT_CANCEL_ACTION_ENVELOPE_REQUIRED"
        else:  # pragma: no cover - test asserts the public order lane is closed
            raise AssertionError("exact cancel reached asynchronous custody")
    finally:
        await clerk.stop_async_custody()

    assert broker.calls == 0
    assert await clerk.reconciliation_snapshot() == []


async def test_client_close_claim_without_server_projection_never_enters_risk_lane(tmp_path) -> None:
    from app.engine.live.account_registry import write_account_instance_binding

    write_account_instance_binding(tmp_path, _binding())
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: _NOW_MS,
    )
    safety.suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )
    broker = _Broker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        broker=broker,
        safety_authority=safety,
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        )
    )

    await clerk.start_async_custody()
    try:
        try:
            await clerk.submit_async_custody(intent)
        except AccountClerkIntentRejected as exc:
            assert exc.reason == "CLERK_RISK_REDUCING_EFFECT_NOT_PROVEN"
        else:  # pragma: no cover - test asserts the fail-closed boundary
            raise AssertionError("unproven close request entered custody")
    finally:
        await clerk.stop_async_custody()

    assert broker.calls == 0
    assert await clerk.reconciliation_snapshot() == []


async def test_final_broker_admission_rejects_a_preexisting_exact_close_without_reduce_only(tmp_path) -> None:
    from app.engine.live.account_registry import write_account_instance_binding

    write_account_instance_binding(tmp_path, _binding())
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        now_ms=lambda: _NOW_MS,
    )
    safety.suspend_retired_owner_custody(
        [
            RetiredOwnerCustody(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-bot",
                intent_id="retired-intent",
                order_ref="learn-ai/retired/v1:retired-intent",
            )
        ]
    )
    broker = _Broker()
    classifier = _CountingEffectClassifier()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        broker=broker,
        safety_authority=safety,
        effect_classifier=classifier,  # type: ignore[arg-type]
    )
    intent = _intent(
        effect_request=AccountEffectRequest(
            purpose=AccountEffectPurpose.EXACT_CLOSE,
            target_con_id=756733,
            expected_signed_quantity=2.0,
        )
    )

    with pytest.raises(AccountClerkIntentRejected) as exc:
        async with clerk._broker_write_admission(
            "test.final_exact_close_fence",
            risk_reducing_intent=intent,
        ):
            raise AssertionError("unreachable without a broker atomic reduce-only guard")

    assert exc.value.reason == "CLERK_ATOMIC_REDUCE_ONLY_UNAVAILABLE"
    assert classifier.calls == 1
    assert broker.calls == 0
