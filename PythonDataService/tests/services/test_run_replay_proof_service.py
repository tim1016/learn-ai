"""Orchestrated receipt generation over injected evidence (no runner needed)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.account_authority import paper_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.services.bot_binding_repository import BotRunOutcomeRecord, BotRunRecord
from app.services.decision_session import RunDecisionSession
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
             outcome: BotRunOutcomeRecord | None = None,
             use_rth: bool = True) -> RunReplayProofService:
    async def _records_for_run(binding, run_id: str) -> LiveRunDecisionEvidence:
        del binding, run_id
        return evidence

    return RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: _binding(run_id="run-1").model_copy(update={"use_rth": use_rth}),
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
        ledger.append(bar, run_id="run-a")
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
async def test_generate_refuses_an_extended_run_that_recorded_no_decision_session(
    tmp_path: Path,
) -> None:
    """Task 4 review finding 1, re-anchored on run evidence (Codex round 1, P2-c).

    A use_rth=False run whose window cannot be proven must refuse replay
    loudly, instead of silently replaying zero decidable bars into a receipt
    that looks like a proven parity result. What proves it is the run's own
    recorded session -- never the live authority, which may have changed or
    may not be active at all.
    """
    evidence = LiveRunDecisionEvidence(records=(), crash_records=(), captured_decisions={}, truncated=False)
    service = _service(tmp_path, evidence, record=_run_record(0), use_rth=False)

    with pytest.raises(RunReplayUnavailableError, match="no decision session") as excinfo:
        await service.generate("alpaca", _SID, "run-1")
    assert excinfo.value.http_status == 503


@pytest.mark.asyncio
async def test_an_extended_replay_reads_the_runs_own_session_not_the_live_authority(
    tmp_path: Path,
) -> None:
    """The run's recorded window decides the replay, whatever is active now.

    Two failures the live read had (Codex round 1, P2-c): on-demand
    generation and boot repair 503'd whenever no authority happened to be
    active, and a capability change after the fact filtered the same retained
    bars differently -- a replay that no longer proves what the run did. Both
    are driven here: the authority declares nothing at all, and then declares
    a window narrow enough to exclude every decided bar.
    """
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
        ledger.append(bar, run_id="run-a")
    ledger.record_decision_session(
        RunDecisionSession(kind="extended", window=ALPACA_EXTENDED_HOURS_WINDOW),
        run_id="run-1",
        recorded_at_ms=bars[0].start_ms,
    )
    ledger.close()
    service = _service(
        tmp_path,
        evidence,
        record=_run_record(bars[0].start_ms - 1),
        outcome=_outcome(bars[-1].end_ms),
        use_rth=False,
    )

    # No authority active at all -- the live read refused this with a 503.
    set_active_clerk_runtime(None)
    receipt = await service.generate("alpaca", _SID, "run-1")

    assert receipt.status == "parity"
    assert receipt.retained_bar_count == len(bars)
    assert receipt.live_compared_count == len(records) > 0

    # A real active authority now declares a window one minute wide, which
    # would have matched no decided bar. Installed for real rather than
    # patched at an import site, so any reintroduced live read sees it
    # however it imports the accessor.
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite",
            clerk=SimpleNamespace(  # type: ignore[arg-type]
                program_leg_policy=ProgramLegPolicy(
                    window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=4 * 60 + 1),
                    allowances=None,
                )
            ),
        )
    )
    try:
        regenerated = await service.generate("alpaca", _SID, "run-1")
    finally:
        set_active_clerk_runtime(None)

    # `generated_at_ms` moves with the clock; everything the receipt proves
    # is derived from the bars the recorded session decided on.
    assert regenerated.model_dump(exclude={"generated_at_ms"}) == receipt.model_dump(
        exclude={"generated_at_ms"}
    )


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
        ledger.append(bar, run_id="run-a")
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
        ledger.append(bar, run_id="run-a")
    ledger.close()
    service = _service(tmp_path, evidence, record=_run_record(bars[0].start_ms - 1), outcome=None)

    with pytest.raises(RunReplayUnavailableError):
        await service.generate("alpaca", _SID, "run-1")


def test_ledger_namespace_follows_the_sealed_binding_custody_world() -> None:
    from app.services.run_replay_proof import ledger_account_id_for

    trade = _binding(run_id="run-1")

    assert (
        ledger_account_id_for(trade.model_copy(update={"sealed_account_id": "shadow:9LIVE0001"}))
        == f"shadow-evidence:{_SID}"
    )
    assert (
        ledger_account_id_for(trade.model_copy(update={"sealed_account_id": "PA-TEST"}))
        == f"paper:{_SID}"
    )
    with pytest.raises(RunReplayUnavailableError):
        ledger_account_id_for(trade.model_copy(update={"mode": "log_only"}))
