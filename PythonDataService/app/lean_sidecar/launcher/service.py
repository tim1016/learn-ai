"""Launcher service — pure logic, no FastAPI binding.

The FastAPI app (``app.py``) is a thin transport on top of this module
so the launcher can also be invoked from tests in-process without
binding a TCP port.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.lean_sidecar.config import RunLimits
from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.runner import (
    RunnerConfigurationError,
    RunResult,
    build_command,
    execute,
)
from app.lean_sidecar.workspace import (
    WorkspaceError,
    resolve_workspace,
)

logger = logging.getLogger(__name__)


class LaunchRejectedError(Exception):
    """The launcher refused to invoke the container.

    Raised before any ``podman run`` is spawned. ``reason`` is a short
    operator-facing label (``"image_not_allowlisted"`` etc.) so callers
    can route on it without parsing free-text messages.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def launch(request: LaunchRequest, *, artifacts_root: Path) -> LaunchResponse:
    """Validate, plan, execute, and persist the launcher log.

    Order of operations is load-bearing for safety:
    1. Resolve workspace under the configured artifacts root.
    2. Refuse if the workspace directory has not been pre-populated.
    3. Build the podman argv (re-asserts image allow-list + limits).
    4. Write the planned argv to ``launcher.log`` *before* execution.
    5. Execute, capturing exit code, duration, and log tail.
    6. Append the result to ``launcher.log``.

    Writing the plan before execution means a launcher crash mid-run
    still leaves an audit trail of "the launcher tried to invoke
    exactly this".
    """
    try:
        workspace = resolve_workspace(request.run_id, artifacts_root)
    except WorkspaceError as e:
        raise LaunchRejectedError("invalid_run_id_or_path", str(e)) from e

    if not workspace.workspace_dir.exists():
        raise LaunchRejectedError(
            "workspace_not_staged",
            f"{workspace.workspace_dir} does not exist; stage data, config, and source before launching",
        )

    limits = RunLimits(
        cpus=request.cpus,
        memory_mb=request.memory_mb,
        pids_limit=request.pids_limit,
        wall_clock_timeout_s=request.wall_clock_timeout_s,
        workspace_max_mb=request.workspace_max_mb,
        log_tail_bytes=request.log_tail_bytes,
    )

    try:
        plan = build_command(
            workspace,
            request.image_digest,
            limits=limits,
            extra_image_args=tuple(request.extra_image_args),
            hardening_flags=tuple(request.hardening_flags),
        )
    except RunnerConfigurationError as e:
        # The runner itself decides which configuration is acceptable
        # (image-allow-list, podman-on-path). Propagate the message as
        # a rejection so the API contract is consistent.
        raise LaunchRejectedError("runner_configuration_error", str(e)) from e

    log_path = workspace.launcher_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        f.write("# launcher plan\n")
        for arg in plan.argv:
            f.write(f"{arg}\n")
        f.write("# end launcher plan\n")

    result: RunResult = execute(plan, limits=limits)

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n# launcher result\n")
        f.write(f"exit_code: {result.exit_code}\n")
        f.write(f"duration_ms: {result.duration_ms}\n")
        f.write(f"timed_out: {result.timed_out}\n")
        f.write("# container log tail (truncated):\n")
        f.write(result.log_tail)
        if not result.log_tail.endswith("\n"):
            f.write("\n")
        f.write("# end launcher result\n")

    return LaunchResponse(
        run_id=request.run_id,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        log_tail=result.log_tail,
    )
