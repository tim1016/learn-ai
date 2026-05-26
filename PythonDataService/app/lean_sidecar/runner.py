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

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.lean_sidecar.config import (
    ALLOWED_IMAGE_DIGESTS,
    DEFAULT_RUN_LIMITS,
    LEAN_IMAGE_REPO,
    RunLimits,
)
from app.lean_sidecar.workspace import Workspace

logger = logging.getLogger(__name__)


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


class HardeningProfile(StrEnum):
    """Reviewer-suggested typed wrapper over the ``hardening_flags``
    argv tokens. The enum surface trades flexibility (any token from the
    allow-list) for safety (caller cannot misorder the tokens that pair
    with ``--tmpfs``, cannot pass an unknown value spec, cannot smuggle
    extra flags).

    Each profile expands deterministically to a fixed tuple of argv
    tokens via ``HARDENING_PROFILE_TOKENS``. Callers may use either
    interface — ``build_command(hardening_profile=...)`` or
    ``build_command(hardening_flags=(...))`` — but not both in the same
    call (the validator rejects it). New code should use the profile
    enum; the raw-tokens path stays for backwards compatibility with
    the security-flag matrix tests until they migrate.
    """

    # No optional flags — only the Phase 1c mandatory shape applies.
    MINIMAL = "minimal"
    # ``--tmpfs /tmp:rw,noexec,nosuid,size=256m`` — the spec used by
    # the trusted-sample E2E.
    WITH_TMPFS_256M = "with_tmpfs_256m"
    # Smaller variant for memory-tight environments.
    WITH_TMPFS_64M = "with_tmpfs_64m"


HARDENING_PROFILE_TOKENS: dict[HardeningProfile, tuple[str, ...]] = {
    HardeningProfile.MINIMAL: (),
    HardeningProfile.WITH_TMPFS_256M: ("--tmpfs", "/tmp:rw,noexec,nosuid,size=256m"),
    HardeningProfile.WITH_TMPFS_64M: ("--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"),
}


def tokens_for_profile(profile: HardeningProfile) -> tuple[str, ...]:
    """Return the argv tokens a profile expands to.

    Exposed so the launcher service can echo the resolved tokens into
    the launcher.log audit trail without re-implementing the mapping.
    """
    return HARDENING_PROFILE_TOKENS[profile]


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

    On Linux the spec is the launcher process's own UID/GID. Paired
    with :func:`_userns_flags` (which emits ``--userns=keep-id`` on
    rootless podman), this guarantees the container's effective user
    matches the *host-side* owner of the workspace files the launcher
    created.

    Why the pairing matters on rootless podman: without
    ``--userns=keep-id``, container UID 1000 maps to a sub-UID from
    ``/etc/subuid`` (e.g., 100999) rather than to the host's 1000.
    The bind-mounted workspace shows up inside the container as owned
    by container UID 0 (because host 1000 → container 0 under the
    default mapping), and the UID-100999 process is denied writes,
    crashing LEAN inside ``BacktestingResultHandler.Exit()`` with
    ``UnauthorizedAccessException`` on every ``output/*`` write.

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


def _is_rootless_podman(podman_path: str) -> bool:
    """Detect whether the discovered podman is running rootless.

    Returns ``False`` on any probe failure (podman not on PATH, exit
    non-zero, malformed output). The downstream :func:`_userns_flags`
    then omits ``--userns=keep-id`` which is the safe default for
    rootful podman (where the flag errors with "keep-id is only
    supported in rootless mode") and for non-Linux hosts.

    Not cached on purpose: the probe runs once per ``build_command``
    call (once per LEAN backtest, which takes seconds), the ~tens-of-ms
    cost is negligible against that baseline, and avoiding a cache
    keeps tests trivial — a ``monkeypatch.setattr(runner,
    "_is_rootless_podman", ...)`` reliably overrides per-test without
    a separate cache-reset step.
    """
    try:
        completed = subprocess.run(
            [podman_path, "info", "--format", "{{.Host.Security.Rootless}}"],
            capture_output=True,
            text=True,
            # ``podman info`` is fast (~1-2s) once the user namespace is
            # initialised, but the very first invocation in a fresh
            # session can take noticeably longer while the storage
            # driver and network stack come up. 15s gives margin
            # without making a real podman misconfiguration hang the
            # launcher for unbounded time.
            timeout=15,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.stdout.strip().lower() == "true"


def _userns_flags(podman_path: str) -> tuple[str, ...]:
    """Return ``('--userns=keep-id',)`` when rootless, ``()`` otherwise.

    Rationale: on rootless podman, the default user-namespace mapping
    routes the launcher's host UID (e.g., 1000) to container UID 0,
    and container UIDs >0 to a high sub-UID range from ``/etc/subuid``.
    The ``--user=<host-uid>:<host-gid>`` spec from
    :func:`_container_user_spec` therefore runs LEAN as a sub-UID with
    no write access to the workspace files (owned by host UID 1000,
    seen inside the container as owned by container UID 0).

    ``--userns=keep-id`` overrides the mapping to make container UID
    == host UID for the launcher's identity, restoring write access.

    The flag is unsupported on rootful podman (errors at parse time);
    omit it there. Detection is one-shot at module load via
    :func:`_is_rootless_podman`.
    """
    return ("--userns=keep-id",) if _is_rootless_podman(podman_path) else ()


@dataclass(frozen=True, slots=True)
class RunnerPlan:
    """The fully constructed `podman run` argv plus the resolved digest.

    Returned by :func:`build_command` so tests can assert on the exact
    invocation without spawning a container, and the launcher can log
    the planned command to ``launcher.log`` before execution.

    ``cidfile_path`` is the host-side path podman writes the container
    ID to on creation. ``execute`` reads it on timeout so the outer
    kill switch can actually stop the LEAN container — Reviewer P1.1:
    ``subprocess.run(..., timeout=...)`` only sends SIGKILL to the
    podman CLIENT, not the container; without the cidfile + a
    follow-up ``podman stop`` the container kept running past the
    wall-clock timeout.
    """

    image_reference: str
    argv: tuple[str, ...]
    cidfile_path: Path


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
    hardening_profile: HardeningProfile | None = None,
) -> RunnerPlan:
    """Construct the `podman run` argv for this workspace + image.

    The returned argv is the *exact* command the runner will execute.
    Tests assert on it so a future refactor cannot silently widen the
    sandbox.

    Two ways to specify optional hardening:

    - ``hardening_profile=HardeningProfile.WITH_TMPFS_256M`` — preferred
      for new code. The enum value expands deterministically to a fixed
      tuple of argv tokens via :data:`HARDENING_PROFILE_TOKENS`.
    - ``hardening_flags=("--tmpfs", "...")`` — the original raw-token
      interface. Every token must be in :data:`ALLOWED_HARDENING_TOKENS`;
      an empty tuple is the safe default.

    Passing BOTH is rejected with ``RunnerConfigurationError`` — there is
    no merge semantic that wouldn't surprise someone. Pick one.

    ``--cap-drop=ALL`` and ``--pids-limit`` are mandatory and never come
    from the caller. There is intentionally **no** ``extra_image_args``
    parameter: the LEAN launcher arg list is determined entirely by
    ``build_command`` so a caller cannot tack on flags after the image
    reference.
    """
    limits.validate()
    if hardening_profile is not None and hardening_flags:
        raise RunnerConfigurationError(
            "build_command accepts hardening_profile OR hardening_flags, not both. "
            f"Got profile={hardening_profile} AND flags={hardening_flags}."
        )
    if hardening_profile is not None:
        # Expand to tokens; skip the allow-list check because the
        # profile mapping is the allow-list — by construction, every
        # value in HARDENING_PROFILE_TOKENS is already in
        # ALLOWED_HARDENING_TOKENS (asserted by a regression test).
        hardening_flags = tokens_for_profile(hardening_profile)
    _validate_hardening_flags(hardening_flags)
    if not workspace.workspace_dir.exists():
        raise RunnerConfigurationError(f"workspace directory does not exist: {workspace.workspace_dir}")
    if not workspace.workspace_dir.is_dir():
        raise RunnerConfigurationError(f"workspace path is not a directory: {workspace.workspace_dir}")

    image_reference = _require_image_in_allowlist(image_digest)
    podman = _require_podman()

    # ``--cidfile`` writes the container id to a host-side file on
    # creation. Used by ``execute`` to issue ``podman stop`` / ``podman
    # rm`` on wall-clock timeout so the container actually dies — see
    # the RunnerPlan docstring for the Reviewer P1.1 context. Path
    # lives under ``workspace.launcher_dir`` (same directory as
    # ``launcher.log``) so a manual operator cleanup ``rm -rf
    # <workspace>/launcher/`` reclaims all launcher-owned scratch.
    cidfile_path = workspace.launcher_dir / "cidfile"
    # ``--cidfile`` refuses to overwrite an existing file ("error
    # opening cidfile: file exists"). A retried run with the same
    # workspace would fail on this check; remove the stale file
    # up-front. ``missing_ok=True`` because the launcher_dir is freshly
    # created on first run.
    cidfile_path.unlink(missing_ok=True)

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
        *_userns_flags(podman),
        f"--user={_container_user_spec()}",
        f"--cpus={limits.cpus}",
        f"--memory={limits.memory_mb}m",
        f"--pids-limit={limits.pids_limit}",
        f"--cidfile={cidfile_path}",
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
    return RunnerPlan(
        image_reference=image_reference,
        argv=tuple(argv),
        cidfile_path=cidfile_path,
    )


class KillReason(StrEnum):
    """Why ``_kill_container_via_cidfile`` was invoked.

    Threaded into the kill helper so the launcher can return the right
    operator-facing reason on each kill path:

    - ``WALL_CLOCK_TIMEOUT`` — ``subprocess.run(..., timeout=...)`` fired
      in ``execute()``; the wall-clock deadline elapsed (PR #280).
    - ``WORKSPACE_MAX_MB_EXCEEDED`` — the in-flight workspace-cap poller
      observed an overrun during the run (P1.4).

    A single "killed" signal collapses these two failure modes; the
    enum is the discriminator. No string magic — callers pass the enum
    member by name.
    """

    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
    WORKSPACE_MAX_MB_EXCEEDED = "workspace_max_mb_exceeded"


def _kill_container_via_cidfile(
    cidfile_path: Path,
    *,
    reason: KillReason,
) -> None:
    """Stop + remove the container whose id was written to ``cidfile_path``.

    Called from two paths:

    1. ``execute()`` on ``TimeoutExpired`` (``reason=WALL_CLOCK_TIMEOUT``)
       — the ADR's outer kill switch.
    2. ``WorkspacePoller`` on workspace-cap overrun
       (``reason=WORKSPACE_MAX_MB_EXCEEDED``) — P1.4's in-flight
       enforcement.

    Read failures, missing cidfile, or podman errors are logged +
    swallowed: the caller is already in an error path, and surfacing a
    secondary exception here would mask the real kill signal.

    ``podman stop --time=5`` sends SIGTERM and waits up to 5 seconds
    before SIGKILL. ``podman rm`` cleans up the stopped container so
    a leftover doesn't accumulate; ``--rm`` on ``podman run`` would
    have removed it on a normal exit but does not fire when the
    container is killed externally.
    """
    podman = shutil.which("podman")
    if podman is None:
        logger.warning(
            "cannot kill container (%s): podman not on PATH (this should not happen — the run started)",
            reason.value,
        )
        return
    try:
        if not cidfile_path.exists():
            # The container never started (timeout fired during
            # podman's own startup, before it wrote the cidfile).
            # Nothing to kill.
            logger.info(
                "no cidfile at %s (%s); container likely never started",
                cidfile_path,
                reason.value,
            )
            return
        cid = cidfile_path.read_text(encoding="utf-8").strip()
        if not cid:
            logger.warning(
                "cidfile %s exists but is empty (%s); container id unknown",
                cidfile_path,
                reason.value,
            )
            return
    except OSError as e:
        logger.warning("could not read cidfile %s (%s): %s", cidfile_path, reason.value, e)
        return

    logger.info("killing container %s: reason=%s", cid, reason.value)
    for action in ("stop", "rm"):
        cmd = [podman, action, "--time=5", cid] if action == "stop" else [podman, action, cid]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            logger.warning("podman %s %s timed out after 15s (%s)", action, cid, reason.value)
            continue
        if result.returncode != 0:
            # ``stop`` can fail because the container already exited
            # (race with TimeoutExpired); ``rm`` can fail because
            # ``--rm`` on podman run already removed it. Log but
            # don't escalate — the goal is best-effort cleanup.
            logger.info(
                "podman %s %s returned %d (%s): %s",
                action,
                cid,
                result.returncode,
                reason.value,
                result.stderr.strip(),
            )


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
        # Reviewer P1.1: ``subprocess.run(..., timeout=...)`` only
        # killed the podman CLIENT process; the LEAN container kept
        # running past the wall-clock deadline (no outer kill switch
        # despite the ADR claiming one). Read the cidfile podman wrote
        # at container creation and explicitly stop + remove the
        # container. Pass the kill reason explicitly so the launcher
        # can return ``wall_clock_timeout`` rather than collapsing it
        # with P1.4's workspace-cap kill path.
        _kill_container_via_cidfile(plan.cidfile_path, reason=KillReason.WALL_CLOCK_TIMEOUT)
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
