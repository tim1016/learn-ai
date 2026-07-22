"""Regression coverage for host-owned stale deployment binding retirement."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrAccountSummary, IbkrConnectionHealth, IbkrOpenOrder, IbkrPositionsSnapshot
from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_registry import AccountInstanceBinding, write_account_instance_binding
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.order_identity import build_order_ref
from app.schemas.account_truth import AccountTruthResponse
from app.schemas.live_runs import HostRunnerProcessStatus
from app.services.stale_binding_retirement import (
    StaleBindingRetirementError,
    StaleBindingRetirementService,
)

_ACCOUNT_ID = "DUM284968"
_SID = "audit-dep-only-0717"
_RUN_ID = "run-audit-dep-only-0717"
_NAMESPACE = "learn-ai/audit-dep-only-0717/v1"
_NOW_MS = 1_780_000_002_000


def _truth(*, open_orders: list[IbkrOpenOrder] | None = None) -> AccountTruthResponse:
    return compose_account_truth(
        health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=7,
            connected=True,
            account_id=_ACCOUNT_ID,
            is_paper=True,
            fetched_at_ms=_NOW_MS,
            connection_state="connected",
            last_transition_ms=_NOW_MS,
        ),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(_ACCOUNT_ID),
        account=IbkrAccountSummary(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            base_currency="USD",
            fetched_at_ms=_NOW_MS,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            positions=[],
            fetched_at_ms=_NOW_MS,
        ),
        open_orders=[] if open_orders is None else open_orders,
        completed_orders=[],
        executions=[],
        generated_at_ms=_NOW_MS,
    )


def _working_order(*, order_ref: str) -> IbkrOpenOrder:
    return IbkrOpenOrder(
        account_id=_ACCOUNT_ID,
        order_id=42,
        perm_id=9001,
        client_id=7,
        con_id=12345,
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=1.0,
        order_type="MKT",
        time_in_force="DAY",
        status="Submitted",
        remaining=1.0,
        order_ref=order_ref,
        fetched_at_ms=_NOW_MS,
    )


def _seed_binding(root: Path) -> AccountInstanceBinding:
    binding = AccountInstanceBinding(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        bot_order_namespace=_NAMESPACE,
        lifecycle_state="DEPLOYED",
        recorded_at_ms=_NOW_MS - 1,
        source="deploy.strategy",
    )
    write_account_instance_binding(root, binding)
    return binding


async def _exited_process(run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(run_id=run_id, state="exited")


async def _live_process(run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(run_id=run_id, state="running")


async def test_candidates_require_fresh_broker_flat_and_terminal_host_process(tmp_path: Path) -> None:
    _seed_binding(tmp_path)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    candidates = await service.candidates(
        account_id=_ACCOUNT_ID,
        account_truth=_truth(),
        fetch_run_process=_exited_process,
    )

    assert [(candidate.strategy_instance_id, candidate.run_id) for candidate in candidates] == [
        (_SID, _RUN_ID)
    ]
    assert candidates[0].proof_summary == "STALE_BINDING_BROKER_FLAT_AND_PROCESS_EXITED"

    no_candidates = await service.candidates(
        account_id=_ACCOUNT_ID,
        account_truth=_truth(),
        fetch_run_process=_live_process,
    )

    assert no_candidates == []


async def test_candidates_reject_missing_or_stale_open_order_proof(tmp_path: Path) -> None:
    _seed_binding(tmp_path)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    stale_truth = _truth().model_copy(
        update={
            "source_freshness": [
                source.model_copy(update={"status": "stale"})
                if source.source == "open_orders"
                else source
                for source in _truth().source_freshness
            ]
        }
    )

    with pytest.raises(StaleBindingRetirementError, match="STALE_BINDING_BROKER_OPEN_ORDERS_UNPROVEN"):
        await service.candidates(
            account_id=_ACCOUNT_ID,
            account_truth=stale_truth,
            fetch_run_process=_exited_process,
        )


async def test_candidates_and_retirement_reject_a_working_order_in_binding_namespace(tmp_path: Path) -> None:
    binding = _seed_binding(tmp_path)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    truth = _truth(
        open_orders=[
            _working_order(order_ref=build_order_ref(binding.bot_order_namespace, "intent-still-live"))
        ]
    )

    candidates = await service.candidates(
        account_id=_ACCOUNT_ID,
        account_truth=truth,
        fetch_run_process=_exited_process,
    )

    assert candidates == []

    async def should_not_retire(
        _account_id: str,
        _strategy_instance_id: str,
        _run_id: str,
    ) -> AccountInstanceBinding:
        raise AssertionError("a working order must block retirement before host mutation")

    with pytest.raises(StaleBindingRetirementError, match="STALE_BINDING_BROKER_OPEN_ORDER_LIVE"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            requested_by="account-desk.operator",
            account_truth=truth,
            fetch_run_process=_exited_process,
            retire_binding=should_not_retire,
        )


async def test_retire_reproves_then_requires_host_retirement_receipt(tmp_path: Path) -> None:
    binding = _seed_binding(tmp_path)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    retired = binding.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": _NOW_MS,
            "source": "operator.stale_binding_retirement",
        }
    )
    calls: list[tuple[str, str, str]] = []

    async def retire_binding(
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
    ) -> AccountInstanceBinding:
        calls.append((account_id, strategy_instance_id, run_id))
        return retired

    receipt = await service.retire(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        requested_by="account-desk.operator",
        account_truth=_truth(),
        fetch_run_process=_exited_process,
        retire_binding=retire_binding,
    )

    assert calls == [(_ACCOUNT_ID, _SID, _RUN_ID)]
    assert receipt.source == "operator.stale_binding_retirement"
    [event] = [
        event
        for event in read_account_events(tmp_path, _ACCOUNT_ID)
        if event["event_type"] == "account_stale_binding_retired"
    ]
    assert event["event_type"] == "account_stale_binding_retired"
    assert event["receipt_id"] == receipt.receipt_id


async def test_retire_rejects_a_mismatched_host_retirement_receipt(tmp_path: Path) -> None:
    binding = _seed_binding(tmp_path)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    async def retire_binding(
        _account_id: str,
        _sid: str,
        _run_id: str,
    ) -> AccountInstanceBinding:
        return binding.model_copy(
            update={
                "lifecycle_state": "RETIRED",
                "run_id": "another-run",
                "source": "operator.stale_binding_retirement",
            }
        )

    with pytest.raises(StaleBindingRetirementError, match="STALE_BINDING_RETIRE_RESPONSE_INVALID"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            requested_by="account-desk.operator",
            account_truth=_truth(),
            fetch_run_process=_exited_process,
            retire_binding=retire_binding,
        )


async def test_retire_recovers_the_receipt_after_an_ambiguous_host_response(tmp_path: Path) -> None:
    binding = _seed_binding(tmp_path)
    retired = binding.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": _NOW_MS,
            "source": "operator.stale_binding_retirement",
        }
    )
    write_account_instance_binding(tmp_path, retired)
    service = StaleBindingRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    async def should_not_call_host(
        _account_id: str,
        _sid: str,
        _run_id: str,
    ) -> AccountInstanceBinding:
        raise AssertionError("a durable host retirement must not be repeated")

    receipt = await service.retire(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        requested_by="account-desk.operator",
        account_truth=_truth(),
        fetch_run_process=_exited_process,
        retire_binding=should_not_call_host,
    )

    assert receipt.retired_at_ms == _NOW_MS
    assert receipt.source == "operator.stale_binding_retirement"
