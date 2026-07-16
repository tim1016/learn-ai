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
from app.services import cohort_batch_launch as cohort_batch_launch_module
from app.services import cohort_validation_certificate as certificate_module
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.cohort_evidence import CohortEvidenceSample, CohortMemberSample
from app.services.cohort_validation_certificate import (
    CohortValidationCertificateService,
    CohortValidationCertificateWindowOpenError,
)
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
    return CohortValidationCertificateService(
        artifacts_root=root,
        now_ms=lambda: now_ms + 30_000,
    ), receipt


def _journal_entry(
    exec_id: str = "exec-a",
    *,
    run_id: str = "run-a",
    symbol: str = "SPY",
) -> SimpleNamespace:
    intent = SimpleNamespace(
        strategy_instance_id="spy-a",
        run_id=run_id,
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
        symbol=symbol,
    )


def _fill_event(entry: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        event_type="fill",
        side="BUY" if "entry" in entry.exec_id else "SELL",
        fill_quantity=1.0,
        symbol=entry.symbol,
        order_id=entry.order_id,
        perm_id=entry.perm_id,
        exec_id=entry.exec_id,
    )


def _two_closed_round_trips(*, run_id: str = "run-a") -> list[SimpleNamespace]:
    return [
        _journal_entry("exec-entry-1", run_id=run_id),
        _journal_entry("exec-exit-1", run_id=run_id),
        _journal_entry("exec-entry-2", run_id=run_id),
        _journal_entry("exec-exit-2", run_id=run_id),
    ]


async def test_certificate_is_deterministic_and_refuses_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    first = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)
    second = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.verdict == "passed"
    assert first.round_trips[0].exec_ids == ["exec-entry-1", "exec-entry-2", "exec-exit-1", "exec-exit-2"]
    assert first.round_trips[0].round_trip_count == 2
    fsynced_paths: list[Path] = []
    monkeypatch.setattr(certificate_module, "_fsync_parent_dir", fsynced_paths.append)
    path = service.write_once(first)
    assert fsynced_paths == [path]
    assert service.read(account_id=receipt.account_id, cohort_id=receipt.cohort_id) == first
    with pytest.raises(FileExistsError):
        service.write_once(second)


async def test_certificate_projects_status_from_its_loaded_events_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    def unexpected_second_event_read(*_args: object) -> list[dict]:
        raise AssertionError("certificate status must reuse its original account-events snapshot")

    monkeypatch.setattr(cohort_batch_launch_module, "read_account_events", unexpected_second_event_read)

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.cohort_id == receipt.cohort_id


async def test_certificate_passes_for_a_complete_window_at_production_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusive window endpoint does not invent a missing final sample."""

    start_ms = 1_780_000_000_000
    end_ms = start_ms + 30_000
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="completed-certificate-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        authorized_by="operator.alice",
        recorded_at_ms=start_ms,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="spy-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    launch_service = CohortBatchLaunchService(artifacts_root=tmp_path)
    for expected_at_ms in range(start_ms, end_ms, 5_000):
        await launch_service.record_evidence_sample(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            sample=CohortEvidenceSample(
                expected_at_ms=expected_at_ms,
                observed_at_ms=expected_at_ms,
                account_truth="healthy",
                fleet="healthy",
                members=(CohortMemberSample("spy-a", "run-a", "healthy", orders_used=1, orders_cap=4),),
                broker_net_positions={},
                broker_residual={},
            ),
        )
    monkeypatch.setattr(cohort_batch_launch_module, "now_ms_utc", lambda: end_ms + 1)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)
    certificate = await CohortValidationCertificateService(
        artifacts_root=tmp_path,
        now_ms=lambda: end_ms + 1,
    ).generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "passed"
    assert certificate.evidence_reason is None


async def test_certificate_is_incomplete_without_linked_round_trip_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(certificate_module, "read_account_clerk_journal", lambda *_args: [])
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "incomplete"
    assert "INCOMPLETE_ROUND_TRIP_IDENTITY" in certificate.reasons
    assert "INCOMPLETE_MEMBER_NAMESPACE_IDENTITY" in certificate.reasons


async def test_certificate_requires_two_closed_round_trips_per_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [_journal_entry("exec-entry-1"), _journal_entry("exec-exit-1")],
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "incomplete"
    assert certificate.round_trips[0].round_trip_count == 1
    assert "INCOMPLETE_ROUND_TRIP_COUNT" in certificate.reasons


async def test_certificate_counts_multi_leg_flattening_as_one_bot_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [
            _journal_entry("exec-entry-spy", symbol="SPY"),
            _journal_entry("exec-entry-qqq", symbol="QQQ"),
            _journal_entry("exec-exit-spy", symbol="SPY"),
            _journal_entry("exec-exit-qqq", symbol="QQQ"),
        ],
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.verdict == "incomplete"
    assert certificate.round_trips[0].round_trip_count == 1
    assert "INCOMPLETE_ROUND_TRIP_COUNT" in certificate.reasons


async def test_certificate_refuses_generation_before_its_validation_window_ends(tmp_path: Path) -> None:
    """A one-time certificate cannot be written from an unfinished window."""

    _completed_service, receipt = await _seed_evidence(tmp_path)
    service = CohortValidationCertificateService(artifacts_root=tmp_path, now_ms=lambda: receipt.window_start_ms)

    with pytest.raises(CohortValidationCertificateWindowOpenError):
        await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)


async def test_certificate_round_trips_exclude_prior_runs_in_the_same_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: [
            _journal_entry("old-entry", run_id="old-run"),
            _journal_entry("old-exit", run_id="old-run"),
            *_two_closed_round_trips(),
        ],
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.round_trips[0].exec_ids == ["exec-entry-1", "exec-entry-2", "exec-exit-1", "exec-exit-2"]


async def test_certificate_preserves_every_final_exposure_symbol_per_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
    )
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)
    monkeypatch.setattr(
        certificate_module,
        "project_journal_exposure",
        lambda *_args, **_kwargs: (
            JournalExposure(receipt.account_id, "namespace", "learn-ai/spy-a/v1", "SPY", 1.0),
            JournalExposure(receipt.account_id, "namespace", "learn-ai/spy-a/v1", "QQQ", -2.0),
        ),
    )

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert certificate.final_journal_exposure == {"learn-ai/spy-a/v1": {"SPY": 1.0, "QQQ": -2.0}}


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
    service = CohortValidationCertificateService(
        artifacts_root=tmp_path,
        now_ms=lambda: now_ms + 30_000,
    )
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
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


async def test_certificate_ignores_incidents_after_its_validation_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
    )
    monkeypatch.setattr(certificate_module, "project_journal_exposure", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(certificate_module, "normalize_journal_broker_event", _fill_event)
    append_account_event(
        tmp_path,
        receipt.account_id,
        {
            "event_type": "account_freeze_recorded",
            "reason": "unrelated-later-incident",
            "recorded_at_ms": receipt.window_end_ms + 1,
        },
    )

    certificate = await service.generate(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert "FAILED_INCIDENT_RECORDED" not in certificate.reasons


async def test_certificate_api_reads_the_immutable_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app
    from app.routers import cohort_batch_launch

    service, receipt = await _seed_evidence(tmp_path)
    monkeypatch.setattr(
        certificate_module,
        "read_account_clerk_journal",
        lambda *_args: _two_closed_round_trips(),
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
            malformed_account = await client.get(
                f"/api/accounts/not%20an%20account/cohort-batch-launches/{receipt.cohort_id}/certificate"
            )
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert duplicate.status_code == 409
    assert malformed_account.status_code == 400
