"""PRD #619-B B3 — pure helpers that compose the four ``engine_runtime``
blocks from raw engine state.

Lives next to ``LiveEngine`` (rather than in
``engine_runtime_publisher.py``) because the composition is engine-side
business logic; the publisher / aggregator stays a pure contract carrier.
Separating composition from the publisher keeps the publisher reusable
for non-engine producers (the daemon watchdog reads daemon_lease and
updates control_plane the same way).

ADR-0011 amendment (PRD #619-A) — the verdict-provider string is the
single input to ``broker_identity``; ``submission_capability`` and
``effective_posture`` are independent facts composed from the run's
declared submit mode and the readonly setting used to construct the
child. The composition rules are stable and the same code runs on
every producer tick.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.broker.ibkr.recovery_state_machine import RecoveryState
from app.engine.live.control_plane import read_daemon_lease
from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
)

# ---------------------------------------------------------------------------
# Verdict / capability / posture composition
# ---------------------------------------------------------------------------


def verdict_to_identity(
    verdict: str | None,
) -> Literal["PAPER_VERIFIED", "LIVE_DETECTED", "UNKNOWN"]:
    """Map the ADR-0011 verdict literal to the engine_runtime identity enum.

    ``paper-only`` → ``PAPER_VERIFIED``, ``unsafe`` → ``LIVE_DETECTED``,
    anything else (including ``None``) → ``UNKNOWN``.
    """
    if verdict == "paper-only":
        return "PAPER_VERIFIED"
    if verdict == "unsafe":
        return "LIVE_DETECTED"
    return "UNKNOWN"


def compose_capability(
    *, run_mode: str, readonly: bool
) -> Literal["PAPER_ORDERS_ENABLED", "READ_ONLY", "BLOCKED", "UNKNOWN"]:
    """Derive ``submission_capability`` from the durable child/run facts.

    ``run_mode`` is the declared ``submit_mode`` (``live_paper`` /
    ``shadow``) — empty / unrecognized values fall through to UNKNOWN.
    ``readonly`` is the actual setting passed to ``LiveEngine`` at
    construction.

    - ``live_paper`` + ``readonly=False`` → ``PAPER_ORDERS_ENABLED``
    - any mode + ``readonly=True`` → ``READ_ONLY``
    - ``shadow`` + ``readonly=False`` → ``READ_ONLY`` (shadow never
      submits regardless of readonly; treat as observation)
    - unrecognized mode → ``UNKNOWN``
    """
    if readonly:
        return "READ_ONLY"
    if run_mode == "live_paper":
        return "PAPER_ORDERS_ENABLED"
    if run_mode == "shadow":
        return "READ_ONLY"
    return "UNKNOWN"


def compose_posture(
    *,
    identity: Literal["PAPER_VERIFIED", "LIVE_DETECTED", "UNKNOWN"],
    capability: Literal["PAPER_ORDERS_ENABLED", "READ_ONLY", "BLOCKED", "UNKNOWN"],
) -> Literal["PAPER_EXECUTION", "PAPER_OBSERVATION", "UNSAFE", "UNKNOWN"]:
    """Compose ``effective_posture`` from identity and capability.

    Matches the ADR-0011 amendment table verbatim:

    - ``PAPER_VERIFIED`` + ``PAPER_ORDERS_ENABLED`` → ``PAPER_EXECUTION``
    - ``PAPER_VERIFIED`` + ``READ_ONLY`` → ``PAPER_OBSERVATION``
    - ``LIVE_DETECTED`` (any) → ``UNSAFE``
    - ``BLOCKED`` capability (any identity) → ``UNSAFE``
    - else → ``UNKNOWN``
    """
    if identity == "LIVE_DETECTED" or capability == "BLOCKED":
        return "UNSAFE"
    if identity == "PAPER_VERIFIED" and capability == "PAPER_ORDERS_ENABLED":
        return "PAPER_EXECUTION"
    if identity == "PAPER_VERIFIED" and capability == "READ_ONLY":
        return "PAPER_OBSERVATION"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------


def build_command_loop_block(
    *, heartbeat_at_ms: int, paused: bool
) -> CommandLoopBlock:
    """Synthesise the command-loop block from engine state.

    ``paused`` reflects the engine's ``self._paused`` flag — True →
    PAUSED, False → RUNNING. DRAINING / FAILED / IDLE are produced by
    other call sites (shutdown handler, exception path, pre-start).
    """
    return CommandLoopBlock(
        heartbeat_at_ms=heartbeat_at_ms,
        state="PAUSED" if paused else "RUNNING",
    )


def build_bar_loop_block(
    *,
    heartbeat_at_ms: int,
    latest_source_bar_ms: int | None,
    expected_interval_ms: int | None,
    source_state: Literal[
        "NOT_REQUESTED",
        "WAITING_FIRST_BAR",
        "ACTIVE",
        "NO_FIRST_BAR_TIMEOUT",
        "FAILED",
    ] = "NOT_REQUESTED",
    source: str | None = None,
    symbol: str | None = None,
    subscription_requested_at_ms: int | None = None,
    first_bar_deadline_ms: int | None = None,
    detail: str | None = None,
) -> BarLoopBlock:
    return BarLoopBlock(
        heartbeat_at_ms=heartbeat_at_ms,
        latest_source_bar_ms=latest_source_bar_ms,
        expected_interval_ms=expected_interval_ms,
        source_state=source_state,
        source=source,
        symbol=symbol,
        subscription_requested_at_ms=subscription_requested_at_ms,
        first_bar_deadline_ms=first_bar_deadline_ms,
        detail=detail,
    )


def build_broker_block(
    *,
    verdict_value: str | None,
    run_mode: str,
    readonly: bool,
    connection_state: Literal[
        "connected",
        "soft_lost",
        "subscriptions_stale",
        "degraded_data_farm",
        "reconnecting",
        "recovering",
        "hard_down",
        "disconnected",
        "disabled",
    ],
    recovery_state: RecoveryState | None,
    connection_epoch: int,
    client_id: int | None,
    connected_account: str | None,
    port_class: Literal["paper_port", "live_port", "unknown"],
    observation_at_ms: int,
    probe_completed_at_ms: int | None,
    reconnect_attempt: int,
) -> BrokerBlock:
    """Compose the full broker block from raw engine + client state.

    The pure composition rules live in ``compose_capability`` /
    ``compose_posture`` / ``verdict_to_identity`` so they remain
    independently testable. The engine producer just calls this once
    per verdict check.
    """
    identity = verdict_to_identity(verdict_value)
    capability = compose_capability(run_mode=run_mode, readonly=readonly)
    posture = compose_posture(identity=identity, capability=capability)
    return BrokerBlock(
        identity=identity,
        submission_capability=capability,
        effective_posture=posture,
        connection_state=connection_state,
        recovery_state=recovery_state,
        connection_epoch=connection_epoch,
        client_id=client_id,
        connected_account=connected_account,
        port_class=port_class,
        observation_at_ms=observation_at_ms,
        probe_completed_at_ms=probe_completed_at_ms,
        reconnect_attempt=reconnect_attempt,
    )


def build_control_plane_block_from_lease(
    artifacts_root: Path | None, *, now_ms: int
) -> ControlPlaneBlock:
    """Read the current daemon lease and project it into a control-plane block.

    No lease on disk yet, or a missing artifacts_root, yields a block
    with ``observed_daemon_boot_id=None`` and ``lease_observed_at_ms=now_ms``.
    The watchdog (619-B B5) will replace this single startup-time read
    with periodic refreshes; for B3 we initialise once so the
    publisher has a complete snapshot to emit.
    """
    if artifacts_root is None:
        return ControlPlaneBlock(lease_observed_at_ms=now_ms, observed_daemon_boot_id=None)
    lease = read_daemon_lease(artifacts_root)
    if lease is None:
        return ControlPlaneBlock(lease_observed_at_ms=now_ms, observed_daemon_boot_id=None)
    return ControlPlaneBlock(
        lease_observed_at_ms=now_ms,
        observed_daemon_boot_id=lease.boot_id,
    )
