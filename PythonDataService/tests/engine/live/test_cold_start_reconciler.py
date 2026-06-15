"""Tests for ColdStartReconciler — 7-step procedure encoding the
design-lock Resolution 2 contract from docs/ibkr-paper-deployment-plan.md.

Engine-side wiring (calling verify at boot, refusing to submit orders
when Poisoned) is consumed by a separate module and out of scope.
FakeBroker is inline and grown alongside the cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from app.engine.live.cold_start_reconciler import (
    ColdStartReconciler,
    Poisoned,
    SafeToResume,
)
from app.engine.live.halt import PoisonedHaltTrigger, read_poisoned_flag
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

    Declares ``_shadow_safe = True`` so the VCR-P3-G shadow-broker
    assertion in ``ColdStartReconciler._assert_shadow_broker`` accepts
    this fake. The fake does not submit orders to any real broker —
    it's an in-memory implementation, structurally equivalent to
    ``NoSubmitBrokerAdapter`` for the reconciler's contract.
    """

    _shadow_safe: ClassVar[bool] = True

    open_orders_by_namespace_result: list[dict[str, object]] = field(default_factory=list)
    executions_for_namespace_result: list[dict[str, object]] = field(default_factory=list)
    raise_on_open_orders: BaseException | None = None
    raise_on_executions: BaseException | None = None

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        if self.raise_on_open_orders is not None:
            raise self.raise_on_open_orders
        return list(self.open_orders_by_namespace_result)

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]:
        if self.raise_on_executions is not None:
            raise self.raise_on_executions
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


def test_corrupt_sidecar_yields_poisoned_with_flag(tmp_path: Path) -> None:
    """If the on-disk sidecar is unreadable (LiveStateSidecarCorruptError)
    the reconciler cannot verify anything and must refuse to resume.
    The flag carries the corrupt-sidecar reason so operator tooling
    can route the operator to inspect the file directly.
    """
    sidecar_path = tmp_path / "live_state.json"
    sidecar_path.write_text("{ this is not valid json", encoding="utf-8")
    repo = LiveStateSidecarRepo(sidecar_path)
    broker = FakeBroker()
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, run_dir=run_dir)

    assert isinstance(result, Poisoned)
    assert result.reason == "sidecar_corrupt"
    flag = read_poisoned_flag(run_dir)
    assert flag is not None
    assert flag.trigger is PoisonedHaltTrigger.COLD_START_DIVERGENCE
    assert flag.details["reason"] == "sidecar_corrupt"


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
    flag = read_poisoned_flag(run_dir)
    assert flag is not None
    assert flag.trigger is PoisonedHaltTrigger.COLD_START_DIVERGENCE
    assert flag.details["reason"] == "unexpected_order_at_broker"


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


def test_multiple_executions_for_one_order_all_recovered(tmp_path: Path) -> None:
    """Regression (Codex P1): IBKR can report several executions for one
    order (partial / split fills, each with its own exec_id) after the
    last flush. The reconciler must recover *every* unflushed execution,
    not just the last one — collapsing to one entry per client_order_id
    understates the resumed fill quantity / P&L.
    """
    order_id = "learn-ai/spy_ema_crossover/v1/2"
    repo = _seed_sidecar(
        tmp_path / "live_state.json",
        submitted_orders={order_id: {"perm_id": 9876543210, "status": "Submitted"}},
        known_exec_ids=[],
    )
    broker = FakeBroker(
        open_orders_by_namespace_result=[],  # filled — no longer open
        executions_for_namespace_result=[
            {"client_order_id": order_id, "exec_id": "exec-1", "fill_qty": 40},
            {"client_order_id": order_id, "exec_id": "exec-2", "fill_qty": 60},
        ],
    )
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, SafeToResume)
    recovered_exec_ids = {fill["exec_id"] for fill in result.recovered_fills}
    assert recovered_exec_ids == {"exec-1", "exec-2"}


def test_multiple_executions_only_unknown_recovered(tmp_path: Path) -> None:
    """A previously-flushed execution (its exec_id already in
    known_exec_ids) is not re-recovered, but its sibling unflushed
    execution on the same order still is.
    """
    order_id = "learn-ai/spy_ema_crossover/v1/2"
    repo = _seed_sidecar(
        tmp_path / "live_state.json",
        submitted_orders={order_id: {"perm_id": 9876543210, "status": "Submitted"}},
        known_exec_ids=["exec-1"],
    )
    broker = FakeBroker(
        open_orders_by_namespace_result=[],
        executions_for_namespace_result=[
            {"client_order_id": order_id, "exec_id": "exec-1", "fill_qty": 40},
            {"client_order_id": order_id, "exec_id": "exec-2", "fill_qty": 60},
        ],
    )
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo)

    assert isinstance(result, SafeToResume)
    assert [fill["exec_id"] for fill in result.recovered_fills] == ["exec-2"]


def test_execution_lookup_failure_yields_cannot_verify_offline(tmp_path: Path) -> None:
    """Regression (Codex P2): if the open-order query succeeds but the
    execution-history query fails, the reconciler must poison rather
    than raise. The module contract says there is no offline/unverified
    resume path — broker reachability failures are Poisoned.
    """
    order_id = "learn-ai/spy_ema_crossover/v1/2"
    repo = _seed_sidecar(
        tmp_path / "live_state.json",
        submitted_orders={order_id: {"perm_id": 9876543210, "status": "Submitted"}},
    )
    broker = FakeBroker(
        open_orders_by_namespace_result=[],  # forces the execution lookup
        raise_on_executions=ConnectionError("execution stream disconnected"),
    )
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, run_dir=run_dir)

    assert isinstance(result, Poisoned)
    assert result.reason == "cannot_verify_offline"
    assert (run_dir / "poisoned.flag").exists()


def test_poisoned_flag_is_shared_json_format(tmp_path: Path) -> None:
    """Regression (Codex P1): poisoned.flag must be the shared halt JSON
    object, not a bare reason string. Otherwise run.py's
    read_poisoned_flag raises on restart and the live-runs status
    endpoint's _read_flag silently drops the poison details. Assert the
    flag round-trips through BOTH consumers.
    """
    import json

    repo = _seed_sidecar(tmp_path / "live_state.json")
    broker = FakeBroker(
        open_orders_by_namespace_result=[{"client_order_id": "rogue", "perm_id": 1}],
    )
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    reconciler = ColdStartReconciler()

    result = reconciler.verify(broker=broker, sidecar=repo, run_dir=run_dir)

    assert isinstance(result, Poisoned)
    # run.py consumer: parses as a PoisonedHaltReason without raising.
    flag = read_poisoned_flag(run_dir)
    assert flag is not None
    assert flag.trigger is PoisonedHaltTrigger.COLD_START_DIVERGENCE
    assert flag.details["reason"] == "unexpected_order_at_broker"
    assert flag.last_clean_bar_close_ms == 1_748_000_000_000
    # live-runs status consumer: valid JSON object, not a bare string.
    payload = json.loads((run_dir / "poisoned.flag").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert payload["trigger"] == "cold_start_divergence"


# ─────────────────────── VCR-P3-G shadow assertion ──────────────────────


def test_shadow_mode_with_non_shadow_safe_broker_raises_p3_g(tmp_path: Path) -> None:
    """VCR-P3-G — ``shadow_mode=True`` with a broker that is neither
    ``NoSubmitBrokerAdapter`` nor ``_shadow_safe=True`` fails fast at
    the reconciler boundary. The shadow contract is structural; a
    runtime flag is not sufficient defense."""
    import pytest

    from app.engine.live.cold_start_reconciler import ShadowBrokerMismatchError

    class _ImitationBroker:
        """A broker that lacks both the NoSubmitBrokerAdapter pedigree
        AND the _shadow_safe marker. Models a real IbkrBrokerAdapter
        being misconfigured with shadow_mode=True."""

        def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
            return []

        def executions_for_namespace(
            self, namespace: str, since_ms: int
        ) -> list[dict[str, object]]:
            return []

    repo = _seed_sidecar(tmp_path / "live_state.json")
    reconciler = ColdStartReconciler()

    with pytest.raises(ShadowBrokerMismatchError, match="shadow_mode=True"):
        reconciler.verify(
            broker=_ImitationBroker(), sidecar=repo, shadow_mode=True
        )


def test_shadow_mode_with_no_submit_broker_adapter_proceeds_p3_g(
    tmp_path: Path,
) -> None:
    """VCR-P3-G — sanity: the production ``NoSubmitBrokerAdapter`` is
    accepted by the assertion. We can't construct one fully here (it
    requires an IB client), so we use ``isinstance(... NoSubmitBrokerAdapter)``
    by constructing a subclass that satisfies the type check without
    the heavy init."""
    from app.engine.live.no_submit_broker_adapter import NoSubmitBrokerAdapter

    class _BareNoSubmitBroker(NoSubmitBrokerAdapter):
        def __init__(self) -> None:  # bypass NoSubmitBrokerAdapter's heavy init
            pass

        def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
            return []

        def executions_for_namespace(
            self, namespace: str, since_ms: int
        ) -> list[dict[str, object]]:
            return []

    repo = _seed_sidecar(tmp_path / "live_state.json")
    reconciler = ColdStartReconciler()

    # Should NOT raise — isinstance(NoSubmitBrokerAdapter) check passes.
    result = reconciler.verify(
        broker=_BareNoSubmitBroker(), sidecar=repo, shadow_mode=True
    )
    assert isinstance(result, SafeToResume)


def test_shadow_mode_with_shadow_safe_marker_proceeds_p3_g(tmp_path: Path) -> None:
    """VCR-P3-G — the ``_shadow_safe=True`` opt-in marker is the
    escape hatch for in-memory test fakes. FakeBroker uses this in the
    existing shadow-mode tests; this test exercises the marker path
    directly with an unrelated class."""
    repo = _seed_sidecar(tmp_path / "live_state.json")

    class _MarkedShadowBroker:
        _shadow_safe: ClassVar[bool] = True

        def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
            return []

        def executions_for_namespace(
            self, namespace: str, since_ms: int
        ) -> list[dict[str, object]]:
            return []

    reconciler = ColdStartReconciler()
    result = reconciler.verify(
        broker=_MarkedShadowBroker(), sidecar=repo, shadow_mode=True
    )
    assert isinstance(result, SafeToResume)
