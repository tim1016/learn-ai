"""Tests for LiveStateSidecar — order-idempotency state across restarts.

Grown vertically via TDD: each cycle adds one behavior and one minimal
slice of schema or mechanics. See plan §16.4 Resolution 3 for the
12-field target schema this module grows toward.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarRepo,
)


def test_write_then_read_round_trips_strategy_instance_id(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = LiveStateEnvelope(strategy_instance_id="spy_ema_crossover")
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
