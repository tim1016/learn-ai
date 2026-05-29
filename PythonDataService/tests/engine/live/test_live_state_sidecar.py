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


def _min_envelope(**overrides: object) -> LiveStateEnvelope:
    """Construct an envelope with the minimum required fields filled in.

    Tests override only the fields they exercise; identity-tuple defaults
    are stable so equality assertions stay readable as the schema grows.
    """
    base: dict[str, object] = {
        "strategy_instance_id": "spy_ema_crossover",
        "run_id": "run-fixture",
        "bot_order_namespace": "learn-ai/spy_ema_crossover/v1",
        "ib_client_id": 17,
    }
    base.update(overrides)
    return LiveStateEnvelope(**base)  # type: ignore[arg-type]


def test_write_then_read_round_trips_strategy_instance_id(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded == env


def test_read_missing_returns_none(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "absent.json")
    assert repo.read() is None


def test_round_trip_persists_identity_tuple(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = LiveStateEnvelope(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-2026-05-28-001",
        bot_order_namespace="learn-ai/spy_ema_crossover/v1",
        ib_client_id=17,
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
