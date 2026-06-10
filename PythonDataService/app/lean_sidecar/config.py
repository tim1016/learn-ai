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

# The image we run is a thin derivative of upstream quantconnect/lean.
# See:
#
# - ``PythonDataService/lean_sidecar/Dockerfile.arm64-dotnet109``:
#   arm64, lean_version 17748, upstream LEAN payload unchanged, .NET
#   Host/Runtime patched from 10.0.2 to 10.0.9 to avoid the AppleHV
#   SME/SVE CoreCLR SIGILL on wider windows.
# - ``PythonDataService/lean_sidecar/Dockerfile.amd64``: amd64,
#   lean_version 17764, opt-in only for native x86_64 execution
#   surfaces. Rosetta on this Apple Silicon host crashes LEAN's JIT'd
#   workload, so amd64 is deliberately not the default.
#
# The arm64 derivatives relax ``/root`` to mode 0755 so the Phase-1c
# ``--user=10001:10001`` sandbox can traverse to
# ``/root/.dotnet/dotnet``. Upstream
# ``quantconnect/lean@sha256:4934c22c…`` ships ``/root`` as 0700 which
# is incompatible with non-root execution.
LEAN_IMAGE_REPO = "localhost/learn-ai/lean-sandbox"

# Resolved by Phase 1 spike on first successful pull. See
# docs/architecture/lean-sidecar-lab.md §"Runner choice" for the policy.
#
# ``PINNED_LEAN_IMAGE_DIGEST`` is the default pin the orchestrator
# passes to the launcher. The arm64 .NET-10.0.9 derivative is the
# default: it keeps the pinned LEAN engine payload on lean_version
# 17748 and fixes the wide-window AppleHV SIGILL by side-by-side
# installing the patched .NET Host/Runtime 10.0.9 into /root/.dotnet.
#
# ``PINNED_LEAN_IMAGE_DIGEST_AMD64`` is published as opt-in for
# environments where the amd64 LEAN build can actually run (native
# Linux/amd64, or a future Rosetta release that translates this
# image's JIT'd code without segfaulting). It is NOT the default —
# the 2026-06-09 empirical bisection on this host (podman applehv
# rootless, Rosetta enabled) showed Rosetta 2 consistently
# segfaulting LEAN's JIT'd code path (``qemu: uncaught target signal
# 11``) during ``TextSubscriptionDataSourceReader.SetCacheSize``, and
# also producing an unrelated managed NRE
# (``Newtonsoft.Json.Converters.StringEnumConverter()``) inside
# ``MarketHoursDatabase.FromFile()`` on the staged-workspace path.
# Multiple DOTNET_* env combinations (ReadyToRun=0,
# TieredCompilation=0, gcServer=0, EnableHWIntrinsic=0,
# JitDisableSimdHWIntrinsic=1) did not move the failure point.
#
# The amd64 image is also the x86_64-arch closest reproduction of the
# Windows-validated SPY EMA-crossover bit-exact baseline at
# ``app/engine/tests/fixtures/spy_lean_trades.csv``; once an
# execution surface for it exists, the parity-against-Windows claim
# can be re-validated end-to-end.
PINNED_LEAN_IMAGE_DIGEST_AMD64: str | None = "sha256:bdb7c7aa3bd5f196905442706f9ebd6d22de08e21cf6ac5cc74b621690005a75"
PINNED_LEAN_IMAGE_DIGEST_ARM64: str | None = "sha256:0b8d4e381b63daaa4cebbea7af294cc5b140793a6fd13f8c9cfd63ef2a2fb24d"

PINNED_LEAN_IMAGE_DIGEST: str | None = PINNED_LEAN_IMAGE_DIGEST_ARM64

ALLOWED_IMAGE_DIGESTS: frozenset[str] = frozenset(
    d for d in (PINNED_LEAN_IMAGE_DIGEST_AMD64, PINNED_LEAN_IMAGE_DIGEST_ARM64) if d is not None
)

# Per-digest container platform. The runner derives ``--platform <value>``
# from this mapping at ``podman run`` time so an image pulled with one
# platform is always run on that same platform — eliminating a class of
# silent mismatches where podman warns ("image platform (linux/amd64)
# does not match the expected platform (linux/arm64)") but the
# container runs anyway with surprising semantics. Digests not keyed
# here run on the host's native platform (the default podman behavior,
# no ``--platform`` flag passed).
DIGEST_PLATFORMS: dict[str, str] = {
    d: "linux/amd64"
    for d in (PINNED_LEAN_IMAGE_DIGEST_AMD64,)
    if d is not None
}


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
    # 3 GiB remains the default wide-window envelope for LEAN under
    # podman applehv. The prior exit-132/SIGILL investigation is now
    # resolved by the arm64 .NET-10.0.9 derivative pinned above, not by
    # the ``HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX``
    # env flags.
    memory_mb=3072,
    pids_limit=512,
    # 2 hours. Sized to match the router's ``_MAX_TRADING_DAYS = 504``
    # (~2 calendar years of US-equity minute data, Polygon.io Starter
    # plan's history depth). Empirical envelope: LEAN minute backtests
    # on EMA/RSI-class indicators against real Polygon data take ~1-3
    # minutes per simulated month, so 24 months ≈ 24-72 minutes wall
    # clock; 7200s leaves margin without indulging a runaway loop.
    # The P1.1 cidfile kill switch (runner.py) fires at this deadline.
    wall_clock_timeout_s=7200,
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
