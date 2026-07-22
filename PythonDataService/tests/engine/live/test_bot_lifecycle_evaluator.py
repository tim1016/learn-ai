"""Failure-first contract tests for the single lifecycle-evaluator writer."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from app.engine.live import bot_lifecycle_evaluator as evaluator_module
from app.engine.live.bot_lifecycle_evaluator import (
    BotLifecycleEvaluator,
    LifecycleDispositionAction,
    LifecycleDispositionCorruptError,
    LifecycleDispositionReceipt,
    LifecycleStartAdmissionEvidence,
    LifecycleTransitionRefusedError,
    stable_bot_lifecycle_disposition_log_path,
)
from app.engine.live.bot_lifecycle_fence import bot_lifecycle_operation_fence
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo, stable_desired_state_path
from app.engine.live.durable_append_log import append_jsonl_record

_SID = "paper-evaluator"


class _DirectLifecycleWriterVisitor(ast.NodeVisitor):
    """Reject a future second durable lifecycle/control-plane writer."""

    _MUTATORS = {
        "BotLifecycleStateRepo": {
            "set_roster",
            "set_phase",
            "retire",
            "record_terminal_outcome",
            "reopen_for_deploy",
            "update",
        },
        "DesiredStateRepo": {"set", "write", "delete"},
    }

    def __init__(self) -> None:
        self._repo_names: dict[str, str] = {}
        self.violations: list[str] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            repo_type = node.value.func.id
            if repo_type in self._MUTATORS:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._repo_names[target.id] = repo_type
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            method for methods in self._MUTATORS.values() for method in methods
        }:
            self.generic_visit(node)
            return
        repo_type = self._repo_type(node.func.value)
        if repo_type is not None and node.func.attr in self._MUTATORS[repo_type]:
            self.violations.append(f"{repo_type}.{node.func.attr} at line {node.lineno}")
        self.generic_visit(node)

    def _repo_type(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self._repo_names.get(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id if node.func.id in self._MUTATORS else None
        return None


def _receipts(root: Path) -> list[LifecycleDispositionReceipt]:
    path = stable_bot_lifecycle_disposition_log_path(root, _SID)
    return [LifecycleDispositionReceipt.model_validate_json(line) for line in path.read_text().splitlines()]


def _admission(run_id: str, *, strategy_instance_id: str = _SID) -> LifecycleStartAdmissionEvidence:
    return LifecycleStartAdmissionEvidence(
        policy="interactive",
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        roll_call_offer_id="offer-1",
        admitted_at_ms=10,
    )


def test_production_lifecycle_repositories_have_one_mutating_owner() -> None:
    app_root = Path(__file__).parents[3] / "app"
    excluded = {
        app_root / "engine/live/bot_lifecycle_evaluator.py",
        app_root / "engine/live/bot_lifecycle_state.py",
        app_root / "engine/live/desired_state.py",
    }
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        if path in excluded:
            continue
        visitor = _DirectLifecycleWriterVisitor()
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        violations.extend(f"{path.relative_to(app_root)}: {violation}" for violation in visitor.violations)

    assert violations == []


def test_start_requires_admission_and_never_clears_a_stopped_latch(tmp_path: Path) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.set_desired_state(
        DesiredState.STOPPED,
        now_ms=10,
        updated_by="operator",
        reason="operator_stop",
    )

    with pytest.raises(LifecycleTransitionRefusedError, match="matching typed"):
        evaluator.record_start_accepted(
            run_id="run-1",
            now_ms=20,
            updated_by="router",
            admission=_admission("run-1", strategy_instance_id="different-instance"),
        )

    with pytest.raises(LifecycleTransitionRefusedError, match="STOPPED_REQUIRES_RESUME"):
        evaluator.assert_start_latch_allows_start()


def test_prepared_start_is_durable_before_actuation_and_commits_from_daemon_observation(
    tmp_path: Path,
) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)

    prepared = evaluator.prepare_start(
        run_id="run-prepared",
        now_ms=10,
        updated_by="router",
        admission=_admission("run-prepared"),
    )

    assert prepared.receipt.status == "PENDING"
    assert BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, _SID)).read() is None
    recovered = evaluator.recover_prepared_start_from_daemon_observation(
        run_id="run-prepared",
        daemon_state="running",
        observed_at_ms=20,
    )

    assert recovered is not None
    assert recovered.receipt.status == "COMMITTED"
    assert recovered.lifecycle_state is not None
    assert recovered.lifecycle_state.phase is BotLifecyclePhase.ON_DUTY
    assert recovered.lifecycle_state.active_run_id == "run-prepared"
    assert [(receipt.sequence, receipt.status) for receipt in _receipts(tmp_path)] == [
        (1, "PENDING"),
        (1, "COMMITTED"),
    ]


def test_prepared_start_is_aborted_only_from_a_known_nonrunning_daemon_observation(
    tmp_path: Path,
) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.prepare_start(
        run_id="run-rejected",
        now_ms=10,
        updated_by="router",
        admission=_admission("run-rejected"),
    )

    recovered = evaluator.recover_prepared_start_from_daemon_observation(
        run_id="run-rejected",
        daemon_state="idle",
        observed_at_ms=20,
    )

    assert recovered is not None
    assert recovered.receipt.status == "ABORTED"
    assert BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, _SID)).read() is None


def test_prepared_start_blocks_an_unrelated_lifecycle_writer_until_reconciled(tmp_path: Path) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.prepare_start(
        run_id="run-response-lost",
        now_ms=10,
        updated_by="router",
        admission=_admission("run-response-lost"),
    )

    with pytest.raises(LifecycleTransitionRefusedError, match="START_ACTUATION_UNRESOLVED"):
        evaluator.set_roster(False, now_ms=20, updated_by="operator")

    assert [(receipt.sequence, receipt.status) for receipt in _receipts(tmp_path)] == [(1, "PENDING")]


def test_stale_terminal_fact_cannot_supersede_a_newer_on_duty_run(tmp_path: Path) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.record_start_accepted(
        run_id="run-old",
        now_ms=10,
        updated_by="router",
        admission=_admission("run-old"),
    )
    evaluator.record_start_accepted(
        run_id="run-new",
        now_ms=20,
        updated_by="router",
        admission=_admission("run-new"),
    )

    evaluator.record_terminal_outcome(
        BotDutyOutcome(
            kind="CRASHED",
            reason_code="PROCESS_CRASHED",
            recorded_at_ms=30,
            run_id="run-old",
        ),
        updated_by="daemon",
        reason="process.crashed",
        expected_active_run_id="run-old",
    )

    record = BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, _SID)).read()
    assert record is not None
    assert record.phase is BotLifecyclePhase.ON_DUTY
    assert record.active_run_id == "run-new"


def test_interrupted_receipt_is_reconciled_from_the_atomic_state_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    append = evaluator_module.append_jsonl_record

    def fail_only_commit(path: Path, row: str, *, trusted_root: Path) -> None:
        if '"status":"COMMITTED"' in row:
            raise OSError("injected append failure after state write")
        append(path, row, trusted_root=trusted_root)

    monkeypatch.setattr(evaluator_module, "append_jsonl_record", fail_only_commit)
    with pytest.raises(OSError, match="injected append failure"):
        evaluator.set_roster(False, now_ms=10, updated_by="operator")
    monkeypatch.setattr(evaluator_module, "append_jsonl_record", append)

    # The next command first repairs the prepared transition, then records its
    # own disposition. No broker or Clerk dependency is involved.
    evaluator.set_desired_state(
        DesiredState.PAUSED,
        now_ms=20,
        updated_by="operator",
        reason="broker_unavailable_pause",
    )

    receipts = _receipts(tmp_path)
    assert [(receipt.sequence, receipt.status) for receipt in receipts] == [
        (1, "PENDING"),
        (1, "COMMITTED"),
        (2, "PENDING"),
        (2, "COMMITTED"),
    ]
    assert receipts[1].on_roster is False
    assert receipts[3].desired_state is DesiredState.PAUSED


def test_interrupted_control_receipt_is_reconciled_from_the_desired_state_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    append = evaluator_module.append_jsonl_record

    def fail_only_commit(path: Path, row: str, *, trusted_root: Path) -> None:
        if '"status":"COMMITTED"' in row:
            raise OSError("injected control commit failure")
        append(path, row, trusted_root=trusted_root)

    monkeypatch.setattr(evaluator_module, "append_jsonl_record", fail_only_commit)
    with pytest.raises(OSError, match="injected control commit failure"):
        evaluator.set_desired_state(
            DesiredState.PAUSED,
            now_ms=10,
            updated_by="operator",
            reason="clerk_offline_pause",
        )
    monkeypatch.setattr(evaluator_module, "append_jsonl_record", append)

    evaluator.set_roster(False, now_ms=20, updated_by="operator")

    receipts = _receipts(tmp_path)
    assert [(receipt.sequence, receipt.status) for receipt in receipts] == [
        (1, "PENDING"),
        (1, "COMMITTED"),
        (2, "PENDING"),
        (2, "COMMITTED"),
    ]
    assert receipts[1].desired_state is DesiredState.PAUSED


def test_concurrent_control_commands_are_serialized_with_contiguous_receipts(tmp_path: Path) -> None:
    def set_state(state: DesiredState) -> None:
        BotLifecycleEvaluator(tmp_path, _SID).set_desired_state(
            state,
            now_ms=10,
            updated_by="operator",
            reason="concurrent_control_test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(set_state, (DesiredState.PAUSED, DesiredState.STOPPED)))

    receipts = _receipts(tmp_path)
    assert [(receipt.sequence, receipt.status) for receipt in receipts] == [
        (1, "PENDING"),
        (1, "COMMITTED"),
        (2, "PENDING"),
        (2, "COMMITTED"),
    ]
    record = DesiredStateRepo(
        stable_desired_state_path(tmp_path, _SID),
        trusted_root=tmp_path / "live_state",
    ).read()
    assert record is not None
    assert record.version == 2
    assert record.desired_state in {DesiredState.PAUSED, DesiredState.STOPPED}


def test_terminal_fact_waits_for_retirement_fence_and_cannot_unretire(tmp_path: Path) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.record_start_accepted(
        run_id="run-active",
        now_ms=10,
        updated_by="router",
        admission=_admission("run-active"),
    )
    entered = Event()
    completed = Event()

    def report_terminal() -> None:
        entered.set()
        evaluator.record_terminal_outcome(
            BotDutyOutcome(
                kind="CRASHED",
                reason_code="PROCESS_CRASHED",
                recorded_at_ms=20,
                run_id="run-active",
            ),
            updated_by="daemon",
            reason="process.crashed",
            expected_active_run_id="run-active",
        )
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with bot_lifecycle_operation_fence(tmp_path, _SID):
            future = executor.submit(report_terminal)
            assert entered.wait(timeout=1)
            assert not completed.wait(timeout=0.05)
            evaluator.retire(
                now_ms=15,
                updated_by="operator",
                reason="replacement",
                operation_fence_held=True,
            )
        future.result()

    record = BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, _SID)).read()
    assert record is not None
    assert record.phase is BotLifecyclePhase.RETIRED


def test_idempotent_retire_repairs_its_interrupted_receipt_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    append = evaluator_module.append_jsonl_record

    def fail_only_commit(path: Path, row: str, *, trusted_root: Path) -> None:
        if '"status":"COMMITTED"' in row:
            raise OSError("injected retirement commit failure")
        append(path, row, trusted_root=trusted_root)

    monkeypatch.setattr(evaluator_module, "append_jsonl_record", fail_only_commit)
    with pytest.raises(OSError, match="injected retirement commit failure"):
        evaluator.retire(now_ms=10, updated_by="operator", reason="replacement")
    monkeypatch.setattr(evaluator_module, "append_jsonl_record", append)

    evaluator.retire(now_ms=20, updated_by="operator", reason="replacement")

    assert [(receipt.sequence, receipt.status) for receipt in _receipts(tmp_path)] == [
        (1, "PENDING"),
        (1, "COMMITTED"),
    ]


def test_corrupt_duplicate_receipt_completion_fails_closed(tmp_path: Path) -> None:
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)
    evaluator.set_roster(False, now_ms=10, updated_by="operator")
    path = stable_bot_lifecycle_disposition_log_path(tmp_path, _SID)
    completion = path.read_text().splitlines()[1]
    append_jsonl_record(path, completion, trusted_root=tmp_path / "live_state")

    with pytest.raises(LifecycleDispositionCorruptError, match="completion"):
        evaluator.set_desired_state(DesiredState.PAUSED, now_ms=20, updated_by="operator")


def test_corrupt_second_prepare_before_completion_fails_closed(tmp_path: Path) -> None:
    path = stable_bot_lifecycle_disposition_log_path(tmp_path, _SID)
    first = LifecycleDispositionReceipt(
        sequence=1,
        receipt_id=f"{_SID}:1",
        strategy_instance_id=_SID,
        action=LifecycleDispositionAction.ROSTER_CHANGED,
        status="PENDING",
        recorded_at_ms=10,
        updated_by="operator",
    )
    second = first.model_copy(update={"sequence": 2, "receipt_id": f"{_SID}:2"})
    append_jsonl_record(path, first.model_dump_json(), trusted_root=tmp_path / "live_state")
    append_jsonl_record(path, second.model_dump_json(), trusted_root=tmp_path / "live_state")

    with pytest.raises(LifecycleDispositionCorruptError, match="prepare arrived before"):
        BotLifecycleEvaluator(tmp_path, _SID).set_roster(False, now_ms=20, updated_by="operator")
