"""Podman invocation core for the LEAN sidecar.

This module owns the `podman run` command construction and execution. It
is the only place in the codebase that may spawn a container that
executes user-supplied source. Every flag in the constructed command
maps back to a row in ``docs/architecture/lean-sidecar-lab.md``
§"Container execution boundary".

The runner is intentionally a thin, testable function on top of
``subprocess``: the launcher service wraps it with request validation,
workspace-size monitoring, and timeout enforcement. Keeping podman-shell
construction here, separate from launcher policy, lets the integration
tests assert the constructed argv without spawning a real container.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from app.lean_sidecar.config import (
    ALLOWED_IMAGE_DIGESTS,
    DEFAULT_RUN_LIMITS,
    LEAN_IMAGE_REPO,
    RunLimits,
)
from app.lean_sidecar.workspace import Workspace


class RunnerConfigurationError(RuntimeError):
    """Raised when the runner cannot or must not launch.

    Examples: requested image is not in the digest allow-list; podman is
    not installed; workspace directory missing on disk.
    """


# Container-side mount point for the workspace. The launcher mounts only
# this single directory; nothing else under the artifacts root is
# visible to the LEAN container.
CONTAINER_WORKSPACE_MOUNT = "/lean-run"

# Token-level allow-list for caller-supplied hardening flags.
#
# Phase 1c promoted ``--read-only`` and ``--user=10001:10001`` into the
# mandatory shape, so the only callers-supply hardening surface left
# is ``--tmpfs <spec>``. Keeping the allow-list (rather than removing
# it) means a caller passing ``--privileged``,
# ``--security-opt=seccomp=unconfined``, etc. still aborts the run
# with ``RunnerConfigurationError`` rather than silently widening the
# sandbox.
#
# The set lists individual argv tokens, not full flag strings, because
# ``--tmpfs <spec>`` splits across two argv entries. Adding a new flag
# is a deliberate change this set documents.
ALLOWED_HARDENING_TOKENS: frozenset[str] = frozenset(
    {
        # --tmpfs takes a second token; allow the flag plus the
        # specific tmpfs specs we've validated.
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "/tmp:rw,noexec,nosuid,size=64m",
    }
)

# LEAN's launcher reads ``config.json`` from its working directory by
# default. The image's working dir is ``/Lean/Launcher/bin/Debug`` and
# the image ships a default ``config.json`` there pointing at the
# `BasicTemplateFrameworkAlgorithm`. We always pass ``--config`` to
# point LEAN at the config the data plane wrote into the workspace,
# otherwise the run silently executes the default template.
CONTAINER_LEAN_CONFIG_PATH = f"{CONTAINER_WORKSPACE_MOUNT}/project/config.json"

# Fallback UID/GID for hosts where ``os.getuid()`` is unavailable
# (Windows native Python). On Windows + WSL2 podman the WSL2 mount
# layer does not enforce host POSIX ownership inside the container,
# so any non-root UID works.
_FALLBACK_CONTAINER_UID = 10001
_FALLBACK_CONTAINER_GID = 10001


def _container_user_spec() -> str:
    """Return the ``--user <uid>:<gid>`` spec for the LEAN container.

    On Linux the spec is the launcher process's own UID/GID so the
    container's effective user matches the owner of the workspace
    files the launcher just created. Without that alignment, the
    container (running as a fixed 10001:10001) cannot write to
    ``workspace/output``/ObjectStore because POSIX permissions
    reject the cross-UID write — reviewer-flagged regression on
    native Linux hosts that the launcher does not chown around.

    On Windows the launcher runs as a native Windows process where
    ``os.getuid()`` does not exist; the bind mount goes through the
    WSL2 layer which does not enforce host UID, so the
    ``10001:10001`` fallback works. The fixed UID is non-root
    (covers the "don't run as container root" requirement) and is
    explicit in the launcher.log for audit.
    """
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return f"{os.getuid()}:{os.getgid()}"
    return f"{_FALLBACK_CONTAINER_UID}:{_FALLBACK_CONTAINER_GID}"


@dataclass(frozen=True, slots=True)
class RunnerPlan:
    """The fully constructed `podman run` argv plus the resolved digest.

    Returned by :func:`build_command` so tests can assert on the exact
    invocation without spawning a container, and the launcher can log
    the planned command to ``launcher.log`` before execution.
    """

    image_reference: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of a real container invocation."""

    exit_code: int
    duration_ms: int
    timed_out: bool
    log_tail: str


def _require_podman() -> str:
    """Return the absolute path to podman, or raise.

    The launcher refuses to run if podman is not on PATH; the error
    message names what is missing so the operator does not get a generic
    FileNotFoundError. We do not silently fall back to docker — docker
    has different default flag semantics that could weaken the
    sandbox.
    """
    podman = shutil.which("podman")
    if not podman:
        raise RunnerConfigurationError("podman is required but was not found on PATH")
    return podman


def _require_image_in_allowlist(image_digest: str) -> str:
    """Verify ``image_digest`` is in the static allow-list.

    Accepts either a bare ``sha256:...`` digest or
    ``quantconnect/lean@sha256:...``. The launcher never resolves tags
    like ``:latest`` — only pinned digests pass.
    """
    bare = image_digest.split("@", 1)[-1]
    if not bare.startswith("sha256:"):
        raise RunnerConfigurationError(f"image must be pinned by sha256 digest, got {image_digest!r}")
    if not ALLOWED_IMAGE_DIGESTS:
        raise RunnerConfigurationError(
            "no LEAN image digests pinned yet — pin one in config.py before invoking the runner"
        )
    if bare not in ALLOWED_IMAGE_DIGESTS:
        raise RunnerConfigurationError(f"image digest {bare} is not in ALLOWED_IMAGE_DIGESTS")
    return f"{LEAN_IMAGE_REPO}@{bare}"


def _validate_hardening_flags(hardening_flags: tuple[str, ...]) -> None:
    """Reject hardening tokens not on the allow-list and malformed pairs.

    Two checks:

    1. **Token membership.** Every argv token must be in
       :data:`ALLOWED_HARDENING_TOKENS`. Unknown tokens
       (``--privileged``, ``--cap-add=...``, ``--security-opt=...``,
       etc.) abort the run; callers cannot widen the sandbox.
    2. **Pair structure.** Two-token flags must be followed by their
       value token. ``["--tmpfs"]`` on its own would silently pass
       token-membership but then podman would consume the image
       reference as the tmpfs spec, breaking the launch in a confusing
       way. We reject those before the argv reaches podman.
    """
    unknown = [t for t in hardening_flags if t not in ALLOWED_HARDENING_TOKENS]
    if unknown:
        raise RunnerConfigurationError(
            f"hardening_flags rejected — tokens not on the allow-list: {unknown}. "
            f"Allowed tokens: {sorted(ALLOWED_HARDENING_TOKENS)}"
        )
    # Pair-structure check: each flag-name token (those starting with
    # ``--`` and not self-contained like ``--read-only``) must be
    # followed by a value token.
    i = 0
    while i < len(hardening_flags):
        token = hardening_flags[i]
        needs_value = token in {"--tmpfs"}
        if needs_value:
            if i + 1 >= len(hardening_flags):
                raise RunnerConfigurationError(f"hardening flag {token!r} requires a value token after it; got nothing")
            value = hardening_flags[i + 1]
            if value.startswith("--"):
                raise RunnerConfigurationError(f"hardening flag {token!r} expects a value, got another flag {value!r}")
            i += 2
        else:
            i += 1


def build_command(
    workspace: Workspace,
    image_digest: str,
    *,
    limits: RunLimits = DEFAULT_RUN_LIMITS,
    hardening_flags: tuple[str, ...] = (),
) -> RunnerPlan:
    """Construct the `podman run` argv for this workspace + image.

    The returned argv is the *exact* command the runner will execute.
    Tests assert on it so a future refactor cannot silently widen the
    sandbox.

    ``hardening_flags`` are the optional flags from the ADR's "if
    compatible" set (``--read-only``, ``--tmpfs ...``) whose viability
    was proven by the security-flag matrix test. Every token must be in
    :data:`ALLOWED_HARDENING_TOKENS`; an empty tuple is the safe
    default. ``--cap-drop=ALL`` and ``--pids-limit`` are mandatory and
    never come from the caller.

    There is intentionally **no** ``extra_image_args`` parameter: the
    LEAN launcher arg list is determined entirely by ``build_command``
    so a caller cannot tack on flags after the image reference.
    """
    limits.validate()
    _validate_hardening_flags(hardening_flags)
    if not workspace.workspace_dir.exists():
        raise RunnerConfigurationError(f"workspace directory does not exist: {workspace.workspace_dir}")
    if not workspace.workspace_dir.is_dir():
        raise RunnerConfigurationError(f"workspace path is not a directory: {workspace.workspace_dir}")

    image_reference = _require_image_in_allowlist(image_digest)
    podman = _require_podman()

    # Mandatory non-conditional flags. Any change here is a sandbox
    # change and must update the ADR in the same PR.
    #
    # ``--cap-drop=ALL`` (Phase 1b), ``--read-only`` (Phase 1c), and
    # ``--user`` (Phase 1c) are all proven safe at both the
    # podman-startup level (security-flag matrix) and the LEAN-
    # runtime level (E2E ``test_buy_and_hold_runs_with_*``). They
    # gate the Phase 4c arbitrary-user-source unlock — without them
    # the sandbox is weaker than the ADR's non-negotiables require.
    #
    # ``--read-only`` is viable because Phase 1c moved LEAN's
    # ObjectStore root from the image overlay
    # (``/Lean/Launcher/bin/Debug/storage``) to the workspace via
    # the ``object-store-root`` config key (see lean_config.py).
    # ``--user`` resolves dynamically via ``_container_user_spec``
    # so the container user matches the launcher's host UID on
    # Linux (otherwise POSIX permissions reject writes the
    # container makes to launcher-created workspace files).
    argv: list[str] = [
        podman,
        "run",
        "--rm",
        "--network=none",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        "--read-only",
        f"--user={_container_user_spec()}",
        f"--cpus={limits.cpus}",
        f"--memory={limits.memory_mb}m",
        f"--pids-limit={limits.pids_limit}",
        "-v",
        f"{workspace.workspace_dir}:{CONTAINER_WORKSPACE_MOUNT}:rw",
    ]
    # Optional hardening flags survive only if the security-flag matrix
    # proved the pinned image tolerates them. Already validated against
    # ALLOWED_HARDENING_TOKENS above.
    argv.extend(hardening_flags)
    argv.append(image_reference)
    # The LEAN launcher's first arg is always ``--config <path>`` so it
    # reads the workspace config, not the image-baked default. This is a
    # safety floor: forgetting it silently runs the default template
    # algorithm and the run looks "successful" with empty output.
    argv.extend(["--config", CONTAINER_LEAN_CONFIG_PATH])
    return RunnerPlan(image_reference=image_reference, argv=tuple(argv))


def _tail_text(buf: bytes, max_bytes: int) -> str:
    """Return the last ``max_bytes`` of a buffer decoded as best-effort
    UTF-8.

    LEAN can emit non-UTF-8 bytes in rare cases (binary tracebacks from
    a crashing native dep). ``errors="replace"`` keeps the launcher's
    own log readable rather than failing on decode.
    """
    tail = buf[-max_bytes:] if len(buf) > max_bytes else buf
    return tail.decode("utf-8", errors="replace")


def execute(plan: RunnerPlan, *, limits: RunLimits = DEFAULT_RUN_LIMITS) -> RunResult:
    """Spawn the container and return a structured result.

    The wall-clock timeout from ``limits`` is enforced here as the outer
    kill switch; LEAN-internal timeouts are independent and may fire
    earlier. ``log_tail`` is truncated to ``limits.log_tail_bytes`` so
    the launcher can persist + return logs without unbounded growth.
    """
    limits.validate()

    import time

    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            plan.argv,
            capture_output=True,
            timeout=limits.wall_clock_timeout_s,
            check=False,
        )
        stdout, stderr = completed.stdout, completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = e.stdout or b""
        stderr = e.stderr or b""
        exit_code = -1
    duration_ms = int((time.monotonic() - started) * 1000)

    # Merge for the tail; keep stderr last so the failure message is the
    # most likely thing the operator sees.
    combined = stdout + b"\n" + stderr if stderr else stdout
    return RunResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        log_tail=_tail_text(combined, limits.log_tail_bytes),
    )
