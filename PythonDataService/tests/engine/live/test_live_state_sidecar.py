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
        "last_processed_bar_ms": 1_748_000_000_000,
        "last_artifact_flush_ms": 1_748_000_000_500,
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
    env = _min_envelope(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-2026-05-28-001",
        bot_order_namespace="learn-ai/spy_ema_crossover/v1",
        ib_client_id=17,
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.strategy_instance_id == "spy_ema_crossover"
    assert loaded.run_id == "run-2026-05-28-001"
    assert loaded.bot_order_namespace == "learn-ai/spy_ema_crossover/v1"
    assert loaded.ib_client_id == 17


def test_round_trip_persists_order_tracking(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(
        pending_intents=[
            {"client_order_id": "learn-ai/spy_ema_crossover/v1/3", "side": "BUY", "qty": 100},
        ],
        submitted_orders={
            "learn-ai/spy_ema_crossover/v1/2": {"perm_id": 9876543210, "status": "Submitted"},
        },
        known_perm_ids=[9876543209, 9876543210],
        known_exec_ids=["0000e0d5.6452f4c2.01.01"],
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.known_perm_ids == [9876543209, 9876543210]
    assert loaded.submitted_orders["learn-ai/spy_ema_crossover/v1/2"]["perm_id"] == 9876543210


def test_order_tracking_defaults_to_empty(tmp_path: Path) -> None:
    """Fresh cold start: no submitted orders, no intents, no known ids."""
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.pending_intents == []
    assert loaded.submitted_orders == {}
    assert loaded.known_perm_ids == []
    assert loaded.known_exec_ids == []


def test_round_trip_persists_position_and_bar_cursors(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(
        expected_position_by_symbol={"SPY": 100},
        last_processed_bar_ms=1_748_000_000_000,
        last_artifact_flush_ms=1_748_000_001_500,
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.expected_position_by_symbol == {"SPY": 100}
    assert loaded.last_processed_bar_ms == 1_748_000_000_000
    assert loaded.last_artifact_flush_ms == 1_748_000_001_500


def test_poisoned_reason_defaults_to_none(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.poisoned_reason is None


def test_round_trip_persists_poisoned_reason(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(poisoned_reason="unexpected_order_at_broker")
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.poisoned_reason == "unexpected_order_at_broker"
