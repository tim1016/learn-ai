"""Router tests for account reconciliation read endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    account_artifacts_root,
    append_account_event,
    read_account_freeze,
    record_cohort_batch_launch_outcomes,
    record_cohort_batch_launch_receipt,
    write_account_freeze,
)
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
from app.services import cohort_batch_launch as cohort_batch_launch_service
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
