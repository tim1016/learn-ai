"""Proof seams for #1019's append-only legacy sidecar retirement cure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import ACCOUNT_EVENTS_FILENAME, read_account_events
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import AccountInstanceBinding
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo, stable_live_state_path
from app.schemas.account_reconciliation import LegacyStaleClaimRetirementReceipt
from app.schemas.account_truth import AccountTruthResponse, AccountTruthSourceFreshness
from app.schemas.live_runs import HostRunnerProcessStatus
from app.services.fleet_contamination import collect_fleet_position_explanations
from app.services.legacy_stale_claim_retirement import (
    LEGACY_STALE_CLAIM_RETIRED_EVENT,
    LegacyStaleClaimRetirementError,
    LegacyStaleClaimRetirementService,
    _record_retired_claim_receipt,
    retired_legacy_claim_keys,
)
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_account_binding,
    write_historical_clerk_journal,
)

_ACCOUNT_ID = "DUM284968"
_RUN_ID = "legacy-run"
_SID = "legacy-spy"
_NAMESPACE = "learn-ai/legacy-spy/v1"
_NOW_MS = 1_780_000_002_000


def _truth() -> AccountTruthResponse:
    """A clean, positions-empty, freshly-verified Account Truth fixture.

    Constructed directly against the schema rather than via the retired
    ``app.broker.ibkr.account_truth.compose_account_truth`` (IBKR account
    decommission, PR-A of #1813) — every test in this file only exercises
    ``_validate_account_truth``'s account-id and positions-freshness checks,
    never the verdict/blocker composition that function used to own.
    """
    return AccountTruthResponse(
        account_id=_ACCOUNT_ID,
        final_verdict="clean",
        final_severity="ok",
        status_label="Clean",
        status_detail="No blockers.",
        generated_at_ms=_NOW_MS,
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
        invariants=[],
        positions=[],
        source_freshness=[
            AccountTruthSourceFreshness(
                source="positions",
                label="Positions",
                status="fresh",
                severity="ok",
                fetched_at_ms=_NOW_MS,
                hard_ttl_ms=60_000,
                message="Positions evidence is fresh.",
            ),
        ],
    )


def _seed_claim(
    root: Path,
    *,
    strategy_instance_id: str = _SID,
    run_id: str = _RUN_ID,
    symbol: str = "SPY",
    binding_state: str | None = "RETIRED",
) -> None:
    namespace = f"learn-ai/{strategy_instance_id}/v1"
    ledger_path = root / "live_runs" / run_id / "run_ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "strategy_instance_id": strategy_instance_id,
                "account_id": _ACCOUNT_ID,
                "created_at_ms": _NOW_MS - 10_000,
            }
        ),
        encoding="utf-8",
    )
    LiveStateSidecarRepo(stable_live_state_path(root, strategy_instance_id), trusted_root=root / "live_state").write(
        LiveStateEnvelope(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            bot_order_namespace=namespace,
            ib_client_id=7,
            expected_position_by_symbol={symbol: 1},
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
    )
    if binding_state is not None:
        write_historical_account_binding(
            root,
            AccountInstanceBinding(
                account_id=_ACCOUNT_ID,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                bot_order_namespace=namespace,
                lifecycle_state=binding_state,  # type: ignore[arg-type]
                recorded_at_ms=_NOW_MS - 5_000,
                source="test",
            ),
        )


async def _dead_process(_run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(state="exited", run_id=_run_id)


async def _live_process(_run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(state="running", run_id=_run_id)


async def _idle_process(_run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(state="idle", run_id=_run_id)


async def _mismatched_exited_process(_run_id: str) -> tuple[DaemonResult, HostRunnerProcessStatus]:
    return DaemonResult.connected(), HostRunnerProcessStatus(state="exited", run_id="another-run")


async def test_retire_refuses_live_process(tmp_path: Path) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    with pytest.raises(LegacyStaleClaimRetirementError, match="LEGACY_CLAIM_RUN_PROCESS_LIVE"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            symbol="SPY",
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_live_process,
        )


async def test_post_clerk_retired_sidecar_claim_still_has_a_retirement_remedy(
    tmp_path: Path,
) -> None:
    journal_started_at_ms = _NOW_MS - 20_000
    intent = AccountOwnerSubmitIntent(
        trace_id="journal-before-sidecar",
        account_id=_ACCOUNT_ID,
        strategy_instance_id="earlier-bot",
        run_id="earlier-run",
        bot_order_namespace="learn-ai/earlier-bot/v1",
        intent_id="journal-before-sidecar",
        order_ref="learn-ai/earlier-bot/v1:journal-before-sidecar",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=journal_started_at_ms,
    )
    write_historical_clerk_journal(
        tmp_path,
        _ACCOUNT_ID,
        [
            AccountClerkJournalEntry(
                seq=1,
                entry_kind="recorded",
                recorded_at_ms=journal_started_at_ms,
                intent=intent,
            )
        ],
    )
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(
        artifacts_root=tmp_path,
        now_ms=lambda: _NOW_MS,
    )

    candidates = await service.candidates(
        account_id=_ACCOUNT_ID,
        account_truth=_truth(),
        fetch_run_process=_dead_process,
    )

    assert [(candidate.strategy_instance_id, candidate.symbol) for candidate in candidates] == [(_SID, "SPY")]


@pytest.mark.parametrize(
    ("fetch_run_process", "reason_code"),
    [
        (_idle_process, "LEGACY_CLAIM_RUN_PROCESS_LIVE"),
        (_mismatched_exited_process, "LEGACY_CLAIM_RUN_PROCESS_UNPROVEN"),
    ],
)
async def test_retire_requires_terminal_process_proof_for_the_matching_run(
    tmp_path: Path,
    fetch_run_process: object,
    reason_code: str,
) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    with pytest.raises(LegacyStaleClaimRetirementError, match=reason_code):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            symbol="SPY",
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=fetch_run_process,
        )


async def test_retire_refuses_active_binding(tmp_path: Path) -> None:
    _seed_claim(tmp_path, binding_state="ACTIVE")
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    with pytest.raises(LegacyStaleClaimRetirementError, match="LEGACY_CLAIM_BINDING_ACTIVE"):
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            symbol="SPY",
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_dead_process,
        )


async def test_retire_records_receipt_and_excludes_only_retired_legacy_claim(tmp_path: Path) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    receipt = await service.retire(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        symbol="SPY",
        requested_by="test.operator",
        account_truth=_truth(),
        fetch_run_process=_dead_process,
    )

    assert receipt.symbol == "SPY"
    events = read_account_events(tmp_path, _ACCOUNT_ID)
    event = next(event for event in events if event["event_type"] == LEGACY_STALE_CLAIM_RETIRED_EVENT)
    assert event["receipt_id"] == receipt.receipt_id
    assert event["ts_ms"] == _NOW_MS
    assert collect_fleet_position_explanations(tmp_path / "live_runs") == {}


async def test_retire_allows_a_claim_that_differs_from_a_prior_receipt_only_by_namespace(
    tmp_path: Path,
) -> None:
    _seed_claim(tmp_path)
    assert _record_retired_claim_receipt(
        tmp_path,
        LegacyStaleClaimRetirementReceipt(
            receipt_id="legacy-retirement-other-namespace",
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=_RUN_ID,
            bot_order_namespace="learn-ai/other-namespace/v1",
            symbol="SPY",
            claimed_quantity=1,
            requested_by="test.operator",
            retired_at_ms=_NOW_MS - 1,
        ),
    )
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    receipt = await service.retire(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=_RUN_ID,
        symbol="SPY",
        requested_by="test.operator",
        account_truth=_truth(),
        fetch_run_process=_dead_process,
    )

    assert receipt.bot_order_namespace == _NAMESPACE


async def test_retire_rejects_a_concurrent_duplicate_receipt(tmp_path: Path) -> None:
    _seed_claim(tmp_path)
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    both_proofs_started = asyncio.Event()
    release_proofs = asyncio.Event()
    proof_calls = 0

    async def simultaneous_terminal_proof(
        run_id: str,
    ) -> tuple[DaemonResult, HostRunnerProcessStatus]:
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls == 2:
            both_proofs_started.set()
        await release_proofs.wait()
        return await _dead_process(run_id)

    retire_kwargs = {
        "account_id": _ACCOUNT_ID,
        "strategy_instance_id": _SID,
        "run_id": _RUN_ID,
        "symbol": "SPY",
        "requested_by": "test.operator",
        "account_truth": _truth(),
        "fetch_run_process": simultaneous_terminal_proof,
    }
    first = asyncio.create_task(service.retire(**retire_kwargs))
    second = asyncio.create_task(service.retire(**retire_kwargs))
    await both_proofs_started.wait()
    release_proofs.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(result, LegacyStaleClaimRetirementReceipt) for result in results) == 1
    assert sum(isinstance(result, LegacyStaleClaimRetirementError) for result in results) == 1
    error = next(result for result in results if isinstance(result, LegacyStaleClaimRetirementError))
    assert error.reason_code == "LEGACY_CLAIM_ALREADY_RETIRED"
    receipts = [
        event
        for event in read_account_events(tmp_path, _ACCOUNT_ID)
        if event["event_type"] == LEGACY_STALE_CLAIM_RETIRED_EVENT
    ]
    assert len(receipts) == 1


async def test_retiring_four_dum284968_shaped_legacy_claims_clears_flat_broker_contamination(
    tmp_path: Path,
) -> None:
    """Regression for the four protected DUM284968 acceptance-fixture claims."""

    claims = (
        ("depval-spy-jul8", "DEPVALSPYJUL8", "SPY"),
        ("june-25-spy", "JUNE-25", "SPY"),
        ("depval-tsla-jul7", "tsladepvaljul7", "TSLA"),
        ("jun26-tsla", "JUN26TSLA", "TSLA"),
    )
    for strategy_instance_id, run_id, symbol in claims:
        _seed_claim(
            tmp_path,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            symbol=symbol,
        )
    service = LegacyStaleClaimRetirementService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    before = compute_fleet_contamination(
        {"SPY": 0, "TSLA": 0},
        collect_fleet_position_explanations(tmp_path / "live_runs"),
    )
    assert before["residual"] == {"SPY": -2, "TSLA": -2}

    for strategy_instance_id, run_id, symbol in claims:
        await service.retire(
            account_id=_ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            symbol=symbol,
            requested_by="test.operator",
            account_truth=_truth(),
            fetch_run_process=_dead_process,
        )

    after = compute_fleet_contamination(
        {"SPY": 0, "TSLA": 0},
        collect_fleet_position_explanations(tmp_path / "live_runs"),
    )
    assert after["verdict"] == "clean"
    assert after["residual"] == {}


def test_legacy_retirement_receipts_are_snapshotted_once(tmp_path: Path) -> None:
    legacy_path = tmp_path / "accounts" / _ACCOUNT_ID / ACCOUNT_EVENTS_FILENAME
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "event_type": LEGACY_STALE_CLAIM_RETIRED_EVENT,
                "strategy_instance_id": _SID,
                "run_id": _RUN_ID,
                "symbol": "spy",
                "bot_order_namespace": _NAMESPACE,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    first = retired_legacy_claim_keys(tmp_path, _ACCOUNT_ID)
    legacy_path.write_text("{truncated", encoding="utf-8")
    second = retired_legacy_claim_keys(tmp_path, _ACCOUNT_ID)

    assert first == {(_SID, _RUN_ID, "SPY", _NAMESPACE)}
    assert second == first
