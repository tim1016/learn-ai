"""Stop schedules background replay-receipt generation (Direction 2)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest

from app.services.bot_runner import BotTaskRegistry
from tests.services.bot_runner.conftest import _SID
from tests.services.test_candidate_uncaptured_at_crash import _binding


def _registry(tmp_path: Path) -> BotTaskRegistry:
    return BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)


def _method_calls(func, name: str) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        for node in ast.walk(tree)
    )


def _exc_label(exc_type) -> str:
    if isinstance(exc_type, ast.Attribute):
        return exc_type.attr
    if isinstance(exc_type, ast.Name):
        return exc_type.id
    return "?"


def _supervise_terminal_branches() -> dict[str, list]:
    """Map each branch of _supervise's terminal-dispatch try/except/else to its body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(BotTaskRegistry._supervise)))
    for node in ast.walk(tree):
        labels = (
            {_exc_label(handler.type) for handler in node.handlers}
            if isinstance(node, ast.Try)
            else set()
        )
        if isinstance(node, ast.Try) and "MarketDataFeedError" in labels and node.orelse:
            branches = {_exc_label(handler.type): handler.body for handler in node.handlers}
            branches["else"] = node.orelse
            return branches
    raise AssertionError("could not find _supervise's terminal-dispatch try/except/else")


def _body_calls(body: list, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        for stmt in body
        for node in ast.walk(stmt)
    )


def test_stop_locked_schedules_the_replay_receipt() -> None:
    """AST pin, same idiom as test_signal_program_mode_parity's stream check:
    if Stop ever stops scheduling the receipt, this is the test that notices."""
    assert _method_calls(BotTaskRegistry._stop_locked, "_schedule_run_replay_receipt")


def test_supervise_schedules_the_replay_receipt_on_every_terminal_branch() -> None:
    """PR #1751 finding 5 + Codex PR #1769: each non-cancel terminal branch
    (feed death, crash, stream end) must schedule — a per-branch pin, so
    deleting the call from one branch while leaving another is caught."""
    branches = _supervise_terminal_branches()
    assert _body_calls(branches["MarketDataFeedError"], "_schedule_run_replay_receipt")  # feed death
    assert _body_calls(branches["Exception"], "_schedule_run_replay_receipt")  # in-process crash
    assert _body_calls(branches["else"], "_schedule_run_replay_receipt")  # bar stream ended
    # The cancel branch is deliberately excluded: operator Stop already
    # scheduled, and a kill is EXITED_UNVERIFIED, covered by the boot scan.
    assert not _body_calls(branches["CancelledError"], "_schedule_run_replay_receipt")


def test_run_boot_recovery_resumes_pending_replay_receipts() -> None:
    """PR #1751 finding 5: a process crash must not orphan `pending` evidence."""
    assert _method_calls(BotTaskRegistry.run_boot_recovery, "_resume_pending_replay_receipts")


@pytest.mark.asyncio
async def test_schedule_run_replay_receipt_writes_pending_then_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    calls: list[tuple[str, str, str]] = []

    async def _fake_generate(broker: str, sid: str, run_id: str):
        calls.append((broker, sid, run_id))

    monkeypatch.setattr(registry._replay_proof, "generate", _fake_generate)
    binding = _binding(run_id="run-1")  # trade-mode, ema_crossover_signal (a Signal Program)

    registry._schedule_run_replay_receipt(binding)

    pending = registry._replay_proof.read(_SID, "run-1")
    assert pending is not None and pending.status == "pending"
    assert registry._replay_receipt_tasks
    await asyncio.gather(*registry._replay_receipt_tasks)
    assert calls == [("alpaca", _SID, "run-1")]
    assert not registry._replay_receipt_tasks  # done-callback reaped it


@pytest.mark.asyncio
async def test_schedule_run_replay_receipt_skips_a_log_only_binding(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1").model_copy(update={"mode": "log_only"})

    registry._schedule_run_replay_receipt(binding)

    assert not registry._replay_receipt_tasks
    assert registry._replay_proof.read(_SID, "run-1") is None


@pytest.mark.asyncio
async def test_resume_pending_replay_receipts_schedules_terminal_runs_lacking_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot scan (PR #1751 finding 5): a terminal current run with no receipt —
    the crashed-process case — gets its generation scheduled at next boot."""
    from app.services.bot_binding_repository import BotRunOutcomeRecord

    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="CRASHED",
        reason_code="FEED_DEATH",
        recorded_at_ms=1_700_000_000_000,
    )
    monkeypatch.setattr(registry._bindings, "list_for_broker", lambda broker: [binding])
    monkeypatch.setattr(registry._bindings, "read_outcome", lambda sid, run_id: outcome)
    calls: list[tuple[str, str, str]] = []

    async def _fake_generate(broker: str, sid: str, run_id: str):
        calls.append((broker, sid, run_id))

    monkeypatch.setattr(registry._replay_proof, "generate", _fake_generate)

    registry._resume_pending_replay_receipts()

    await asyncio.gather(*registry._replay_receipt_tasks)
    assert calls == [("alpaca", _SID, "run-1")]


@pytest.mark.asyncio
async def test_resume_pending_replay_receipts_skips_runs_with_final_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.bot_binding_repository import BotRunOutcomeRecord
    from tests.services.test_run_replay_receipt_store import _receipt

    registry = _registry(tmp_path)
    binding = _binding(run_id="run-1")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id="run-1",
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=1_700_000_000_000,
    )
    monkeypatch.setattr(registry._bindings, "list_for_broker", lambda broker: [binding])
    monkeypatch.setattr(registry._bindings, "read_outcome", lambda sid, run_id: outcome)
    from app.services.run_replay_proof import write_run_replay_receipt

    final = _receipt(status="parity").model_copy(
        update={"strategy_instance_id": _SID, "strategy_key": binding.strategy_key}
    )
    write_run_replay_receipt(registry._replay_proof.instance_dir_for(_SID), final)

    registry._resume_pending_replay_receipts()

    assert not registry._replay_receipt_tasks  # nothing owed, nothing scheduled
