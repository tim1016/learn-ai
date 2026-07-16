"""Regression coverage for immutable operator journal cures (#1059)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_clerk_journal import AccountClerkJournal, read_account_clerk_journal
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import AccountInstanceBinding, write_account_instance_binding
from app.engine.live.journal_exposure import project_journal_account_exposure, project_journal_exposure
from app.schemas.journal_cures import JournalCureRequest
from app.services.journal_cures import JournalCureError, JournalCureService

ACCOUNT = "DU123456"
NAMESPACE = "learn-ai/bot-a/v1"


def _intent() -> AccountOwnerSubmitIntent:
    return AccountOwnerSubmitIntent(
        trace_id="trace-cure",
        account_id=ACCOUNT,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=NAMESPACE,
        intent_id="intent-cure",
        order_ref=f"{NAMESPACE}:intent-cure",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=100,
    )


def _request(**overrides: object) -> JournalCureRequest:
    payload = {
        "bot_order_namespace": NAMESPACE,
        "symbol": "spy",
        "signed_quantity": -2,
        "reason": "stale local claim after operator reconciliation",
        "evidence_refs": ("account-reconciliation:receipt-1",),
        "request_provenance": "account-monitor/cure",
        "idempotency_key": "cure-1",
    }
    payload.update(overrides)
    return JournalCureRequest.model_validate(payload)


def _journal_with_fill(root: Path) -> None:
    journal = AccountClerkJournal(artifacts_root=root, account_id=ACCOUNT, now_ms=lambda: 100)
    intent = _intent()
    journal.record_intent(intent, validate_intent=lambda _: None)
    journal.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=2,
            exec_id="cure-fill-1",
            ts_ms=100,
        )
    )
    write_account_instance_binding(
        root,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace=NAMESPACE,
            lifecycle_state="RETIRED",
            recorded_at_ms=101,
            source="test.retired",
        ),
    )


def test_apply_reduces_only_namespace_claim_without_rewriting_broker_fill(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)

    receipt = JournalCureService(artifacts_root=tmp_path).apply(
        account_id=ACCOUNT,
        request=_request(),
        now_ms=200,
    )

    entries = read_account_clerk_journal(tmp_path, ACCOUNT)
    assert receipt.journal_seq == 3
    assert [entry.entry_kind for entry in entries] == ["recorded", "broker_event", "operator_adjustment"]
    assert project_journal_exposure(entries, account_id=ACCOUNT) == ()
    assert [(row.symbol, row.quantity) for row in project_journal_account_exposure(entries, account_id=ACCOUNT)] == [
        ("SPY", 2.0)
    ]


def test_apply_replays_identical_key_and_rejects_conflicting_reuse(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)
    service = JournalCureService(artifacts_root=tmp_path)

    first = service.apply(account_id=ACCOUNT, request=_request(), now_ms=200)
    repeated = service.apply(account_id=ACCOUNT, request=_request(), now_ms=300)

    assert repeated == first
    with pytest.raises(JournalCureError, match="different cure payload"):
        service.apply(account_id=ACCOUNT, request=_request(reason="different"), now_ms=300)


def test_apply_revalidates_the_claim_before_each_new_adjustment(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)
    service = JournalCureService(artifacts_root=tmp_path)
    service.apply(account_id=ACCOUNT, request=_request(), now_ms=200)

    with pytest.raises(JournalCureError, match="no Clerk-attributed claim"):
        service.apply(
            account_id=ACCOUNT,
            request=_request(idempotency_key="cure-2", signed_quantity=-1),
            now_ms=201,
        )


def test_apply_cannot_invent_an_explanation_for_external_broker_exposure(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)

    with pytest.raises(JournalCureError, match="must only reduce"):
        JournalCureService(artifacts_root=tmp_path).apply(
            account_id=ACCOUNT,
            request=_request(signed_quantity=2),
            now_ms=200,
        )


def test_preview_reports_the_server_derived_direction_for_a_reducible_claim(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)

    preview = JournalCureService(artifacts_root=tmp_path).preview(
        account_id=ACCOUNT,
        bot_order_namespace=NAMESPACE,
        symbol="spy",
    )

    assert preview.journal_quantity == 2
    assert preview.required_adjustment_sign == "negative"
    assert preview.can_cure is True
    assert preview.confirmation is not None
    assert preview.confirmation.confirm_label == "Append journal cure"


def test_apply_refuses_a_claim_from_an_active_namespace(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-active",
            bot_order_namespace=NAMESPACE,
            lifecycle_state="ACTIVE",
            recorded_at_ms=200,
            source="test.active",
        ),
    )

    with pytest.raises(JournalCureError, match="registry-proven retired"):
        JournalCureService(artifacts_root=tmp_path).apply(
            account_id=ACCOUNT, request=_request(), now_ms=201
        )


def test_preview_refuses_a_claim_from_an_active_namespace(tmp_path: Path) -> None:
    _journal_with_fill(tmp_path)
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-active",
            bot_order_namespace=NAMESPACE,
            lifecycle_state="ACTIVE",
            recorded_at_ms=200,
            source="test.active",
        ),
    )

    preview = JournalCureService(artifacts_root=tmp_path).preview(
        account_id=ACCOUNT,
        bot_order_namespace=NAMESPACE,
        symbol="SPY",
    )

    assert preview.can_cure is False
    assert preview.reason_code == "JOURNAL_CURE_NAMESPACE_NOT_PROVEN_RETIRED"


def test_request_rejects_blank_evidence_reference() -> None:
    with pytest.raises(ValueError, match="evidence_refs must not contain blank references"):
        _request(evidence_refs=(" ",))
