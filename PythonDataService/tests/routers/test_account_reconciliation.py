"""Router tests for account reconciliation read endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrOrderEvent,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    AccountClerkLease,
    AccountFreezeEvidence,
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    account_artifacts_root,
    advance_account_clerk_generation,
    append_account_event,
    read_account_events,
    read_account_freeze,
    record_cohort_batch_launch_outcomes,
    record_cohort_batch_launch_receipt,
    write_account_clerk_lease,
    write_account_freeze,
)
from app.engine.live.account_clerk_journal import (
    AccountClerkBrokerAckReceipt,
    AccountClerkJournal,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
)
from app.engine.live.account_clerk_rpc import AccountClerkRpcRejectedError, AccountClerkRpcRequestIdentity
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
    write_account_instance_binding,
)
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo, stable_live_state_path
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.routers import account_reconciliation, cohort_batch_launch
from app.schemas.journal_cures import JournalCureReceipt, JournalCureRequest
from app.services import cohort_batch_launch as cohort_batch_launch_service
from app.services.account_directory import AccountDirectoryService, CurrentBrokerAccount
from app.services.account_event_journal import AccountEventJournalService
from app.services.account_reconciliation import AccountReconciliationService
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.cohort_evidence import CohortEvidenceSample, CohortMemberSample
from app.services.legacy_stale_claim_retirement import LegacyStaleClaimRetirementService
from app.utils.timestamps import now_ms_utc


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _account_summary() -> IbkrAccountSummary:
    return IbkrAccountSummary(
        account_id="DU1234567",
        is_paper=True,
        base_currency="USD",
        net_liquidation=100_000.0,
        buying_power=50_000.0,
        fetched_at_ms=1_780_000_000_000,
    )


def _positions_snapshot() -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id="DU1234567",
        is_paper=True,
        positions=[],
        fetched_at_ms=1_780_000_000_400,
    )


def _truth():
    return compose_account_truth(
        health=_health(),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear("DU1234567"),
        account=_account_summary(),
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )


def _seed_legacy_claim(root: Path, *, binding_state: str = "RETIRED") -> None:
    run_id = "legacy-run"
    sid = "legacy-spy"
    namespace = "learn-ai/legacy-spy/v1"
    write_ledger(
        root / "live_runs" / run_id / "run_ledger.json",
        LiveRunLedger(
            run_id=run_id,
            code_sha="a" * 40,
            strategy_instance_id=sid,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="b" * 64,
            qc_audit_copy_path="audit.py",
            qc_audit_copy_sha256="c" * 64,
            qc_cloud_backtest_id="qc-1",
            account_id="DU1234567",
            start_date_ms=1_780_000_000_000,
            live_config={},
            created_at_ms=1_780_000_000_000,
        ),
    )
    LiveStateSidecarRepo(
        stable_live_state_path(root, sid), trusted_root=root / "live_state"
    ).write(
        LiveStateEnvelope(
            strategy_instance_id=sid,
            run_id=run_id,
            bot_order_namespace=namespace,
            ib_client_id=7,
            expected_position_by_symbol={"SPY": 1},
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
    )
    write_account_instance_binding(
        root,
        AccountInstanceBinding(
            account_id="DU1234567",
            strategy_instance_id=sid,
            run_id=run_id,
            bot_order_namespace=namespace,
            lifecycle_state=binding_state,  # type: ignore[arg-type]
            recorded_at_ms=1_780_000_001_000,
            source="test",
        ),
    )


async def _dead_run_process(_base_url: str, _run_id: str) -> tuple[DaemonResult, dict]:
    return DaemonResult.connected(), {"state": "exited", "run_id": _run_id}


async def _live_run_process(_base_url: str, _run_id: str) -> tuple[DaemonResult, dict]:
    return DaemonResult.connected(), {"state": "running", "run_id": _run_id}


async def test_latest_reconciliation_returns_404_when_missing(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/reconciliation/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_reconciliation_bootstraps_clerk_before_refreshing_account_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    calls: list[str] = []

    async def fake_ensure(_base_url: str, account_id: str) -> dict:
        assert account_id == "DU1234567"
        calls.append("ensure")
        return {}

    async def fake_refresh(*_args, account_id: str, **kwargs):
        assert account_id == "DU1234567"
        assert kwargs["account_truth_observer"] == service.observe_account_truth
        assert kwargs["account_truth_failure_observer"] == service.observe_account_truth_failure
        calls.append("refresh")
        return _truth()

    monkeypatch.setattr(account_reconciliation.host_daemon_client, "ensure_account_clerk", fake_ensure)
    monkeypatch.setattr(account_reconciliation, "refresh_account_truth_now", fake_refresh)
    monkeypatch.setattr(
        account_reconciliation,
        "get_settings",
        lambda: SimpleNamespace(live_runner_daemon_url="http://daemon"),
    )
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/accounts/DU1234567/reconciliation")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["account_id"] == "DU1234567"
    assert calls == ["ensure", "refresh"]


async def test_legacy_stale_claim_route_returns_only_proven_candidates_and_receipts_retirement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.main import app

    _seed_legacy_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_002_000)
    app.dependency_overrides[
        account_reconciliation.get_legacy_stale_claim_retirement_service
    ] = lambda: service
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    monkeypatch.setattr(account_reconciliation, "refresh_account_truth_now", lambda *_args, **_kwargs: _async_truth())
    monkeypatch.setattr(
        account_reconciliation,
        "get_settings",
        lambda: SimpleNamespace(live_runner_daemon_url="http://daemon"),
    )
    monkeypatch.setattr(account_reconciliation.host_daemon_client, "fetch_run_process", _dead_run_process)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            candidates = await client.get("/api/accounts/DU1234567/legacy-stale-claims/candidates")
            retired = await client.post(
                "/api/accounts/DU1234567/legacy-stale-claims/retire",
                json={
                    "strategy_instance_id": "legacy-spy",
                    "run_id": "legacy-run",
                    "symbol": "SPY",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert candidates.status_code == 200
    assert candidates.json()["candidates"][0]["strategy_instance_id"] == "legacy-spy"
    assert retired.status_code == 200
    assert retired.json()["symbol"] == "SPY"


async def test_legacy_stale_claim_route_refuses_live_process_with_specific_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.main import app

    _seed_legacy_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path)
    app.dependency_overrides[
        account_reconciliation.get_legacy_stale_claim_retirement_service
    ] = lambda: service
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    monkeypatch.setattr(account_reconciliation, "refresh_account_truth_now", lambda *_args, **_kwargs: _async_truth())
    monkeypatch.setattr(
        account_reconciliation,
        "get_settings",
        lambda: SimpleNamespace(live_runner_daemon_url="http://daemon"),
    )
    monkeypatch.setattr(account_reconciliation.host_daemon_client, "fetch_run_process", _live_run_process)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/legacy-stale-claims/retire",
                json={
                    "strategy_instance_id": "legacy-spy",
                    "run_id": "legacy-run",
                    "symbol": "SPY",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "LEGACY_CLAIM_RUN_PROCESS_LIVE"


async def _async_truth():
    return _truth()


async def test_latest_cohort_status_reloads_exact_persisted_blocker(tmp_path: Path) -> None:
    from app.main import app

    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="opening-batch-1",
        member_strategy_instance_ids=("spy-a", "spy-b"),
        window_start_ms=1_780_000_000_000,
        window_end_ms=1_780_000_030_000,
        authorized_by="local-operator",
        recorded_at_ms=1_780_000_000_000,
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    record_cohort_batch_launch_outcomes(
        tmp_path,
        CohortBatchLaunchOutcomesReceipt(
            account_id="DU1234567",
            cohort_id="opening-batch-1",
            outcomes=(
                CohortBatchLaunchMemberOutcome(
                    strategy_instance_id="spy-a",
                    state="accepted",
                    reason="COHORT_START_ACCEPTED",
                    next_safe_action="Monitor receipt state.",
                ),
                CohortBatchLaunchMemberOutcome(
                    strategy_instance_id="spy-b",
                    state="blocked",
                    reason="ACCOUNT_FROZEN",
                    next_safe_action="Clear the account freeze.",
                ),
            ),
            recorded_at_ms=1_780_000_000_001,
        ),
    )
    service = CohortBatchLaunchService(artifacts_root=tmp_path)
    app.dependency_overrides[
        cohort_batch_launch.get_cohort_batch_launch_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/accounts/DU1234567/cohort-batch-launches/latest",
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["cohort_id"] == "opening-batch-1"
    assert body["outcomes_state"] == "recorded"
    assert body["outcomes"][1] == {
        "strategy_instance_id": "spy-b",
        "state": "blocked",
        "reason": "ACCOUNT_FROZEN",
        "next_safe_action": "Clear the account freeze.",
    }


async def test_latest_cohort_status_fails_closed_for_unreadable_newest_authorization(
    monkeypatch,
) -> None:
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="valid-earlier-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=1_780_000_000_000,
        window_end_ms=1_780_000_030_000,
        authorized_by="local-operator",
        recorded_at_ms=1_780_000_000_000,
    )
    events = [
        {
            "event_type": "cohort_batch_launch_authorized",
            "seq": 1,
            **receipt.model_dump(mode="json"),
        },
        {
            "event_type": "cohort_batch_launch_authorized",
            "cohort_id": "unreadable-newest-cohort",
            "seq": 2,
        },
    ]
    monkeypatch.setattr(cohort_batch_launch_service, "read_account_events", lambda *_args: events)
    service = CohortBatchLaunchService(artifacts_root=Path("/unused"))

    try:
        await service.get_status(account_id="DU1234567", cohort_id=None)
    except ValueError as exc:
        assert str(exc) == "cohort authorization is unreadable: unreadable-newest-cohort"
    else:
        raise AssertionError("latest cohort retrieval must not skip unreadable authorization")


async def test_latest_cohort_status_projects_durable_server_evidence(tmp_path: Path) -> None:
    now_ms = now_ms_utc()
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="evidence-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=now_ms,
        window_end_ms=now_ms + 30_000,
        authorized_by="local-operator",
        recorded_at_ms=now_ms,
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    service = CohortBatchLaunchService(artifacts_root=tmp_path)
    await service.record_evidence_sample(
        account_id=receipt.account_id,
        cohort_id=receipt.cohort_id,
        sample=CohortEvidenceSample(
            expected_at_ms=now_ms,
            observed_at_ms=now_ms,
                account_truth="healthy",
                fleet="healthy",
                members=(CohortMemberSample("spy-a", "run-spy-a", "healthy", orders_used=0, orders_cap=4),),
                broker_net_positions={},
                broker_residual={},
        ),
    )

    status = await service.get_status(account_id=receipt.account_id, cohort_id=receipt.cohort_id)

    assert status is not None
    assert status.evidence.model_dump() == {
        "sample_count": 1,
        "cadence_ms": 5_000,
        "healthy_overlap_ms": 5_000,
        "verdict": "healthy",
        "reason": None,
        "source": "account_event.cohort_evidence_sample",
        "members": [
            {
                "strategy_instance_id": "spy-a",
                "run_id": "run-spy-a",
                "verdict": "healthy",
                "reason": None,
                "orders_used": 0,
                "orders_cap": 4,
            }
        ],
    }
    evidence_event = next(
        event for event in read_account_events(tmp_path, receipt.account_id)
        if event["event_type"] == "cohort_evidence_sample"
    )
    assert evidence_event["broker_net_positions"] == {}
    assert evidence_event["broker_residual"] == {}


async def test_malformed_cohort_evidence_fails_closed(tmp_path: Path) -> None:
    now_ms = now_ms_utc()
    receipt = CohortBatchLaunchReceipt(
        account_id="DU1234567",
        cohort_id="malformed-evidence-cohort",
        member_strategy_instance_ids=("spy-a",),
        window_start_ms=now_ms,
        window_end_ms=now_ms + 30_000,
        authorized_by="local-operator",
        recorded_at_ms=now_ms,
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    append_account_event(
        tmp_path,
        receipt.account_id,
        {
            "event_type": "cohort_evidence_sample",
            "cohort_id": receipt.cohort_id,
            "expected_at_ms": now_ms,
            "observed_at_ms": now_ms,
            "account_truth": "healthy",
            "fleet": "healthy",
            "members": "not-a-list",
        },
    )

    status = await CohortBatchLaunchService(artifacts_root=tmp_path).get_status(
        account_id=receipt.account_id,
        cohort_id=receipt.cohort_id,
    )

    assert status is not None
    assert status.evidence.verdict == "failed"
    assert status.evidence.reason == "COHORT_EVIDENCE_UNREADABLE"


async def test_triage_returns_latest_receipt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "DU1234567"
    assert body["account_reconciliation_receipt"]["receipt_id"] == receipt.receipt_id
    assert body["overall_gate_result"]["status"] == "pass"
    assert body["verdict"] == {
        "state": "CLEAN",
        "headline": "Account is clean",
        "detail": "The current reconciliation proof and account checks are passing.",
        "primary_move": None,
        "operator_attention_count": 0,
    }
    assert body["conditions"] == []
    assert body["reconciliation_automation_policy"]["enabled"] is False
    assert body["account_reconciliation_valid_until_ms"] == receipt.expires_at_ms


async def test_update_reconciliation_automation_policy(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path)
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/accounts/du1234567/reconciliation/automation",
                json={"enabled": True, "updated_by": "test.operator"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "account_id": "DU1234567",
        "enabled": True,
        "updated_at_ms": response.json()["updated_at_ms"],
        "updated_by": "test.operator",
    }
    assert service.read_automation_policy("DU1234567").enabled is True


async def test_triage_returns_unknown_when_registry_is_corrupt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    account_root = account_artifacts_root(tmp_path, "DU1234567")
    account_root.mkdir(parents=True, exist_ok=True)
    (account_root / "instance_registry.jsonl").write_text("{not-json\n", encoding="utf-8")
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["overall_gate_result"]["status"] == "unknown"
    assert {row["gate_id"] for row in body["gate_rows"]} == {
        "account.reconciliation",
        "account.instance_registry",
    }


async def test_latest_reconciliation_normalizes_account_id(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/du1234567/reconciliation/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "DU1234567"
    assert body["receipt_id"] == receipt.receipt_id


async def test_reconciliation_service_honors_artifact_root_dependency_override(tmp_path: Path) -> None:
    """Default service construction must use the route's injected artifact root."""

    from app.main import app

    receipt = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000).write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/reconciliation/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["receipt_id"] == receipt.receipt_id


async def test_triage_contract_returns_condition_rows_for_active_freeze(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["overall_gate_result"]["status"] == "freeze"
    assert body["clear_freeze_actionable"] is False
    assert body["conditions"] == [
        {
            "condition_type": "exposure_freeze",
            "scope": "account",
            "owner": {
                "owner_type": "account",
                "owner_id": "DU1234567",
                "label": "Account DU1234567",
                "strategy_instance_id": None,
                "run_id": None,
                "lifecycle_state": None,
            },
            "severity": "critical",
            "title": "Account freeze active",
            "detail": "watchdog.flatten_timed_out",
            "operator_next_step": "CHECK_IBKR",
            "source": "watchdog_halt_executor",
            "evidence_at_ms": 1_780_000_002_500,
            "evidence_refs": [],
            "affected_strategy_instance_ids": [],
            "cure_action": "resolve_exposure",
        }
    ]


async def test_clear_freeze_endpoint_refuses_stale_pre_freeze_receipt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/clear",
                json={"requested_by": "operator", "receipt_id": receipt.receipt_id},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "newer than the active freeze" in response.json()["detail"]
    assert read_account_freeze(tmp_path, "DU1234567") is not None


async def test_clear_freeze_endpoint_returns_refreshed_triage_after_success(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/clear",
                json={"requested_by": "operator", "receipt_id": receipt.receipt_id},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["receipt_id"] == receipt.receipt_id
    assert body["triage"]["overall_gate_result"]["status"] == "pass"
    assert body["triage"]["conditions"] == []
    assert read_account_freeze(tmp_path, "DU1234567") is None


async def test_accept_exposure_override_endpoint_returns_refreshed_triage(
    tmp_path: Path,
) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/accept-exposure-override",
                json={
                    "requested_by": "operator",
                    "reason": "Operator accepts the current account exposure.",
                    "strategy_instance_id": "bot-a",
                    "run_id": "run-a",
                    "bot_order_namespace": "learn-ai/bot-a/v1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["cleared_source"] == "account_audited_override"
    assert body["triage"]["overall_gate_result"]["status"] == "unknown"
    assert read_account_freeze(tmp_path, "DU1234567") is None


async def test_false_crash_backfill_endpoint_repairs_disproven_registry_row(
    tmp_path: Path,
) -> None:
    from app.main import app

    active = AccountInstanceBinding(
        account_id="DU1234567",
        strategy_instance_id="bot-a",
        run_id="run-halt",
        bot_order_namespace="learn-ai/bot-a/v1",
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_780_000_001_000,
        source="host_daemon.start",
    )
    retired = active.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": 1_780_000_002_000,
            "source": "host_daemon.process_crashed",
        }
    )
    write_account_instance_binding(tmp_path, active)
    write_account_instance_binding(tmp_path, retired)
    run_dir = tmp_path / "live_runs" / "run-halt"
    run_dir.mkdir(parents=True)
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-halt",
                "started_at_ms": 1_780_000_001_000,
                "last_update_ms": 1_780_000_002_000,
                "ended_at_ms": 1_780_000_002_000,
                "exit_code": 1,
                "exit_reason": "fatal_halt",
            }
        ),
        encoding="utf-8",
    )
    app.dependency_overrides[
        account_reconciliation.get_account_artifacts_root
    ] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/registry/backfill-false-crashes"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["rows_repaired"] == 1
    assert body["repaired_run_ids"] == ["run-halt"]
    repaired = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, "DU1234567"),
        account_id="DU1234567",
        strategy_instance_id="bot-a",
    )
    assert repaired is not None
    assert repaired.source == "host_daemon.process_halted"


async def test_journal_cure_endpoint_ensures_clerk_before_appending_adjustment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.main import app

    namespace = "learn-ai/bot-a/v1"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-cure-route",
        account_id="DU1234567",
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=namespace,
        intent_id="intent-cure-route",
        order_ref=f"{namespace}:intent-cure-route",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=100,
    )
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id="DU1234567", now_ms=lambda: 100)
    journal.record_intent(intent, validate_intent=lambda _: None)
    journal.record_broker_event(
        IbkrOrderEvent(
            account_id="DU1234567",
            order_id=1,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="cure-route-fill",
            ts_ms=100,
        )
    )
    ensured: list[str] = []

    async def _ensure_clerk(_base_url: str, account_id: str) -> dict:
        ensured.append(account_id)
        return {}

    class FakeRpcClient:
        def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
            assert artifacts_root == tmp_path
            assert account_id == "DU1234567"

        async def apply_operator_adjustment(self, request: JournalCureRequest) -> JournalCureReceipt:
            assert request.idempotency_key == "cure-route-1"
            return JournalCureReceipt(
                account_id="DU1234567",
                bot_order_namespace=namespace,
                symbol="SPY",
                signed_quantity=-1,
                request_provenance=request.request_provenance,
                reason=request.reason,
                evidence_refs=request.evidence_refs,
                idempotency_key=request.idempotency_key,
                recorded_at_ms=101,
                journal_seq=3,
            )

    monkeypatch.setattr(account_reconciliation.host_daemon_client, "ensure_account_clerk", _ensure_clerk)
    monkeypatch.setattr(account_reconciliation, "AccountClerkRpcClient", FakeRpcClient)
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/journal-cures",
                json={
                    "bot_order_namespace": namespace,
                    "symbol": "SPY",
                    "signed_quantity": -1,
                    "reason": "operator verified stale claim",
                    "evidence_refs": ["account-reconciliation:receipt-1"],
                    "request_provenance": "account-monitor/cure",
                    "idempotency_key": "cure-route-1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert ensured == ["DU1234567"]
    assert response.json()["signed_quantity"] == -1


async def test_journal_cure_endpoint_preserves_clerk_rejection_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An operator needs the Clerk's actionable cure rejection, not a generic code."""

    from app.main import app

    async def _ensure_clerk(_base_url: str, _account_id: str) -> dict:
        return {}

    class FakeRpcClient:
        def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
            assert artifacts_root == tmp_path
            assert account_id == "DU1234567"

        async def apply_operator_adjustment(self, _request: JournalCureRequest) -> JournalCureReceipt:
            raise AccountClerkRpcRejectedError(
                reason="JOURNAL_CURE_IDEMPOTENCY_CONFLICT",
                operation="operator_adjustment",
                request_identity=AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None),
            )

    monkeypatch.setattr(account_reconciliation.host_daemon_client, "ensure_account_clerk", _ensure_clerk)
    monkeypatch.setattr(account_reconciliation, "AccountClerkRpcClient", FakeRpcClient)
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/journal-cures",
                json={
                    "bot_order_namespace": "learn-ai/bot-a/v1",
                    "symbol": "SPY",
                    "signed_quantity": -1,
                    "reason": "operator verified stale claim",
                    "evidence_refs": ["account-reconciliation:receipt-1"],
                    "request_provenance": "account-monitor/cure",
                    "idempotency_key": "cure-route-1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "JOURNAL_CURE_IDEMPOTENCY_CONFLICT"


async def test_journal_cure_preview_honors_artifact_root_dependency_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview must read the test/app-provided artifact root, never global settings."""

    from app.main import app

    observed_roots: list[Path] = []

    class FakeJournalCureService:
        def __init__(self, *, artifacts_root: Path) -> None:
            observed_roots.append(artifacts_root)

        def preview(
            self,
            *,
            account_id: str,
            bot_order_namespace: str,
            symbol: str,
        ) -> account_reconciliation.JournalCurePreview:
            return account_reconciliation.JournalCurePreview(
                account_id=account_id,
                bot_order_namespace=bot_order_namespace,
                symbol=symbol,
                journal_quantity=0.0,
                can_cure=False,
                reason_code="JOURNAL_CURE_NO_STALE_CLAIM",
            )

    monkeypatch.setattr(account_reconciliation, "JournalCureService", FakeJournalCureService)
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/accounts/DU1234567/journal-cures/preview",
                params={"bot_order_namespace": "learn-ai/retired-bot/v1", "symbol": "SPY"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert observed_roots == [tmp_path]


async def test_operator_recovery_flatten_endpoint_ensures_clerk_and_appends_audit_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.main import app

    namespace = "learn-ai/retired-bot/v1"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-operator-recovery",
        account_id="DU1234567",
        strategy_instance_id="retired-bot",
        run_id="run-retired",
        bot_order_namespace=namespace,
        intent_id="intent-operator-recovery",
        order_ref=f"{namespace}:intent-operator-recovery",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=2,
        created_at_ms=100,
    )
    recorded = AccountClerkRecordedReceipt(
        trace_id=intent.trace_id,
        account_id=intent.account_id,
        strategy_instance_id=intent.strategy_instance_id,
        run_id=intent.run_id,
        bot_order_namespace=intent.bot_order_namespace,
        intent_id=intent.intent_id,
        order_ref=intent.order_ref,
        journal_seq=1,
        recorded_at_ms=100,
    )
    receipt = AccountClerkRecoveryFlattenReceipt(
        recorded=recorded,
        broker_acked=AccountClerkBrokerAckReceipt(
            **recorded.model_dump(exclude={"status", "recorded_at_ms"}),
            order_id=12,
            recorded_at_ms=101,
        ),
        cancelled_order_ids=(11,),
    )
    ensured: list[str] = []

    async def _ensure_clerk(_base_url: str, account_id: str) -> dict:
        ensured.append(account_id)
        return {}

    class FakeRpcClient:
        def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
            assert artifacts_root == tmp_path
            assert account_id == "DU1234567"

        async def submit_operator_recovery_flatten(
            self,
            submitted_intent: AccountOwnerSubmitIntent,
        ) -> AccountClerkRecoveryFlattenReceipt:
            assert submitted_intent == intent
            return receipt

    monkeypatch.setattr(account_reconciliation.host_daemon_client, "ensure_account_clerk", _ensure_clerk)
    monkeypatch.setattr(account_reconciliation, "AccountClerkRpcClient", FakeRpcClient)
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/operator-recovery-flatten",
                json={"intent": intent.model_dump(mode="json"), "request_provenance": "account-monitor/recovery"},
            )
            replay = await client.post(
                "/api/accounts/DU1234567/operator-recovery-flatten",
                json={"intent": intent.model_dump(mode="json"), "request_provenance": "account-monitor/recovery"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert replay.status_code == 200
    assert ensured == ["DU1234567", "DU1234567"]
    [event] = read_account_events(tmp_path, "DU1234567")
    assert {
        key: event[key]
        for key in (
            "account_id",
            "event_type",
            "intent_id",
            "order_ref",
            "request_provenance",
            "recorded_at_ms",
            "receipt_id",
        )
    } == {
        "account_id": "DU1234567",
        "event_type": "account_clerk_operator_recovery_flatten",
        "intent_id": intent.intent_id,
        "order_ref": intent.order_ref,
        "request_provenance": "account-monitor/recovery",
        "recorded_at_ms": 101,
        "receipt_id": "account-clerk-operator-recovery:intent-operator-recovery:1",
    }


async def test_account_events_endpoint_pages_filters_and_preserves_stable_event_identity(
    tmp_path: Path,
) -> None:
    from app.main import app

    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_owner_generation_recorded", "recorded_at_ms": 1_710_000_000_000},
    )
    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_freeze_recorded", "receipt_id": "freeze-receipt", "recorded_at_ms": 1_710_000_001_000},
    )
    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_reconciliation_receipt_recorded", "recorded_at_ms": 1_710_000_002_000},
    )
    service = AccountEventJournalService(artifacts_root=tmp_path, now_ms=lambda: 1_710_000_003_000)
    app.dependency_overrides[account_reconciliation.get_account_event_journal_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first_page = await client.get("/api/accounts/DU1234567/events?limit=2")
            older_page = await client.get("/api/accounts/DU1234567/events?before_seq=2")
            newer_page = await client.get("/api/accounts/DU1234567/events?after_seq=1")
            safety = await client.get("/api/accounts/DU1234567/events?kinds=safety")
    finally:
        app.dependency_overrides.clear()

    assert first_page.status_code == 200
    assert first_page.json() == {
        "schema_version": 1,
        "account_id": "DU1234567",
        "view": "operations",
        "rows": [
            {
                "schema_version": 1,
                "event_id": "DU1234567:3",
                "seq": 3,
                "kind": "reconciliation",
                "occurred_at_ms": 1_710_000_002_000,
                "trader_narration": "Account reconciliation was recorded.",
                "operator_detail": "Account reconciliation receipt recorded in the journal.",
                "evidence_refs": [{"source": "account_event_journal", "ref": "DU1234567:3", "detail": None}],
            },
            {
                "schema_version": 1,
                "event_id": "DU1234567:2",
                "seq": 2,
                "kind": "safety",
                "occurred_at_ms": 1_710_000_001_000,
                "trader_narration": "An account safety freeze was recorded.",
                "operator_detail": "Account safety freeze recorded in the journal.",
                "evidence_refs": [
                    {"source": "account_event_journal", "ref": "DU1234567:2", "detail": None},
                    {"source": "receipt", "ref": "freeze-receipt", "detail": None},
                ],
            },
        ],
        "latest_seq": 3,
        "next_before_seq": 2,
    }
    assert [row["seq"] for row in older_page.json()["rows"]] == [1]
    assert [row["seq"] for row in newer_page.json()["rows"]] == [3, 2]
    assert [row["seq"] for row in safety.json()["rows"]] == [2]
    assert len({row["event_id"] for row in first_page.json()["rows"]}) == 2


async def test_account_events_endpoint_rejects_invalid_cursors_and_limits(tmp_path: Path) -> None:
    from app.main import app

    service = AccountEventJournalService(artifacts_root=tmp_path)
    app.dependency_overrides[account_reconciliation.get_account_event_journal_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            both_cursors = await client.get("/api/accounts/DU1234567/events?before_seq=2&after_seq=1")
            invalid_limit = await client.get("/api/accounts/DU1234567/events?limit=0")
    finally:
        app.dependency_overrides.clear()

    assert both_cursors.status_code == 422
    assert both_cursors.json()["detail"]["reason_code"] == "ACCOUNT_EVENTS_CURSOR_EXCLUSIVE"
    assert invalid_limit.status_code == 422


async def test_account_events_endpoint_returns_empty_for_missing_journal_and_typed_error_for_corruption(
    tmp_path: Path,
) -> None:
    from app.main import app

    service = AccountEventJournalService(artifacts_root=tmp_path)
    app.dependency_overrides[account_reconciliation.get_account_event_journal_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            empty = await client.get("/api/accounts/DU1234567/events")
            journal = tmp_path / "accounts" / "DU1234567" / "account_events.jsonl"
            journal.parent.mkdir(parents=True)
            journal.write_text("{malformed json\n", encoding="utf-8")
            corrupt = await client.get("/api/accounts/DU1234567/events")
    finally:
        app.dependency_overrides.clear()

    assert empty.status_code == 200
    assert empty.json()["rows"] == []
    assert empty.json()["latest_seq"] is None
    assert corrupt.status_code == 409
    assert corrupt.json()["detail"]["reason_code"] == "ACCOUNT_EVENTS_JOURNAL_CORRUPT"
    assert journal.read_text(encoding="utf-8") == "{malformed json\n"


async def test_account_events_endpoint_trader_today_uses_new_york_day_across_dst(tmp_path: Path) -> None:
    from app.main import app

    ny = ZoneInfo("America/New_York")
    yesterday = int(datetime(2024, 3, 9, 23, 30, tzinfo=ny).timestamp() * 1_000)
    today = int(datetime(2024, 3, 10, 0, 30, tzinfo=ny).timestamp() * 1_000)
    now = int(datetime(2024, 3, 10, 12, 0, tzinfo=ny).timestamp() * 1_000)
    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_freeze_recorded", "recorded_at_ms": yesterday},
    )
    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_freeze_cleared", "recorded_at_ms": today},
    )
    append_account_event(
        tmp_path,
        "DU1234567",
        {"event_type": "account_clerk_generation_recorded", "recorded_at_ms": today},
    )
    service = AccountEventJournalService(artifacts_root=tmp_path, now_ms=lambda: now)
    app.dependency_overrides[account_reconciliation.get_account_event_journal_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/events?view=trader_today")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    [row] = response.json()["rows"]
    assert row["seq"] == 2
    assert row["occurred_at_ms"] == today
    assert isinstance(row["occurred_at_ms"], int)
    assert row["trader_narration"] == "An account safety freeze was cleared."


async def test_accounts_roster_exposes_the_current_single_account_without_a_service(tmp_path: Path) -> None:
    from app.main import app

    service = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id="DU1234567", is_paper=True),
        now_ms=lambda: 1_780_000_000_000,
    )
    expected_triage = AccountReconciliationService(artifacts_root=tmp_path).triage(
        account_id="DU1234567",
        now_ms=1_780_000_000_000,
    )
    app.dependency_overrides[account_reconciliation.get_account_directory_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            roster = await client.get("/api/accounts")
            status_response = await client.get("/api/accounts/DU1234567/clerk")
    finally:
        app.dependency_overrides.clear()

    assert roster.status_code == 200
    assert roster.json() == {
        "schema_version": 1,
        "rows": [
            {
                "account_id": "DU1234567",
                "broker": "IBKR",
                "effective_posture": "PAPER_EXECUTION",
                "service": {"attachment": "UNATTACHED", "phase": None, "generation": None},
                "latest_verdict_summary": {
                    "state": expected_triage.verdict.state,
                    "headline": expected_triage.verdict.headline,
                    "generated_at_ms": 1_780_000_000_000,
                },
                "last_verified_at_ms": None,
            }
        ],
    }
    assert status_response.status_code == 200
    assert status_response.json() == {
        "schema_version": 1,
        "account_id": "DU1234567",
        "attachment": "UNATTACHED",
        "phase": None,
        "generation": None,
        "generation_recorded_at_ms": None,
        "source": None,
        "binding": {"state": "UNATTACHED", "generation": None, "lease_generation": None},
        "lease": None,
        "journal": {"last_seq": None, "last_write_ms": None},
    }
    assert not (tmp_path / "accounts" / "DU1234567").exists()


async def test_account_service_status_endpoint_projects_durable_service_evidence(tmp_path: Path) -> None:
    from app.main import app

    account_id = "DU7654321"
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=account_id,
            strategy_instance_id="desk-roster",
            run_id="run-roster",
            bot_order_namespace="learn-ai/desk-roster/v1",
            lifecycle_state="ACTIVE",
            recorded_at_ms=1_780_000_000_000,
            source="test",
        ),
    )
    generation = advance_account_clerk_generation(
        tmp_path,
        account_id,
        phase="accepting",
        recorded_at_ms=1_780_000_000_100,
        source="host_daemon.clerk_spawn",
    )
    write_account_clerk_lease(
        tmp_path,
        AccountClerkLease(
            account_id=account_id,
            generation=generation.generation,
            pid=123,
            ibkr_client_id=80,
            status="RUNNING",
            started_at_ms=1_780_000_000_101,
            renewed_at_ms=1_780_000_000_102,
            valid_until_ms=1_780_000_060_102,
        ),
    )
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-roster",
        account_id=account_id,
        strategy_instance_id="desk-roster",
        run_id="run-roster",
        bot_order_namespace="learn-ai/desk-roster/v1",
        intent_id="intent-roster",
        order_ref="learn-ai/desk-roster/v1:intent-roster",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=1_780_000_000_103,
    )
    AccountClerkJournal(
        artifacts_root=tmp_path,
        account_id=account_id,
        now_ms=lambda: 1_780_000_000_104,
    ).record_intent(intent, validate_intent=lambda _: None)
    service = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=None,
        now_ms=lambda: 1_780_000_000_200,
    )
    app.dependency_overrides[account_reconciliation.get_account_directory_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/accounts/{account_id}/clerk")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "account_id": account_id,
        "attachment": "ATTACHED",
        "phase": "accepting",
        "generation": 1,
        "generation_recorded_at_ms": 1_780_000_000_100,
        "source": "host_daemon.clerk_spawn",
        "binding": {"state": "ATTACHED", "generation": 1, "lease_generation": 1},
        "lease": {
            "status": "RUNNING",
            "generation": 1,
            "started_at_ms": 1_780_000_000_101,
            "renewed_at_ms": 1_780_000_000_102,
            "valid_until_ms": 1_780_000_060_102,
        },
        "journal": {"last_seq": 1, "last_write_ms": 1_780_000_000_104},
    }


async def test_account_service_status_endpoint_rejects_unknown_and_corrupt_artifacts_without_repair(
    tmp_path: Path,
) -> None:
    from app.main import app

    service = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id="DU1234567", is_paper=True),
    )
    app.dependency_overrides[account_reconciliation.get_account_directory_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unknown = await client.get("/api/accounts/DU7654321/clerk")
            artifact = tmp_path / "accounts" / "DU1234567" / "clerk_generation.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{not valid json", encoding="utf-8")
            corrupt = await client.get("/api/accounts/DU1234567/clerk")
    finally:
        app.dependency_overrides.clear()

    assert unknown.status_code == 404
    assert unknown.json()["detail"]["reason_code"] == "ACCOUNT_UNKNOWN"
    assert corrupt.status_code == 409
    assert corrupt.json()["detail"]["reason_code"] == "ACCOUNT_SERVICE_ARTIFACT_CORRUPT"
    assert artifact.read_text(encoding="utf-8") == "{not valid json"
