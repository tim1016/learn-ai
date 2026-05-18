"""Launcher service — pure logic, no FastAPI binding.

The FastAPI app (``app.py``) is a thin transport on top of this module
so the launcher can also be invoked from tests in-process without
binding a TCP port.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from app.lean_sidecar.config import RunLimits
from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.result_classifier import classify_workspace
from app.lean_sidecar.runner import (
    HardeningProfile,
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
        # Pass exactly one of profile-or-flags to build_command. The
        # model validator already rejects both-set; this rejects neither-
        # set being ambiguous: when profile is None, fall through to the
        # raw flags (defaults to empty tuple, the Phase 1 safe shape).
        if request.hardening_profile is not None:
            plan = build_command(
                workspace,
                request.image_digest,
                limits=limits,
                hardening_profile=HardeningProfile(request.hardening_profile),
            )
        else:
            plan = build_command(
                workspace,
                request.image_digest,
                limits=limits,
                hardening_flags=tuple(request.hardening_flags),
            )
    except RunnerConfigurationError as e:
        # The runner itself decides which configuration is acceptable
        # (image-allow-list, podman-on-path, hardening allow-list).
        # Propagate the message as a rejection so the API contract is
        # consistent.
        raise LaunchRejectedError("runner_configuration_error", str(e)) from e

    log_path = workspace.launcher_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        f.write("# launcher plan\n")
        # Shell-quoted single-line form first so an operator can
        # copy/paste it into a terminal to reproduce manually. The
        # argv-per-line form below stays for audit grep-ability.
        f.write(f"# shell: {shlex.join(plan.argv)}\n")
        for arg in plan.argv:
            f.write(f"{arg}\n")
        f.write("# end launcher plan\n")

    result: RunResult = execute(plan, limits=limits)

    # Classify LEAN's own log.txt — exit_code 0 alone lies (LEAN can
    # crash ResultsAnalyzer, fail data requests, or raise in
    # Initialize while still exiting 0). The classifier categorizes
    # any ERROR:: lines so callers can branch on actual cleanliness.
    classified = classify_workspace(workspace.lean_log_path)

    # Post-run workspace size enforcement. The launcher does not stream
    # mid-run size — wall-clock timeout already caps how much the
    # container can write — but after the container exits we walk the
    # workspace and surface a hard error if it overran the cap so the
    # operator sees the violation rather than a silently-stale workspace.
    # The ADR's longer-term "kill on overrun during run" is queued for
    # Phase 1c+ once a separate monitor process exists.
    overran = _workspace_size_bytes(workspace.workspace_dir) > limits.workspace_max_mb * (1 << 20)

    is_clean = result.exit_code == 0 and not result.timed_out and classified.is_clean

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n# launcher result\n")
        f.write(f"exit_code: {result.exit_code}\n")
        f.write(f"duration_ms: {result.duration_ms}\n")
        f.write(f"timed_out: {result.timed_out}\n")
        f.write(f"workspace_overran_cap: {overran}\n")
        f.write(f"is_clean: {is_clean}\n")
        f.write(f"lean_error_total: {classified.total}\n")
        for cat in classified.categories:
            f.write(f"  lean_errors[{cat}]: {len(classified.by_category[cat])}\n")
        f.write("# container log tail (truncated):\n")
        f.write(result.log_tail)
        if not result.log_tail.endswith("\n"):
            f.write("\n")
        f.write("# end launcher result\n")

    if overran:
        raise LaunchRejectedError(
            "workspace_max_mb_exceeded",
            f"workspace exceeded {limits.workspace_max_mb} MiB cap after "
            f"container exit (exit_code={result.exit_code}); "
            "see launcher.log for the run trace",
        )

    return LaunchResponse(
        run_id=request.run_id,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        log_tail=result.log_tail,
        lean_errors=dict(classified.by_category),
        is_clean=is_clean,
    )


def _workspace_size_bytes(root: Path) -> int:
    """Sum on-disk file sizes under ``root``. Symlinks are not followed.

    Used by ``launch()`` to enforce ``workspace_max_mb`` after the
    container exits. Walking after-the-fact is acceptable for Phase 1
    because ``wall_clock_timeout_s`` already caps how much the
    container could write in one run; live monitoring is a Phase 1c+
    item per the ADR.
    """
    total = 0
    for path in root.rglob("*"):
        # ``is_file()`` follows symlinks; we want disk usage, not link
        # target sizes that may live outside the workspace.
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                # A file disappearing mid-walk is rare but possible if
                # the launcher is racing a still-shutting-down LEAN
                # process. Treat as zero — the next walk picks it up
                # or proves the file is truly gone.
                continue
    return total
