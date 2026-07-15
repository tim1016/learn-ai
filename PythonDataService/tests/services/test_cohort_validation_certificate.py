"""Durable cohort validation certificate tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchReceipt,
    append_account_event,
    record_cohort_batch_launch_receipt,
)
from app.engine.live.journal_exposure import JournalExposure
from app.services import cohort_validation_certificate as certificate_module
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.cohort_evidence import CohortEvidenceSample, CohortMemberSample
from app.services.cohort_validation_certificate import CohortValidationCertificateService
from app.utils.timestamps import now_ms_utc


async def _seed_evidence(root: Path) -> tuple[CohortValidationCertificateService, CohortBatchLaunchReceipt]:
    now_ms = now_ms_utc()
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="certificate-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=now_ms,
        window_end_ms=now_ms + 30_000,
        authorized_by="operator.alice",
        recorded_at_ms=now_ms,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="spy-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )
    record_cohort_batch_launch_receipt(root, receipt)
    await CohortBatchLaunchService(artifacts_root=root).record_evidence_sample(
        account_id=receipt.account_id,
        cohort_id=receipt.cohort_id,
        sample=CohortEvidenceSample(
            expected_at_ms=now_ms,
            observed_at_ms=now_ms,
            account_truth="healthy",
            fleet="healthy",
            members=(CohortMemberSample("spy-a", "run-a", "healthy", orders_used=1, orders_cap=4),),
            broker_net_positions={},
            broker_residual={},
        ),
    )
    return CohortValidationCertificateService(artifacts_root=root), receipt


def _journal_entry(exec_id: str = "exec-a") -> SimpleNamespace:
    intent = SimpleNamespace(
        strategy_instance_id="spy-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/spy-a/v1",
        order_ref="learn-ai/spy-a/v1:intent-a",
    )
    return SimpleNamespace(
        intent=intent,
        entry_kind="broker_acked",
        broker_event=None,
        order_id=17,
        perm_id=23,
        exec_id=exec_id,
    )


def _fill_event(entry: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        event_type="fill",
        side="BUY" if entry.exec_id == "exec-entry" else "SELL",
        fill_quantity=1.0,
        order_id=entry.order_id,
        perm_id=entry.perm_id,
        exec_id=entry.exec_id,
    )


async def test_certificate_is_deterministic_and_refuses_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [_journal_entry("exec-entry"), _journal_entry("exec-exit")],
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    first = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)
    second = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.verdict == "passed"
    assert first.round_trips[0].exec_ids == ["exec-entry", "exec-exit"]
    service.write_once(first)
    assert service.read(account_id=receipt.account_id, cohort_id=receipt.cohort_id) == first
    with pytest.raises(FileExistsError):
        service.write_once(second)


async def test_certificate_is_incomplete_without_linked_round_trip_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(certificate_module, "read_account_clerk_journal", lambda *_args: [])
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "incomplete"
    assert "INCOMPLETE_ROUND_TRIP_IDENTITY" in certificate.reasons
    assert "INCOMPLETE_MEMBER_NAMESPACE_IDENTITY" in certificate.reasons


async def test_certificate_is_incomplete_when_no_five_second_sample_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = now_ms_utc()
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="missing-sample-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=now_ms,
        window_end_ms=now_ms + 30_000,
        authorized_by="operator.alice",
        recorded_at_ms=now_ms,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="spy-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    service = CohortValidationCertificateService(artifacts_root=tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [_journal_entry("exec-entry"), _journal_entry("exec-exit")],
    )
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "incomplete"
    assert "INCOMPLETE_EVIDENCE_SAMPLES" in certificate.reasons


async def test_certificate_fails_for_nonflat_namespace_or_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(certificate_module, "read_account_clerk_journal", lambda *_args: [_journal_entry()])
    monkeypatch.setattr(
        certificate_module,
        "project_journal_exposure",
        lambda *_args, **_kwargs: (
            JournalExposure(
                account_id=receipt.account_id,
                group_by="namespace",
                group_id="learn-ai/spy-a/v1",
                symbol="SPY",
                quantity=1.0,
            ),
        ),
    )
    append_account_event(
        tmp_path,
        receipt.account_id,
        {"event_type": "account_freeze_recorded", "reason": "test", "recorded_at_ms": now_ms_utc()},
    )

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "failed"
    assert "FAILED_NAMESPACE_EXPOSURE_NONZERO" in certificate.reasons
    assert "FAILED_INCIDENT_RECORDED" in certificate.reasons


async def test_certificate_api_reads_the_immutable_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app
    from app.routers import cohort_batch_launch

    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [_journal_entry("exec-entry"), _journal_entry("exec-exit")],
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)
    app.dependency_overrides[cohort_batch_launch.get_cohort_validation_certificate_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                f"/api/accounts/{receipt.account_id}/cohort-batch-launches/{receipt.cohort_id}/certificate"
            )
            fetched = await client.get(
                f"/api/accounts/{receipt.account_id}/cohort-batch-launches/{receipt.cohort_id}/certificate"
            )
            duplicate = await client.post(
                f"/api/accounts/{receipt.account_id}/cohort-batch-launches/{receipt.cohort_id}/certificate"
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert duplicate.status_code == 409
