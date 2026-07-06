"""PRD #619-B B7 — backend freshness evaluation over engine_runtime.json.

Pure function. Given the latest ``EngineRuntimeSnapshot`` + ``now_ms`` +
a ``RuntimeFreshnessConfig``, returns a per-domain ``RuntimeFreshness``
the operator-surface composer can consume verbatim. No I/O, no
calendar lookups inside this module — when session-awareness lands as
a follow-up, the caller injects a ``SessionState`` provider.

Per PRD §B:

- **command_loop** stale > 3s → posture demoted to last-known; all
  normal instance actions disabled.
- **broker** probe stale > 25s → effective_posture becomes UNKNOWN.
- **bar_loop** session/resolution-aware: RTH allowed lag =
  ``max(2 * expected_interval_ms, source_min_ms)``; outside session
  ``NOT_APPLICABLE``; halted market ``DEGRADED`` / ``UNKNOWN`` via
  calendar evidence. This module ships the threshold-based path; the
  session-aware overlay is wired by passing a non-None
  ``session_state`` (``RTH_OPEN`` | ``CLOSED`` | ``HALTED``).
- **control_plane** stale (no lease, expired, or boot_id mismatch
  observed before this evaluation) → operator action surface
  demotes; this module reports the freshness of the last observation
  but never decides the action-matrix (that lives in operator_surface).

Thresholds are server config with validated defaults — Angular never
authors them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.engine_runtime import EngineRuntimeSnapshot

DomainFreshnessState = Literal[
    "FRESH",
    "STALE",
    "NOT_APPLICABLE",
    "UNKNOWN",
    "DEGRADED",
]

SessionState = Literal["RTH_OPEN", "CLOSED", "HALTED"]
EngineEffectivePosture = Literal[
    "PAPER_EXECUTION",
    "PAPER_OBSERVATION",
    "UNSAFE",
    "UNKNOWN",
]


class RuntimeFreshnessConfig(BaseModel):
    """Server-authored thresholds for the freshness evaluator.

    Defaults match the PRD §B values. Override via FastAPI settings
    when a deployment needs tighter/looser bounds; never override from
    Angular.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_loop_stale_threshold_ms: int = Field(default=3_000, ge=1)
    broker_probe_stale_threshold_ms: int = Field(default=25_000, ge=1)
    bar_loop_source_min_ms: int = Field(default=30_000, ge=1)
    control_plane_stale_threshold_ms: int = Field(default=5_000, ge=1)


@dataclass(frozen=True)
class DomainFreshness:
    """Per-domain freshness verdict + diagnostics."""

    state: DomainFreshnessState
    age_ms: int | None
    stale_reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeFreshness:
    """Composed runtime-freshness verdict the operator surface consumes.

    ``posture_demoted`` is True iff the data plane should fall back to
    "last-known" rendering for the instance. The triggers per PRD §B
    are: command_loop stale, broker probe stale, or control_plane
    stale. The bar_loop status alone never demotes posture (a closed
    market or halted symbol is not a posture event).
    """

    command_loop: DomainFreshness
    broker: DomainFreshness
    bar_loop: DomainFreshness
    control_plane: DomainFreshness
    posture_demoted: bool
    effective_posture: EngineEffectivePosture = "UNKNOWN"


def unavailable_runtime_freshness(
    reason_code: Literal[
        "ENGINE_RUNTIME_MISSING",
        "ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE",
    ],
) -> RuntimeFreshness:
    """Fail-closed freshness when a live run has no readable runtime artifact."""
    def _unavailable_domain() -> DomainFreshness:
        return DomainFreshness(
            state="UNKNOWN",
            age_ms=None,
            stale_reason_codes=[reason_code],
        )

    return RuntimeFreshness(
        command_loop=_unavailable_domain(),
        broker=_unavailable_domain(),
        bar_loop=_unavailable_domain(),
        control_plane=_unavailable_domain(),
        posture_demoted=True,
        effective_posture="UNKNOWN",
    )


def runtime_freshness_reason_codes(freshness: RuntimeFreshness) -> list[str]:
    """Return stable, de-duplicated reason codes in domain order."""
    codes: list[str] = []
    for domain in (
        freshness.command_loop,
        freshness.broker,
        freshness.bar_loop,
        freshness.control_plane,
    ):
        for code in domain.stale_reason_codes:
            if code not in codes:
                codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# Per-domain evaluators — kept as small helpers so the composition is
# trivial to read.
# ---------------------------------------------------------------------------


def _evaluate_command_loop(
    snapshot: EngineRuntimeSnapshot, *, now_ms: int, config: RuntimeFreshnessConfig
) -> DomainFreshness:
    age = now_ms - snapshot.command_loop.heartbeat_at_ms
    if age > config.command_loop_stale_threshold_ms:
        return DomainFreshness(
            state="STALE",
            age_ms=age,
            stale_reason_codes=["COMMAND_LOOP_STALE"],
        )
    return DomainFreshness(state="FRESH", age_ms=age)


def _evaluate_broker(
    snapshot: EngineRuntimeSnapshot, *, now_ms: int, config: RuntimeFreshnessConfig
) -> DomainFreshness:
    probe_at = snapshot.broker.probe_completed_at_ms
    if probe_at is None:
        return DomainFreshness(
            state="UNKNOWN",
            age_ms=None,
            stale_reason_codes=["BROKER_PROBE_MISSING"],
        )
    age = now_ms - probe_at
    if age > config.broker_probe_stale_threshold_ms:
        return DomainFreshness(
            state="UNKNOWN",  # PRD §B: stale probe → effective_posture UNKNOWN
            age_ms=age,
            stale_reason_codes=["BROKER_PROBE_STALE"],
        )
    return DomainFreshness(state="FRESH", age_ms=age)


def _evaluate_bar_loop(
    snapshot: EngineRuntimeSnapshot,
    *,
    now_ms: int,
    config: RuntimeFreshnessConfig,
    session_state: SessionState | None,
) -> DomainFreshness:
    # Session-aware short-circuits first.
    if session_state == "CLOSED":
        return DomainFreshness(
            state="NOT_APPLICABLE",
            age_ms=None,
            stale_reason_codes=["BAR_LOOP_SESSION_CLOSED"],
        )
    if session_state == "HALTED":
        return DomainFreshness(
            state="DEGRADED",
            age_ms=None,
            stale_reason_codes=["BAR_LOOP_SESSION_HALTED"],
        )

    heartbeat_age = now_ms - snapshot.bar_loop.heartbeat_at_ms

    # Allowed lag derives from the expected bar interval. ``None`` (no
    # expected interval supplied) falls back to the source_min.
    expected = snapshot.bar_loop.expected_interval_ms
    allowed_lag = max(
        (expected or 0) * 2,
        config.bar_loop_source_min_ms,
    )

    latest = snapshot.bar_loop.latest_source_bar_ms
    latest_age = (now_ms - latest) if latest is not None else None
    stale_reasons: list[str] = []
    if latest is None and snapshot.bar_loop.source_state == "NO_FIRST_BAR_TIMEOUT":
        stale_reasons.append("BAR_LOOP_FIRST_BAR_TIMEOUT")
    if heartbeat_age > allowed_lag:
        if latest is None and "BAR_LOOP_FIRST_BAR_TIMEOUT" not in stale_reasons:
            stale_reasons.append("BAR_LOOP_SOURCE_MISSING")
        else:
            stale_reasons.append("BAR_LOOP_HEARTBEAT_STALE")
    if latest is not None and latest_age is not None and latest_age > allowed_lag:
        stale_reasons.append("BAR_LOOP_LATEST_BAR_STALE")

    if stale_reasons:
        return DomainFreshness(
            state="STALE", age_ms=heartbeat_age, stale_reason_codes=stale_reasons
        )
    return DomainFreshness(state="FRESH", age_ms=heartbeat_age)


def _evaluate_control_plane(
    snapshot: EngineRuntimeSnapshot,
    *,
    now_ms: int,
    config: RuntimeFreshnessConfig,
) -> DomainFreshness:
    age = now_ms - snapshot.control_plane.lease_observed_at_ms
    reasons: list[str] = []
    state: DomainFreshnessState = "FRESH"
    if age > config.control_plane_stale_threshold_ms:
        state = "STALE"
        reasons.append("CONTROL_PLANE_LEASE_STALE")
    # Boot-id mismatch is reported by the child watchdog (619-B B5) as
    # ``ORPHANED_CONTROL_PLANE`` on the daemon-side classifier. Here we
    # only flag the observed mismatch when the engine's
    # ``expected_daemon_boot_id`` differs from the lease's
    # ``observed_daemon_boot_id``.
    if (
        snapshot.expected_daemon_boot_id is not None
        and snapshot.control_plane.observed_daemon_boot_id is not None
        and snapshot.expected_daemon_boot_id
        != snapshot.control_plane.observed_daemon_boot_id
    ):
        # Mismatch is a degraded condition even if the observation is
        # fresh.
        state = "DEGRADED"
        reasons.append("CONTROL_PLANE_BOOT_ID_MISMATCH")
    return DomainFreshness(state=state, age_ms=age, stale_reason_codes=reasons)


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def evaluate_runtime_freshness(
    snapshot: EngineRuntimeSnapshot,
    *,
    now_ms: int,
    config: RuntimeFreshnessConfig | None = None,
    session_state: SessionState | None = None,
) -> RuntimeFreshness:
    """Pure evaluator. Same inputs always produce the same output.

    ``session_state`` is the caller-supplied trading-session signal
    (RTH_OPEN / CLOSED / HALTED). When ``None``, the bar_loop is
    evaluated purely by threshold — the session-aware overlay is
    skipped. Wiring a real session provider is a follow-up to this PR.
    """
    cfg = config or RuntimeFreshnessConfig()
    command_loop = _evaluate_command_loop(snapshot, now_ms=now_ms, config=cfg)
    broker = _evaluate_broker(snapshot, now_ms=now_ms, config=cfg)
    bar_loop = _evaluate_bar_loop(
        snapshot, now_ms=now_ms, config=cfg, session_state=session_state
    )
    control_plane = _evaluate_control_plane(snapshot, now_ms=now_ms, config=cfg)

    posture_demoted = (
        command_loop.state in {"STALE", "UNKNOWN"}
        or broker.state in {"STALE", "UNKNOWN"}
        or control_plane.state in {"STALE", "DEGRADED"}
    )

    return RuntimeFreshness(
        command_loop=command_loop,
        broker=broker,
        bar_loop=bar_loop,
        control_plane=control_plane,
        posture_demoted=posture_demoted,
        effective_posture=(
            snapshot.broker.effective_posture
            if broker.state == "FRESH"
            else "UNKNOWN"
        ),
    )
