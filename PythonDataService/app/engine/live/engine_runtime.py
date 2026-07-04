"""PRD #619-B — per-run engine runtime snapshot.

The ``engine_runtime.json`` artifact lives at
``artifacts/live_runs/<run_id>/engine_runtime.json`` and carries every
fact the data plane needs to evaluate per-domain freshness without
inferring liveness from the absence of bar events. Four independent
domain blocks (command loop, broker, bar loop, control plane) each
stamp their own ``heartbeat_at_ms`` / ``observation_at_ms`` so a quiet
broker session does not look like a halted bar loop and vice versa.

Module split for testability (see ``tests/engine/live/test_engine_runtime_publisher.py``):

1. **This file** — the typed schema + the atomic file writer. Pure: no
   I/O beyond the writer's single ``tmp + fsync + replace`` step; no
   ordering enforcement (``snapshot_seq`` monotonicity is the caller's
   responsibility).
2. **``engine_runtime_aggregator.py``** — the in-memory state
   aggregator (619-B follow-up). Domain producers push slot updates;
   ``produce_snapshot()`` reads a coherent value.
3. **``engine_runtime_publisher.py``** — the single serialized
   publisher task (619-B follow-up). 1Hz steady-state writes; immediate
   flush on safety transitions; monotonic ``snapshot_seq``.

Splitting the three pieces means the concurrent producer race tests
exercise the aggregator + publisher together while the atomic-write
contract is testable in isolation.

All timestamps are ``int64`` ms UTC at the artifact boundary per
``.claude/rules/numerical-rigor.md``. No ``datetime`` / ISO strings on
the wire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.ibkr.recovery_state_machine import RecoveryState
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact

ENGINE_RUNTIME_FILENAME = "engine_runtime.json"
ENGINE_RUNTIME_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Domain blocks — each carries its own heartbeat/observation timestamp so the
# data plane can evaluate freshness per-domain. A single ``written_at_ms`` on
# the envelope is NOT sufficient: a stalled bar loop is invisible if the
# command poll task is still updating the envelope timestamp.
# ---------------------------------------------------------------------------


class CommandLoopBlock(BaseModel):
    """The engine's command-channel poll loop.

    ``heartbeat_at_ms`` is updated on every poll tick (1Hz cadence in
    ``live_engine.py:_command_poll_loop``). ``state`` is the engine's
    high-level disposition the cockpit renders.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    heartbeat_at_ms: int = Field(ge=0)
    state: Literal["IDLE", "RUNNING", "PAUSED", "DRAINING", "FAILED"]


class BrokerBlock(BaseModel):
    """Broker session facts at observation time.

    ``identity`` / ``submission_capability`` / ``effective_posture``
    are the three independent ADR-0011-amendment axes (PRD #619-A).
    ``probe_completed_at_ms`` is **probe-based**, not last-event-based
    — a quiet broker that has not pushed a market data tick recently
    still has a fresh probe. Initial defaults per PRD §B: probe
    cadence 10s, timeout 4s, fresh ≤25s.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: Literal["PAPER_VERIFIED", "LIVE_DETECTED", "UNKNOWN"]
    submission_capability: Literal[
        "PAPER_ORDERS_ENABLED", "READ_ONLY", "BLOCKED", "UNKNOWN"
    ]
    effective_posture: Literal[
        "PAPER_EXECUTION", "PAPER_OBSERVATION", "UNSAFE", "UNKNOWN"
    ]
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
    ]
    recovery_state: RecoveryState | None = None
    connection_epoch: int = Field(ge=0)
    client_id: int | None = Field(default=None, ge=0)
    connected_account: str | None = None
    port_class: Literal["paper_port", "live_port", "unknown"]
    observation_at_ms: int = Field(ge=0)
    probe_completed_at_ms: int | None = None
    reconnect_attempt: int = Field(ge=0)


class BarLoopBlock(BaseModel):
    """The strategy bar loop's view of market-data freshness.

    ``heartbeat_at_ms`` tracks **loop scheduling** — a bar loop that is
    waking up on cadence but receives no bars (closed market, halted
    symbol) still updates this. ``latest_source_bar_ms`` tracks
    **market-data freshness** — the close-time of the most recent bar
    actually emitted. Splitting them lets the backend freshness
    evaluator distinguish a halted engine (heartbeat stale) from a
    closed market (heartbeat fresh, latest_source_bar_ms old).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    heartbeat_at_ms: int = Field(ge=0)
    latest_source_bar_ms: int | None = None
    expected_interval_ms: int | None = Field(default=None, ge=0)


class ControlPlaneBlock(BaseModel):
    """The child's observation of the daemon-side control plane.

    ``observed_daemon_boot_id`` is the daemon's ``boot_id`` at the
    moment the child last read the lease file. A mismatch between
    ``expected_daemon_boot_id`` (set at child spawn) and
    ``observed_daemon_boot_id`` is the watchdog's signal that the
    daemon has restarted under it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_observed_at_ms: int = Field(ge=0)
    observed_daemon_boot_id: str | None = None


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class EngineRuntimeSnapshot(BaseModel):
    """Per-run runtime snapshot artifact.

    Written by the serialized publisher task at 1Hz steady-state plus
    immediate-flush on safety transitions. ``snapshot_seq`` is
    monotonic across the lifetime of a single child process; the
    backend freshness evaluator uses it (along with ``written_at_ms``)
    to detect torn / out-of-order reads.

    ``process_start_identity`` is a per-child stable identity that
    survives the child but is unique per process start. It is NOT the
    daemon's ``boot_id`` — that lives on ``expected_daemon_boot_id``
    + ``control_plane.observed_daemon_boot_id``.

    Schema version is part of the contract. A reader that sees a
    higher schema_version than it knows about MUST surface the
    artifact as "UNKNOWN — incompatible contract" rather than silently
    parse a partial subset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=ENGINE_RUNTIME_SCHEMA_VERSION, ge=1)

    strategy_instance_id: str
    run_id: str
    pid: int = Field(ge=0)
    process_start_identity: str
    expected_daemon_boot_id: str | None = None

    snapshot_seq: int = Field(ge=0)
    written_at_ms: int = Field(ge=0)

    command_loop: CommandLoopBlock
    broker: BrokerBlock
    bar_loop: BarLoopBlock
    control_plane: ControlPlaneBlock


# ---------------------------------------------------------------------------
# Atomic file writer + reader.
#
# The writer mirrors the ``run_status.py:_atomic_write_json`` pattern
# (tmp + fsync + replace). Ordering of ``snapshot_seq`` is the publisher's
# responsibility; this layer is single-statement durable but does not
# enforce monotonicity. Concurrent writers WILL race — the publisher
# contract is "one writer per run dir".
# ---------------------------------------------------------------------------


def write_engine_runtime_snapshot(
    run_dir: Path, snapshot: EngineRuntimeSnapshot
) -> None:
    """Write the snapshot to ``<run_dir>/engine_runtime.json`` atomically.

    Delegates the ``tmp + fsync + replace`` pattern to the canonical
    ``atomic_write_pydantic_artifact`` helper so the on-disk byte
    shape stays in lockstep with every other Pydantic artifact in the
    live-run / control-plane wire surface.
    """
    atomic_write_pydantic_artifact(run_dir / ENGINE_RUNTIME_FILENAME, snapshot)


def read_engine_runtime_snapshot(path: Path) -> EngineRuntimeSnapshot | None:
    """Read the latest snapshot, returning ``None`` on missing/malformed.

    Delegates the four fail-closed guards (missing / unreadable /
    malformed / forward-incompatible ``schema_version``) to the
    canonical ``read_pydantic_artifact`` helper. The backend freshness
    evaluator surfaces ``UNKNOWN`` on ``None``.
    """
    return read_pydantic_artifact(path, EngineRuntimeSnapshot)
