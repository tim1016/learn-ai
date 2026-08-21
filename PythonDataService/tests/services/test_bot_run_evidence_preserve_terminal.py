"""Focused coverage for issue #1712: receipt-authoritative preservation.

``BotRunEvidenceService.preserve_terminal`` is exercised directly against a
real, file-backed ``BotBindingRepository`` (no mocking) to pin the reuse /
create / disagreement-warning behavior described in PRD #1716 FR-1, without
needing the full async supervision machinery. The end-to-end regression
(crash then Resume through the public ``BotTaskRegistry`` interface) lives in
``test_bot_runner_ema_resume.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.engine.live.bot_lifecycle_state import BotDutyOutcome, BotLifecycleStateRecord
from app.engine.live.identity import strategy_instance_artifact_dir
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BotRunOutcomeRecord,
    BrokerBotBinding,
    RunOutcomeConflictError,
    alpaca_v1_action_plan,
)
from app.services.bot_run_evidence import BotRunEvidenceService

_STRATEGY_INSTANCE_ID = "focus-1712"
_RUN_ID = "run-1712-a"


def _repository(tmp_path: Path) -> BotBindingRepository:
    return BotBindingRepository(
        tmp_path,
        instance_dir_for=lambda sid: strategy_instance_artifact_dir(
            tmp_path, "live_state", sid
        ),
    )


def _service(repository: BotBindingRepository) -> BotRunEvidenceService:
    return BotRunEvidenceService(
        repository,
        # preserve_terminal never consults either collaborator below.
        lifecycle_repo_for=lambda _sid: None,
        lifecycle_projector=None,
    )


def _seed_run(repository: BotBindingRepository, *, run_id: str = _RUN_ID) -> None:
    """Create the append-only run file record_outcome()/read_outcome() need."""
    binding = BrokerBotBinding(
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        broker="alpaca",
        symbol="SPY",
        run_id=run_id,
        created_at_ms=1_000,
        action_plan=alpaca_v1_action_plan("SPY"),
    )
    repository.record_launch(binding, launch_reason="deploy")


def _lifecycle_with_outcome(outcome: BotDutyOutcome) -> BotLifecycleStateRecord:
    return BotLifecycleStateRecord(
        last_transition_at_ms=outcome.recorded_at_ms,
        duty_outcome=outcome,
    )


def test_preserve_terminal_is_a_noop_for_a_fresh_deploy(tmp_path: Path) -> None:
    """AC-7: no lifecycle record means nothing to preserve."""
    repository = _repository(tmp_path)
    service = _service(repository)

    service.preserve_terminal(_STRATEGY_INSTANCE_ID, None)

    assert repository.read_outcome(_STRATEGY_INSTANCE_ID, _RUN_ID) is None


def test_preserve_terminal_creates_a_receipt_when_none_exists(tmp_path: Path) -> None:
    """AC-3: projection-only terminal evidence is converted into a receipt."""
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_run(repository)
    outcome = BotDutyOutcome(
        kind="EXITED_UNVERIFIED",
        reason_code="INTERRUPTED_BY_RESTART",
        recorded_at_ms=5_000,
        run_id=_RUN_ID,
    )

    service.preserve_terminal(_STRATEGY_INSTANCE_ID, _lifecycle_with_outcome(outcome))

    receipt = repository.read_outcome(_STRATEGY_INSTANCE_ID, _RUN_ID)
    assert receipt is not None
    assert receipt.kind == "EXITED_UNVERIFIED"
    assert receipt.reason_code == "INTERRUPTED_BY_RESTART"
    assert receipt.recorded_at_ms == 5_000


def test_preserve_terminal_leaves_a_matching_receipt_untouched(tmp_path: Path) -> None:
    """Reuse path: an existing, agreeing receipt is never rewritten."""
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_run(repository)
    repository.record_outcome(
        BotRunOutcomeRecord(
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            run_id=_RUN_ID,
            kind="CRASHED",
            reason_code="TypeError",
            recorded_at_ms=1_234,
        )
    )
    outcome = BotDutyOutcome(
        kind="CRASHED",
        reason_code="TypeError",
        recorded_at_ms=1_234,
        run_id=_RUN_ID,
    )

    service.preserve_terminal(_STRATEGY_INSTANCE_ID, _lifecycle_with_outcome(outcome))

    receipt = repository.read_outcome(_STRATEGY_INSTANCE_ID, _RUN_ID)
    assert receipt is not None
    assert receipt.kind == "CRASHED"
    assert receipt.reason_code == "TypeError"
    assert receipt.recorded_at_ms == 1_234


def test_preserve_terminal_reuses_a_receipt_that_disagrees_with_the_projection(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-2: a stale/restamped projection can never override the receipt."""
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_run(repository)
    repository.record_outcome(
        BotRunOutcomeRecord(
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            run_id=_RUN_ID,
            kind="CRASHED",
            reason_code="TypeError",
            recorded_at_ms=1_234,
        )
    )
    # Simulates a boot sweep restamping the projection after the receipt was
    # already committed (PRD #1716 §2, "second trigger").
    restamped = BotDutyOutcome(
        kind="EXITED_UNVERIFIED",
        reason_code="INTERRUPTED_BY_RESTART",
        recorded_at_ms=9_999,
        run_id=_RUN_ID,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.bot_run_evidence"):
        service.preserve_terminal(
            _STRATEGY_INSTANCE_ID, _lifecycle_with_outcome(restamped)
        )

    receipt = repository.read_outcome(_STRATEGY_INSTANCE_ID, _RUN_ID)
    assert receipt is not None
    assert receipt.kind == "CRASHED"
    assert receipt.reason_code == "TypeError"
    assert receipt.recorded_at_ms == 1_234
    disagreements = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "terminal_receipt_projection_disagreement"
    ]
    assert len(disagreements) == 1


def test_preserve_terminal_skips_a_provisional_stop(tmp_path: Path) -> None:
    """Provisional stops are resolved later by replace_provisional_stop, not here."""
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_run(repository)
    outcome = BotDutyOutcome(
        kind="STOPPED",
        reason_code="STOPPED_PENDING_CUSTODY_PROOF",
        recorded_at_ms=1_234,
        run_id=_RUN_ID,
    )

    service.preserve_terminal(_STRATEGY_INSTANCE_ID, _lifecycle_with_outcome(outcome))

    assert repository.read_outcome(_STRATEGY_INSTANCE_ID, _RUN_ID) is None


def test_preserve_terminal_propagates_an_unreadable_receipt(tmp_path: Path) -> None:
    """An unreadable receipt must never fall through to reconstruction."""
    repository = _repository(tmp_path)
    service = _service(repository)
    _seed_run(repository)
    receipt_path = (
        strategy_instance_artifact_dir(tmp_path, "live_state", _STRATEGY_INSTANCE_ID)
        / "run_outcomes"
        / f"{_RUN_ID}.json"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text("{not valid json", encoding="utf-8")
    outcome = BotDutyOutcome(
        kind="CRASHED",
        reason_code="TypeError",
        recorded_at_ms=1_234,
        run_id=_RUN_ID,
    )

    with pytest.raises(ValueError):
        service.preserve_terminal(_STRATEGY_INSTANCE_ID, _lifecycle_with_outcome(outcome))

    # Unchanged: the corrupt file was never overwritten by a reconstruction.
    assert receipt_path.read_text(encoding="utf-8") == "{not valid json"


def test_record_outcome_rejects_a_genuinely_different_receipt(tmp_path: Path) -> None:
    """AC-6: writing conflicting terminal evidence for the same run still raises."""
    repository = _repository(tmp_path)
    _seed_run(repository)
    repository.record_outcome(
        BotRunOutcomeRecord(
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            run_id=_RUN_ID,
            kind="CRASHED",
            reason_code="TypeError",
            recorded_at_ms=1_234,
        )
    )

    with pytest.raises(RunOutcomeConflictError):
        repository.record_outcome(
            BotRunOutcomeRecord(
                strategy_instance_id=_STRATEGY_INSTANCE_ID,
                run_id=_RUN_ID,
                kind="CRASHED",
                reason_code="ValueError",
                recorded_at_ms=1_234,
            )
        )
