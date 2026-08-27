"""Orchestrated receipt generation over injected evidence (no runner needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.services.bot_binding_repository import BotRunOutcomeRecord, BotRunRecord
from app.services.run_replay_proof import (
    LiveRunDecisionEvidence,
    RunReplayProofService,
    RunReplayUnavailableError,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding
from tests.services.test_run_replay_fidelity import _record_live_pass


def _run_record(started_at_ms: int) -> BotRunRecord:
    return BotRunRecord(
        run_id="run-1",
        strategy_instance_id=_SID,
        configuration_hash="0" * 64,
        launch_reason="deploy",
        started_at_ms=started_at_ms,
    )


def _outcome(recorded_at_ms: int) -> BotRunOutcomeRecord:
    return BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=recorded_at_ms,
    )


def _service(tmp_path: Path, evidence: LiveRunDecisionEvidence, *, running: bool = False,
             record: BotRunRecord | None = None,
             outcome: BotRunOutcomeRecord | None = None) -> RunReplayProofService:
    async def _records_for_run(binding, run_id: str) -> LiveRunDecisionEvidence:
        del binding, run_id
        return evidence

    return RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: _binding(run_id="run-1"),
        run_record_for=lambda sid, run_id: record,
        is_running=lambda sid: running,
        run_outcome_for=lambda sid, run_id: outcome,
        records_for_run=_records_for_run,
    )


@pytest.mark.asyncio
async def test_generate_produces_a_parity_receipt_for_a_faithful_run(tmp_path: Path) -> None:
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    evidence = LiveRunDecisionEvidence(
        records=tuple(records), crash_records=(), captured_decisions={}, truncated=False
    )
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(
        tmp_path,
        evidence,
        record=_run_record(bars[0].start_ms - 1),
        outcome=_outcome(bars[-1].end_ms),  # wall-clock end bound: run ended after the last bar
    )

    receipt = await service.generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity"
    assert receipt.drift_count == 0
    assert receipt.retained_bar_count == len(bars)
    assert receipt.ledger_end_seq == len(bars)  # the applied bound is disclosed for stable regeneration
    assert receipt.digest_verified_count == len(records)
    assert receipt.engine_parity_trace_root is not None
    assert receipt.live_compared_count == len(records) > 0
    assert service.read(_SID, "run-1") == receipt  # durably persisted


@pytest.mark.asyncio
async def test_generate_refuses_the_currently_live_run(tmp_path: Path) -> None:
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    service = _service(tmp_path, evidence, running=True, record=_run_record(0))

    with pytest.raises(RunReplayUnavailableError) as excinfo:
        await service.generate("alpaca", _SID, "run-1")
    assert excinfo.value.http_status == 409


@pytest.mark.asyncio
async def test_generate_without_launch_evidence_is_a_404(tmp_path: Path) -> None:
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    service = _service(tmp_path, evidence, record=None)

    with pytest.raises(RunReplayUnavailableError) as excinfo:
        await service.generate("alpaca", _SID, "run-1")
    assert excinfo.value.http_status == 404


@pytest.mark.asyncio
async def test_generate_with_truncated_evidence_is_indeterminate_never_parity(tmp_path: Path) -> None:
    """PR #1751 finding 6: known-incomplete decision history must not prove parity."""
    bars = _ema_parity_bars_through_first_exit()
    records = await _record_live_pass(bars, block_first_enter=False)
    evidence = LiveRunDecisionEvidence(
        records=tuple(records), crash_records=(), captured_decisions={}, truncated=True
    )
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(
        tmp_path, evidence,
        record=_run_record(bars[0].start_ms - 1),
        outcome=_outcome(bars[-1].end_ms),
    )

    receipt = await service.generate("alpaca", _SID, "run-1")

    assert receipt.status == "indeterminate"
    assert receipt.records_truncated is True


@pytest.mark.asyncio
async def test_generate_without_any_end_bound_refuses(tmp_path: Path) -> None:
    """PR #1751 finding 4: an unbounded replay input is not evidence."""
    bars = _ema_parity_bars_through_first_exit()
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    for bar in bars:
        ledger.append(bar)
    ledger.close()
    service = _service(tmp_path, evidence, record=_run_record(bars[0].start_ms - 1), outcome=None)

    with pytest.raises(RunReplayUnavailableError):
        await service.generate("alpaca", _SID, "run-1")
