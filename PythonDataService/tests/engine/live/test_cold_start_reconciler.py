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
    executions_for_namespace_result: list[dict[str, object]] = field(default_factory=list)
    raise_on_open_orders: BaseException | None = None

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        if self.raise_on_open_orders is not None:
            raise self.raise_on_open_orders
        return list(self.open_orders_by_namespace_result)

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]:
        return list(self.executions_for_namespace_result)


def test_empty_broker_and_empty_sidecar_yields_safe_to_resume(tmp_path: Path) -> None:
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, SafeToResume)
    assert result.from_bar_ms == 1_748_000_000_000


def test_verify_never_calls_req_all_open_orders(tmp_path: Path) -> None:
    """Resolution 2 forbids reqAllOpenOrders. The reconciler must query
    only via its namespaced orderRef / client_order_id. Add the method
    to the fake broker as a spy and assert the count stays at 0 across
    every branch verify() takes.
    """

    @dataclass
    class TrackingBroker(FakeBroker):
        req_all_open_orders_calls: int = 0

        def req_all_open_orders(self) -> list[dict[str, object]]:
            self.req_all_open_orders_calls += 1
            return []

    reconciler = ColdStartReconciler()

    scenarios: list[tuple[str, TrackingBroker, dict[str, object]]] = [
        ("happy", TrackingBroker(), {}),
        (
            "unexpected_order",
            TrackingBroker(
                open_orders_by_namespace_result=[
                    {"client_order_id": "rogue", "perm_id": 1}
                ],
            ),
            {},
        ),
        (
            "missing_expected",
            TrackingBroker(open_orders_by_namespace_result=[]),
            {"submitted_orders": {"x": {"perm_id": 1, "status": "Submitted"}}},
        ),
        ("shadow_empty", TrackingBroker(), {"shadow_kwarg": True}),
        (
            "connect_failure",
            TrackingBroker(raise_on_open_orders=ConnectionError("x")),
            {},
        ),
    ]

    for name, broker, overrides in scenarios:
        sidecar_path = tmp_path / f"{name}.json"
        shadow = overrides.pop("shadow_kwarg", False)
        repo = _seed_sidecar(sidecar_path, **overrides)
        reconciler.verify(broker=broker, sidecar=repo, shadow_mode=bool(shadow))
        assert broker.req_all_open_orders_calls == 0, (
            f"scenario {name!r}: reqAllOpenOrders was invoked "
            f"({broker.req_all_open_orders_calls}x)"
        )


def test_broker_connect_failure_yields_cannot_verify_offline(tmp_path: Path) -> None:
    """No broker connection, no verified resume. Per Resolution 2 there
    is no offline path: if we can't reach the broker, we cannot
    distinguish a clean cold start from one with hidden divergence,
    so we refuse to resume.
    """
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker(raise_on_open_orders=ConnectionError("gateway unreachable"))
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, Poisoned)
    assert result.reason == "cannot_verify_offline"


def test_shadow_mode_with_any_broker_order_yields_poisoned(tmp_path: Path) -> None:
    """Shadow-mode invariant: a shadow strategy never submits real orders,
    so its namespace at the broker must always yield zero open orders.
    Any non-empty result is a sign that something is submitting under
    this namespace that shouldn't be — refuse to resume.
    """
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker(
        open_orders_by_namespace_result=[
            {"client_order_id": "learn-ai/spy_vwap_shadow/v1/1", "perm_id": 1},
        ],
    )
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, shadow_mode=True)

    assert isinstance(result, Poisoned)
    assert result.reason == "shadow_namespace_nonempty"


def test_shadow_mode_with_empty_broker_is_safe(tmp_path: Path) -> None:
    """The good shadow-mode case: namespace yields zero orders, sidecar
    is clean, verify returns SafeToResume."""
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker(open_orders_by_namespace_result=[])
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, shadow_mode=True)

    assert isinstance(result, SafeToResume)


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


def test_fill_arrived_after_last_flush_yields_safe_with_recovered_fill(
    tmp_path: Path,
) -> None:
    """Recovery happy path: the bot submitted an order; the broker filled
    it; the bot crashed before flushing the exec_id to artifacts. At
    cold-start the broker shows the order absent from open orders
    (filled, no longer open) but present in the executions list. The
    reconciler classifies as a recovered_fill and proceeds.
    """
    repo = _seed_sidecar(
        tmp_path / "live_state.json",
        submitted_orders={
            "learn-ai/spy_ema_crossover/v1/2": {
                "perm_id": 9876543210,
                "status": "Submitted",
            }
        },
        known_exec_ids=[],
    )
    broker = FakeBroker(
        open_orders_by_namespace_result=[],  # filled — no longer open
        executions_for_namespace_result=[
            {
                "client_order_id": "learn-ai/spy_ema_crossover/v1/2",
                "perm_id": 9876543210,
                "exec_id": "0000e0d5.6452f4c2.01.01",
                "fill_price": 421.50,
                "fill_qty": 100,
            }
        ],
    )
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, SafeToResume)
    assert len(result.recovered_fills) == 1
    assert result.recovered_fills[0]["exec_id"] == "0000e0d5.6452f4c2.01.01"
    assert result.recovered_fills[0]["client_order_id"] == "learn-ai/spy_ema_crossover/v1/2"


def test_safe_to_resume_leaves_no_poisoned_flag(tmp_path: Path) -> None:
    """Negative-space invariant: a clean cold start must not create
    poisoned.flag. Otherwise the engine's halt check would treat every
    boot as poisoned and refuse to ever submit orders.
    """
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker()
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, run_dir=run_dir)

    assert isinstance(result, SafeToResume)
    assert not (run_dir / "poisoned.flag").exists()


def test_poisoned_outcome_writes_poisoned_flag(tmp_path: Path) -> None:
    """When verify returns Poisoned and a run_dir was supplied, the
    reconciler writes <run_dir>/poisoned.flag with the reason. The
    file is the cross-process signal to the engine and any operator
    tooling that this run must not submit new orders.
    """
    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker(
        open_orders_by_namespace_result=[
            {"client_order_id": "rogue", "perm_id": 1234567}
        ],
    )
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, run_dir=run_dir)

    assert isinstance(result, Poisoned)
    flag_path = run_dir / "poisoned.flag"
    assert flag_path.exists()
    assert "unexpected_order_at_broker" in flag_path.read_text(encoding="utf-8")


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
