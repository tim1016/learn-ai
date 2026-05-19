"""LEAN Sidecar Lab configuration constants and types.

Pinned values that the rest of the package depends on. Image digest is
resolved in Phase 1 by ``scripts/lean_sidecar_pin_image.py`` and written
here, then echoed into ``docs/architecture/lean-sidecar-lab.md``.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Runner choice".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Image allow-list
# ---------------------------------------------------------------------------
#
# Every value here is a ``sha256:...`` digest. The launcher refuses to run
# any image not in this set. ``LEAN_IMAGE_DIGEST`` is the current pinned
# default for Phase 1; older digests may be retained here while an upgrade
# is being validated so reconciliation fixtures remain reproducible.
#
# The digest is populated by ``scripts/lean_sidecar_pin_image.py`` after
# the image is pulled. Until then, a placeholder of ``None`` forces the
# tests to skip with a clear message rather than silently running an
# unpinned image.

LEAN_IMAGE_REPO = "quantconnect/lean"

# Resolved by Phase 1 spike on first successful pull. See
# docs/architecture/lean-sidecar-lab.md §"Runner choice" for the policy.
PINNED_LEAN_IMAGE_DIGEST: str | None = "sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c"

ALLOWED_IMAGE_DIGESTS: frozenset[str] = frozenset(d for d in (PINNED_LEAN_IMAGE_DIGEST,) if d is not None)


# ---------------------------------------------------------------------------
# Default per-run limits
# ---------------------------------------------------------------------------
#
# The launcher refuses to run without explicit limits; these are the
# defaults the data-plane uses when a request does not override them.
# Authority: docs/architecture/lean-sidecar-lab.md §"Wall-clock timeout"
# and §"Container execution boundary".


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Per-run resource ceilings. Every field is mandatory.

    All fields together form the contract the launcher enforces before
    issuing ``podman run``; missing or non-positive values cause an
    immediate refusal (see ``LauncherValidationError``).
    """

    cpus: float
    memory_mb: int
    pids_limit: int
    wall_clock_timeout_s: int
    workspace_max_mb: int
    log_tail_bytes: int

    def validate(self) -> None:
        """Reject any non-positive ceiling.

        Defensive: callers can construct ``RunLimits`` with any values,
        and the launcher boundary calls this before launch. Both layers
        catching the same class of bug is intentional.
        """
        for name, value in (
            ("cpus", self.cpus),
            ("memory_mb", self.memory_mb),
            ("pids_limit", self.pids_limit),
            ("wall_clock_timeout_s", self.wall_clock_timeout_s),
            ("workspace_max_mb", self.workspace_max_mb),
            ("log_tail_bytes", self.log_tail_bytes),
        ):
            if value is None or value <= 0:
                raise ValueError(f"RunLimits.{name} must be positive, got {value!r}")


DEFAULT_RUN_LIMITS = RunLimits(
    cpus=2.0,
    memory_mb=2048,
    pids_limit=512,
    wall_clock_timeout_s=120,
    workspace_max_mb=512,
    log_tail_bytes=8 * 1024 * 1024,
)


# ---------------------------------------------------------------------------
# Per-request input ceilings
# ---------------------------------------------------------------------------
#
# Authority: lean-sidecar-lab.md §"Wall-clock timeout" final bullet.

MAX_ALGORITHM_SOURCE_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Workspace-cap poller
# ---------------------------------------------------------------------------
#
# The interval at which the in-flight workspace-cap poller walks the
# workspace looking for ``workspace_max_mb`` overruns. Intentionally a
# module-level constant, NOT a ``RunLimits`` field — the interval IS the
# overshoot budget; making it caller-settable would let a caller widen
# the budget unsafely.
#
# Tuning happens by improving the walk (``os.scandir`` vs ``du -sb``),
# not by lengthening the interval.
#
# Authority: docs/handoffs/2026-05-18-design-p1-4-live-workspace-cap-v2.md.

_WORKSPACE_POLL_INTERVAL_S: float = 1.0


# ---------------------------------------------------------------------------
# Artifacts root
# ---------------------------------------------------------------------------
#
# Every run owns a fresh directory under ``ARTIFACTS_ROOT / "lean-sidecar"
# / <run_id>``. The launcher refuses any host path that does not resolve
# strictly inside this root after symlink resolution.

DEFAULT_ARTIFACTS_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "lean-sidecar"


# The mandatory and allow-listed-optional podman flags now live in
# ``app.lean_sidecar.runner`` alongside the argv-construction code that
# uses them. Keeping the lists next to the code that enforces them
# prevents the documentation-vs-behavior drift we had during Phase 1a
# when the constants were here but unused.
