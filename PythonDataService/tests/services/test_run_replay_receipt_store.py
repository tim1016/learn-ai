"""Durable per-run replay receipts under live_state/<sid>/run_replay_receipts/."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.marketdata.feed import FeedContinuityEvent
from app.schemas.run_replay import RunReplayReceipt
from app.services.run_replay_proof import (
    RunReplayProofService,
    ledger_account_id_for,
    read_run_replay_receipt,
    write_run_replay_receipt,
)
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_candidate_uncaptured_at_crash import _binding
from tests.services.test_run_replay_proof_assembly import _market_bar


def _receipt(*, status: str = "pending", run_id: str = "run-1") -> RunReplayReceipt:
    return RunReplayReceipt(
        strategy_instance_id="bot-a",
        run_id=run_id,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        provider="feed-a",
        status=status,
        bar_set_digest="0" * 64,
        retained_bar_count=0,
        ledger_end_seq=None,
        engine_parity_trace_root=None,
        engine_parity_compared_count=0,
        engine_parity_divergence=None,
        live_compared_count=0,
        match_count=0,
        expected_live_effect_count=0,
        drift_count=0,
        digest_verified_count=0,
        records_truncated=False,
        divergences=[],
        program_version=None,
        sealed_program_hash=None,
        generated_at_ms=1_700_000_000_000,
    )


def test_write_then_read_round_trips_and_pending_is_replaceable(tmp_path: Path) -> None:
    instance_dir = tmp_path / "live_state" / "bot-a"

    path = write_run_replay_receipt(instance_dir, _receipt(status="pending"))
    assert path == instance_dir / "run_replay_receipts" / "run-1.json"
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1").status == "pending"

    write_run_replay_receipt(instance_dir, _receipt(status="parity"))
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1").status == "parity"


def test_pending_receipt_snapshots_the_evidence_end_seq(tmp_path: Path) -> None:
    """Stop snapshots the journal position too, so a later replay reads the run's
    bars and its continuity events at the same evidence bound."""
    binding = _binding(run_id="run-1")
    service = RunReplayProofService(
        artifacts_root=tmp_path / "artifacts",
        instance_dir_for=lambda sid: tmp_path / "live_state" / sid,
        binding_for=lambda broker, sid: binding,
        run_record_for=lambda sid, run_id: None,
        is_running=lambda sid: False,
    )
    ledger = SourceBarLedger(
        artifacts_root=service.artifacts_root, account_id=ledger_account_id_for(binding)
    )
    try:
        ledger.append(_market_bar(0), run_id=binding.run_id)
        ledger.append(_market_bar(1), run_id=binding.run_id)
        ledger.append_event(
            FeedContinuityEvent(
                kind="interruption",
                feed_id="feed-a",
                symbol="SPY",
                observed_at_ms=1_700_000_120_000,
                cause="socket_down",
            ),
            run_id=binding.run_id,
        )

        service.write_pending(binding, "run-1")

        receipt = read_run_replay_receipt(
            service.instance_dir_for(binding.strategy_instance_id), binding.strategy_instance_id, "run-1"
        )
        assert receipt is not None
        assert receipt.ledger_end_seq == 2
        assert receipt.evidence_end_seq == ledger.evidence_end_seq() == 3
    finally:
        ledger.close(checkpoint=False)


def test_read_run_replay_receipt_absent_is_none_and_foreign_identity_raises(tmp_path: Path) -> None:
    instance_dir = tmp_path / "live_state" / "bot-a"
    assert read_run_replay_receipt(instance_dir, "bot-a", "run-1") is None

    write_run_replay_receipt(instance_dir, _receipt(run_id="run-1"))
    with pytest.raises(ValueError):
        read_run_replay_receipt(instance_dir, "bot-OTHER", "run-1")
