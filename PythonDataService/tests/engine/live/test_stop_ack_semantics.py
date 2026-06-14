"""Phase 6B / VCR-0018-B — Stop response distinguishes signal-accepted
from process-exited."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock


def test_exit_reason_from_code_classifies_known_codes() -> None:
    """``cmd_start`` exit codes get readable names so the cockpit can
    render "process gone" with cause rather than a bare rc."""
    from app.engine.live.host_daemon import _exit_reason_from_code

    assert _exit_reason_from_code(0) == "normal"
    assert _exit_reason_from_code(1) == "fatal_halt"
    assert _exit_reason_from_code(2) == "operator_refusal"
    assert _exit_reason_from_code(3) == "exception"
    assert _exit_reason_from_code(4) == "hydration_failure"
    assert _exit_reason_from_code(None) == "alive"
    assert _exit_reason_from_code(42) == "exited(42)"


def test_stop_response_carries_command_id_and_outcome() -> None:
    """VCR-0018-B / Phase 6B — the response carries a stable
    ``command_id`` and a distinct ``stop_outcome`` so the cockpit can
    render "signal sent" vs "process gone" as separate stages."""
    from app.schemas.live_runs import HostRunnerActionResponse, HostRunnerProcessState, HostRunnerProcessStatus

    response = HostRunnerActionResponse(
        accepted=True,
        process=HostRunnerProcessStatus(state=HostRunnerProcessState.idle),
        command_id="stop-abc123",
        stop_outcome="exited",
        exit_reason="normal",
    )
    assert response.command_id == "stop-abc123"
    assert response.stop_outcome == "exited"
    assert response.exit_reason == "normal"


def test_stop_outcome_still_running_after_timeout() -> None:
    """When the process does not exit within ``_STOP_WAIT_SECONDS`` and
    the operator did not pass ``force``, the response surfaces the new
    ``still_running_after_2s`` outcome instead of silently claiming
    ``accepted`` without telling the operator the process is still
    alive."""
    from app.engine.live.host_daemon import RunnerProcessManager, _STOP_WAIT_SECONDS
    from app.schemas.live_runs import HostRunnerStopRequest
    from pathlib import Path

    # The constant must be the documented 2 seconds per PRD / ADR 0010.
    assert _STOP_WAIT_SECONDS == 2.0

    manager = RunnerProcessManager(
        repo_root=Path("/tmp"), live_runs_root=Path("/tmp/live_runs")
    )
    process = MagicMock()
    process.poll.return_value = None  # alive
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=_STOP_WAIT_SECONDS)
    process.pid = 4242
    process.returncode = None
    current = MagicMock(
        process=process,
        run_id="run-1",
        instance_id="inst",
        started_at_ms=0,
        log_path=Path("/tmp/x.log"),
        command=["python"],
        stopping=False,
    )
    # Inject the fake "tracked process".
    manager._current = current  # type: ignore[attr-defined]
    manager._by_run_id = lambda run_id: current if run_id == "run-1" else None  # type: ignore[assignment]
    from app.schemas.live_runs import HostRunnerProcessState, HostRunnerProcessStatus

    manager._refresh = lambda _: None  # type: ignore[assignment]
    manager.process_status = lambda _run_id: HostRunnerProcessStatus(
        state=HostRunnerProcessState.running,
        pid=4242,
    )  # type: ignore[assignment]

    response = manager.stop("run-1", HostRunnerStopRequest(force=False))

    assert response.command_id is not None and response.command_id.startswith("stop-")
    assert response.stop_outcome == "still_running_after_2s"
    assert response.exit_reason is None
