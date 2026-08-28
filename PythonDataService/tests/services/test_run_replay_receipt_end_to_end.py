"""End-to-end: retained bars + real decision receipts -> classified parity receipt."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.engine.strategy.signal_program import Settlement, trace_root
from app.services.bot_binding_repository import BotRunOutcomeRecord, BotRunRecord
from app.services.bot_trade_strategy import strategy_evaluations
from app.services.run_replay_proof import (
    RunReplayProofService,
    live_run_decision_evidence_from_rows,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.ema_parity import _ema_parity_bars_through_first_exit
from tests.services.test_candidate_uncaptured_at_crash import _binding, _PhaseFeed


async def _run_and_receipt_live_pass(receipts: SqliteDecisionReceipts, *, block_first_enter: bool) -> None:
    """Drive the shared seam exactly as run_trade_bot would and durably receipt it,
    including the Task 5b live-time trace-digest capture."""
    binding = _binding(run_id="run-1")
    blocked_once = False
    async for evaluation in strategy_evaluations(binding, _PhaseFeed(live_bars=_ema_parity_bars_through_first_exit())):
        staged = evaluation.intents[0].kind.value if evaluation.intents else None
        facts = {
            "bar_ref": f"decision-bar:fake-phase:SPY:{evaluation.decision_bar_close_ms}",
            "decision_id": evaluation.evaluation_id,
            "evaluation_id": evaluation.evaluation_id,
            "run_id": "run-1",
            "decision_bar_close_ms": evaluation.decision_bar_close_ms,
        }
        facts["trace_digest"] = trace_root([evaluation.trace])
        if staged is None:
            receipts.append(outcome="no_action", symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": "NO_ACTION"}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.COMMIT)
        elif block_first_enter and staged == "ENTER" and not blocked_once:
            blocked_once = True
            receipts.append(outcome="blocked", symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": "MARKET_CLOSED"}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.DISCARD)
        else:
            outcome = "enter_intent" if staged == "ENTER" else "exit_intent"
            receipts.append(outcome=outcome, symbol="SPY", observed_at_ms=evaluation.decision_bar_close_ms,
                            facts={**facts, "reason_code": outcome}, intent_id=evaluation.evaluation_id)
            evaluation.settle_stage(Settlement.COMMIT)


def _service(tmp_path: Path, receipts: SqliteDecisionReceipts) -> RunReplayProofService:
    async def _records_for_run(binding, run_id: str):
        return live_run_decision_evidence_from_rows(receipts.retained_window(), run_id)

    bars = _ema_parity_bars_through_first_exit()
    return RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: _binding(run_id="run-1"),
        run_record_for=lambda sid, run_id: BotRunRecord(
            run_id="run-1", strategy_instance_id=_SID, configuration_hash="0" * 64,
            launch_reason="deploy", started_at_ms=bars[0].start_ms - 1,
        ),
        is_running=lambda sid: False,
        # Wall-clock end bound: the run terminated right after its last bar.
        run_outcome_for=lambda sid, run_id: BotRunOutcomeRecord(
            strategy_instance_id=_SID, run_id="run-1", kind="STOPPED",
            reason_code="OPERATOR_STOP", recorded_at_ms=bars[-1].end_ms,
        ),
        records_for_run=_records_for_run,
    )


@pytest.fixture
def receipts(tmp_path: Path) -> SqliteDecisionReceipts:
    repo = ClerkSqliteRepository.initialize(account_id="PA-E2E", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    return SqliteDecisionReceipts(repo, strategy_instance_id=_SID)


def _retain_all(tmp_path: Path) -> None:
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        for bar in _ema_parity_bars_through_first_exit():
            ledger.append(bar)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_end_to_end_faithful_run_yields_full_parity(tmp_path: Path, receipts: SqliteDecisionReceipts) -> None:
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=False)

    receipt = await _service(tmp_path, receipts).generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity"
    assert receipt.drift_count == 0
    assert receipt.expected_live_effect_count == 0
    assert receipt.live_compared_count > 0
    assert receipt.digest_verified_count == receipt.live_compared_count  # content-verified end to end
    assert receipt.engine_parity_trace_root is not None
    assert receipt.bar_set_digest != ""


@pytest.mark.asyncio
async def test_end_to_end_blocked_enter_is_classified_not_reported_as_drift(
    tmp_path: Path, receipts: SqliteDecisionReceipts
) -> None:
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=True)

    receipt = await _service(tmp_path, receipts).generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity_with_expected_live_effects"
    assert receipt.drift_count == 0
    assert receipt.expected_live_effect_count >= 1
    assert any(
        d.classification == "expected_live_effect" and d.reason_code == "MARKET_CLOSED"
        for d in receipt.divergences
    )


@pytest.mark.asyncio
async def test_end_to_end_regenerating_run_one_after_later_appends_is_stable(
    tmp_path: Path, receipts: SqliteDecisionReceipts
) -> None:
    """PR #1751 finding 4: run N's receipt must not change when run N+1 has
    appended more bars to the same instance-scoped ledger."""
    _retain_all(tmp_path)
    await _run_and_receipt_live_pass(receipts, block_first_enter=False)
    service = _service(tmp_path, receipts)

    first = await service.generate("alpaca", _SID, "run-1")

    # Simulate run N+1: append later bars beyond run-1's terminal instant.
    bars = _ema_parity_bars_through_first_exit()
    ledger = SourceBarLedger(
        artifacts_root=tmp_path / "artifacts",
        account_id=paper_evidence_account_id_for_strategy(_SID),
    )
    try:
        last = bars[-1]
        for offset in range(1, 4):
            ledger.append(
                last.model_copy(
                    update={
                        "start_ms": last.start_ms + offset * 60_000,
                        "end_ms": last.end_ms + offset * 60_000,
                        "fetched_at_ms": last.fetched_at_ms + offset * 60_000,
                    }
                )
            )
    finally:
        ledger.close()

    second = await service.generate("alpaca", _SID, "run-1")

    assert second.bar_set_digest == first.bar_set_digest
    assert second.retained_bar_count == first.retained_bar_count
    assert second.ledger_end_seq == first.ledger_end_seq
    assert second.status == first.status == "parity"
    assert second.engine_parity_trace_root == first.engine_parity_trace_root
