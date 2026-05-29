"""Tests for ColdStartReconciler — 7-step procedure encoding the
design-lock Resolution 2 contract from docs/ibkr-paper-deployment-plan.md.

Engine-side wiring (calling verify at boot, refusing to submit orders
when Poisoned) is consumed by a separate module and out of scope.
FakeBroker is inline and grown alongside the cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.engine.live.cold_start_reconciler import (
    ColdStartReconciler,
    Poisoned,
    SafeToResume,
)
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo


def _seed_sidecar(path: Path, **overrides: object) -> LiveStateSidecarRepo:
    """Construct + persist a minimal envelope; return the repo."""
    base: dict[str, object] = {
        "strategy_instance_id": "spy_ema_crossover",
        "run_id": "run-fixture",
        "bot_order_namespace": "learn-ai/spy_ema_crossover/v1",
        "ib_client_id": 17,
        "last_processed_bar_ms": 1_748_000_000_000,
        "last_artifact_flush_ms": 1_748_000_000_500,
    }
    base.update(overrides)
    repo = LiveStateSidecarRepo(path)
    repo.write(LiveStateEnvelope(**base))  # type: ignore[arg-type]
    return repo


@dataclass
class FakeBroker:
    """Inline test fake — grows as cycles demand more methods.

    Only exposes the verbs the reconciler is permitted to call. The
    absence of a reqAllOpenOrders method is part of the contract:
    Resolution 2 forbids that call entirely.
    """

    open_orders_by_namespace_result: list[dict[str, object]] = field(default_factory=list)

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        return list(self.open_orders_by_namespace_result)


def test_empty_broker_and_empty_sidecar_yields_safe_to_resume(tmp_path: Path) -> None:
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, SafeToResume)
    assert result.from_bar_ms == 1_748_000_000_000


def test_expected_order_missing_at_broker_yields_poisoned(tmp_path: Path) -> None:
    """Sidecar believes an order is open at the broker, but the broker
    doesn't show it. Could mean the order was cancelled out of band,
    or it was never submitted in the first place but the sidecar lied.
    Either way: not safe to resume.
    """
    repo = _seed_sidecar(
        tmp_path / "live_state.json",
        submitted_orders={
            "learn-ai/spy_ema_crossover/v1/2": {
                "perm_id": 9876543210,
                "status": "Submitted",
            }
        },
    )
    broker = FakeBroker(open_orders_by_namespace_result=[])
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, Poisoned)
    assert result.reason == "expected_order_missing_at_broker"


def test_unexpected_order_at_broker_yields_poisoned(tmp_path: Path) -> None:
    """Broker namespace shows an order whose client_order_id is not in
    sidecar.submitted_orders. The reconciler must refuse to resume.
    """
    repo = _seed_sidecar(tmp_path / "live_state.json", submitted_orders={})
    broker = FakeBroker(
        open_orders_by_namespace_result=[
            {
                "client_order_id": "learn-ai/spy_ema_crossover/v1/99",
                "perm_id": 1234567,
                "status": "Submitted",
            }
        ],
    )
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, Poisoned)
    assert result.reason == "unexpected_order_at_broker"
