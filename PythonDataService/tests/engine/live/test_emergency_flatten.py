"""Tests for the emergency-flatten subcommand (§ 7.2 #6).

Drives the CLI through ``run.main`` with an injected fake broker so
no real IBKR connection is needed. Verifies the operator-facing
contract: refuses without --confirm, refuses on account mismatch,
liquidates each non-zero position with the correct action and
quantity, and writes a complete audit log.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrOrderAck,
    IbkrOrderSpec,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_owner_fence import current_account_owner_write_grant
from app.engine.live.intent_events import IntentEventType, IntentKind
from app.engine.live.intent_wal import IntentWal
from app.engine.live.run import cmd_emergency_flatten


class _FakeFlattenBroker:
    """Just enough surface for cmd_emergency_flatten — fetch_positions,
    cancel_open_orders, place_order. No event stream, no portfolio refresh.

    ``timeline`` records every method call so cancel-before-liquidate
    ordering can be asserted directly (VCR-0009 regression)."""

    def __init__(
        self,
        *,
        account_id: str = "DU123",
        positions: list[IbkrPosition] | None = None,
        owned_open_order_ids: list[int] | None = None,
        cancel_raises: BaseException | None = None,
    ) -> None:
        self._account_id = account_id
        self._positions = positions or []
        self._owned_open_order_ids = list(owned_open_order_ids or [])
        self._cancel_raises = cancel_raises
        self.placed: list[IbkrOrderSpec] = []
        self.cancel_calls = 0
        self.timeline: list[str] = []
        self._next_order_id = 500

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return IbkrAccountSummary(
            account_id=self._account_id,
            is_paper=True,
            cash_balance=0.0,
            net_liquidation=0.0,
            fetched_at_ms=1,
        )

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return IbkrPositionsSnapshot(
            account_id=self._account_id,
            is_paper=True,
            positions=self._positions,
            fetched_at_ms=1,
        )

    async def cancel_open_orders(self) -> list[int]:
        self.cancel_calls += 1
        self.timeline.append("cancel_open_orders")
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return list(self._owned_open_order_ids)

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        self.placed.append(spec)
        self.timeline.append(f"place_order:{spec.symbol}")
        order_id = self._next_order_id
        self._next_order_id += 1
        return IbkrOrderAck(
            account_id=self._account_id,
            is_paper=True,
            order_id=order_id,
            client_id=42,
            con_id=12345,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )


def _pos(symbol: str, quantity: float) -> IbkrPosition:
    return IbkrPosition(
        account_id="DU123",
        con_id=12345,
        symbol=symbol,
        sec_type="STK",
        quantity=quantity,
        avg_cost=500.0,
        fetched_at_ms=1,
    )


def _args(
    *,
    run_dir: Path,
    account: str = "DU123",
    confirm: bool = True,
    broker=None,
    client=None,
    artifacts_root: Path | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        run_dir=run_dir,
        account=account,
        confirm=confirm,
        broker=broker,
        client=client,
        artifacts_root=artifacts_root or Path("PythonDataService/artifacts"),
    )


class _LifecycleTrackingClient:
    """Records ``connect()`` / ``disconnect()`` invocations so tests can
    assert the emergency-flatten path manages the IBKR client lifecycle.

    Lightweight stand-in for ``IbkrClient`` — provides only what
    ``cmd_emergency_flatten`` actually calls."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self._connected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


# ──────────────────────────── Refusals ───────────────────────────────


def test_emergency_flatten_refuses_without_confirm(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    broker = _FakeFlattenBroker(positions=[_pos("SPY", 200)])
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, confirm=False, broker=broker))
    assert rc == 2
    assert "refusing without --confirm" in capsys.readouterr().err
    assert broker.placed == [], "no orders may be placed without --confirm"


def test_emergency_flatten_refuses_on_account_mismatch(
    tmp_path: Path,
) -> None:
    """If the connected broker's account doesn't match --account, refuse —
    the operator must name the account they intend to flatten."""
    broker = _FakeFlattenBroker(account_id="DU999", positions=[_pos("SPY", 200)])
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, account="DU123", broker=broker))
    assert rc == 2
    assert broker.placed == []
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "REFUSED" in log
    assert "DU999" in log
    assert "DU123" in log


# ──────────────────────────── Happy path ─────────────────────────────


def test_emergency_flatten_liquidates_each_nonzero_position(tmp_path: Path) -> None:
    broker = _FakeFlattenBroker(
        positions=[
            _pos("SPY", 200),
            _pos("QQQ", -50),  # short — flatten via BUY
            _pos("IWM", 0),  # zero — should NOT generate an order
        ]
    )
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))
    assert rc == 0

    # Two non-zero positions ⇒ two orders, both with the correct
    # opposing action.
    assert len(broker.placed) == 2
    by_symbol = {o.symbol: o for o in broker.placed}
    assert by_symbol["SPY"].action == "SELL"
    assert by_symbol["SPY"].quantity == 200
    assert by_symbol["QQQ"].action == "BUY"
    assert by_symbol["QQQ"].quantity == 50
    # All emergency orders carry the unique-prefix client_order_id so
    # they're distinguishable from any pre-existing live-N orders.
    assert all(o.client_order_id.startswith("emergency-flatten-") for o in broker.placed)


def test_emergency_flatten_preserves_fractional_quantities(tmp_path: Path) -> None:
    """Fractional positions (e.g. 0.5 share of FRAC) must produce a
    fractional liquidation order — not get truncated to zero by an
    int cast. (CodeRabbit P2 from #193.)"""
    broker = _FakeFlattenBroker(
        positions=[
            _pos("SPY", 100.5),
            _pos("FRAC", 0.25),
        ]
    )
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))
    assert rc == 0
    assert len(broker.placed) == 2
    by_symbol = {o.symbol: o for o in broker.placed}
    assert by_symbol["SPY"].quantity == 100.5
    assert by_symbol["FRAC"].quantity == 0.25


def test_emergency_flatten_does_nothing_on_empty_account(tmp_path: Path) -> None:
    broker = _FakeFlattenBroker(positions=[])
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))
    assert rc == 0
    assert broker.placed == []
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "complete: liquidated=0" in log


# ──────────────────────────── VCR-0009 ───────────────────────────────


def test_emergency_flatten_cancels_open_orders_before_liquidating_vcr_0009(
    tmp_path: Path,
) -> None:
    """VCR-0009 — cmd_emergency_flatten MUST cancel owned open orders BEFORE
    any liquidation order is submitted. Without this, an open bot SELL limit
    can race the emergency SELL market and double-sell the position."""
    broker = _FakeFlattenBroker(
        positions=[_pos("SPY", 200), _pos("QQQ", -50)],
        owned_open_order_ids=[111, 222],
    )

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    assert rc == 0
    assert broker.cancel_calls == 1
    # The critical ordering invariant: cancel comes first; every place_order
    # comes after.
    assert broker.timeline[0] == "cancel_open_orders"
    assert all(step.startswith("place_order:") for step in broker.timeline[1:])
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "cancelled_open_orders: count=2" in log


def test_emergency_flatten_runs_broker_writes_under_account_owner_grant(
    tmp_path: Path,
) -> None:
    class _GrantAssertingBroker(_FakeFlattenBroker):
        async def cancel_open_orders(self) -> list[int]:
            grant = current_account_owner_write_grant()
            assert grant is not None
            assert grant.account_id == "DU123"
            assert grant.boundary == "broker.cancel_order"
            return await super().cancel_open_orders()

        async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
            grant = current_account_owner_write_grant()
            assert grant is not None
            assert grant.account_id == "DU123"
            assert grant.boundary == "broker.place_order"
            return await super().place_order(spec)

    broker = _GrantAssertingBroker(positions=[_pos("SPY", 100)])

    rc = cmd_emergency_flatten(
        _args(run_dir=tmp_path, broker=broker, artifacts_root=tmp_path / "artifacts")
    )

    assert rc == 0
    assert broker.timeline == ["cancel_open_orders", "place_order:SPY"]


def test_emergency_flatten_proceeds_when_cancel_raises_vcr_0009(
    tmp_path: Path,
) -> None:
    """VCR-0009 — emergency-flatten is an operator-confirmed force-flatten
    path. If cancel_open_orders raises (broker glitch, partial outage), the
    runner logs the failure loudly and proceeds with liquidation anyway —
    leaving open positions during a panic is worse than acting without
    cancel confirmation. The audit log carries both events."""
    broker = _FakeFlattenBroker(
        positions=[_pos("SPY", 200)],
        cancel_raises=RuntimeError("broker timeout"),
    )

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    assert rc == 0
    assert broker.cancel_calls == 1
    assert len(broker.placed) == 1  # liquidation still happened
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "cancel_open_orders failed" in log
    assert "RuntimeError" in log
    assert "broker timeout" in log
    assert "EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS" in log


def test_emergency_flatten_proceeds_on_cancel_confirm_timeout_vcr_0002(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 5C / VCR-0002 — emergency-flatten force carve-out.

    The managed paths (LiveEngine._flatten, _recovery_flatten) refuse to
    liquidate on cancel-confirm timeout. emergency-flatten is the operator-
    confirmed panic surface and is allowed to proceed past timeout — but
    the unconfirmed-cancel decision is recorded as an audit event so a
    post-mortem can identify runs that liquidated against possibly-still-
    open orders.

    Models the broker session whose cancel-confirm path has gone silent
    (gateway dropped the callback, or the cancel acknowledgement never
    arrives). The operator already typed ``--confirm`` and named the
    account; the panic semantics commit to flattening.
    """
    import asyncio

    class _HangingCancelBroker(_FakeFlattenBroker):
        async def cancel_open_orders(self) -> list[int]:
            self.cancel_calls += 1
            self.timeline.append("cancel_open_orders")
            await asyncio.sleep(10)
            return []

    monkeypatch.setattr(
        "app.engine.live.live_engine.CANCEL_CONFIRM_TIMEOUT_S", 0.05
    )
    broker = _HangingCancelBroker(positions=[_pos("SPY", 200)])

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    assert rc == 0
    assert broker.cancel_calls == 1
    assert len(broker.placed) == 1, "force-flatten must liquidate past timeout"
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "timed out" in log
    assert "EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS" in log
    assert "cancel_confirm_timeout_s=0.05" in log


# ──────────────────────────── Audit log ──────────────────────────────


def test_emergency_flatten_log_records_every_action(tmp_path: Path) -> None:
    broker = _FakeFlattenBroker(positions=[_pos("SPY", 100), _pos("QQQ", 50)])
    cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    log = (tmp_path / "emergency_flatten.log").read_text()
    # Start, two liquidation lines, complete. Exact format checked
    # loosely so the field-by-field message can evolve without
    # re-pinning every test. ``qty`` is float-formatted because
    # IbkrOrderSpec.quantity is float (fractional-share support).
    assert "start: account=DU123" in log
    assert "liquidated: symbol=SPY qty=100.0 action=SELL" in log
    assert "liquidated: symbol=QQQ qty=50.0 action=SELL" in log
    assert "complete: liquidated=2" in log


def test_emergency_flatten_appends_to_existing_log(tmp_path: Path) -> None:
    """Re-running emergency-flatten in the same run_dir must NOT clobber
    the prior session's audit trail. Each invocation appends."""
    (tmp_path / "emergency_flatten.log").write_text(
        "2026-05-04T01:00:00+00:00 prior session: liquidated=1\n", encoding="utf-8"
    )
    broker = _FakeFlattenBroker(positions=[_pos("SPY", 100)])
    cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "prior session: liquidated=1" in log, "must preserve prior log"
    assert "complete: liquidated=1" in log, "must append new run"


# ──────────────────────────── VCR-0020 ───────────────────────────────


class _DurableSubmitRequiringBroker(_FakeFlattenBroker):
    """Fake that enforces the Phase 5A real-broker invariant: every spec
    handed to ``place_order`` must carry an ``order_ref``. Mirrors
    ``place_paper_order``'s OrderRefusedError so we exercise the actual
    contract that ``cmd_emergency_flatten`` was failing in production."""

    requires_durable_submit = True

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        if spec.order_ref is None:
            raise AssertionError(
                "ADR 0008: place_paper_order requires spec.order_ref "
                "(VCR-0020 — emergency-flatten must stamp order_ref)"
            )
        return await super().place_order(spec)


def test_emergency_flatten_stamps_order_ref_on_each_spec_vcr_0020(tmp_path: Path) -> None:
    """VCR-0020 — every liquidation spec must carry a deterministic
    ``order_ref`` so a real-broker adapter accepts it. Without this fix the
    documented panic CLI exits 3 with OrderRefusedError on the first
    placement and the operator's only escape hatch fails.

    Receipt: 2026-06-16 HITL run — operator invoked emergency-flatten to
    clean up VCR-0019's stray short and got
    ``OrderRefusedError: ADR 0008: place_paper_order requires spec.order_ref``
    on every liquidation attempt. Cleanup fell back to hand-crafted
    /api/broker/orders calls.
    """
    broker = _DurableSubmitRequiringBroker(
        positions=[_pos("SPY", 200), _pos("QQQ", -50)]
    )

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    assert rc == 0, "all placements must succeed with a stamped order_ref"
    assert len(broker.placed) == 2
    for spec in broker.placed:
        assert spec.order_ref is not None
        # Synthetic namespace ``learn-ai/eflat-{account}/v1``: same
        # ``{namespace}:{intent_id}`` shape as engine-issued orders, with a
        # short ``eflat-`` prefix to keep within the 60-char order_ref cap.
        assert spec.order_ref.startswith("learn-ai/eflat-DU123/v1:")
        _ns, _, intent = spec.order_ref.rpartition(":")
        assert len(intent) == 22  # 22-char base64url intent_id

    # Each liquidation must get a UNIQUE intent_id (no replay-by-mistake).
    intents = {spec.order_ref.rpartition(":")[2] for spec in broker.placed}
    assert len(intents) == len(broker.placed)


def test_emergency_flatten_writes_structured_audit_wal(tmp_path: Path) -> None:
    broker = _DurableSubmitRequiringBroker(
        positions=[_pos("SPY", 200), _pos("QQQ", -50)]
    )

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))

    assert rc == 0
    events = IntentWal(tmp_path / "emergency_flatten_audit.jsonl").read_tail()
    assert [event.event_type for event in events] == [
        IntentEventType.PENDING_INTENT,
        IntentEventType.SUBMITTED,
        IntentEventType.PENDING_INTENT,
        IntentEventType.SUBMITTED,
    ]
    pending = [event for event in events if event.event_type is IntentEventType.PENDING_INTENT]
    submitted = [event for event in events if event.event_type is IntentEventType.SUBMITTED]
    assert [event.intent_kind for event in events] == [IntentKind.EMERGENCY_FLATTEN] * 4
    assert [event.order_ref for event in pending] == [spec.order_ref for spec in broker.placed]
    assert [event.intent_id for event in submitted] == [event.intent_id for event in pending]
    assert [event.order_id for event in submitted] == [500, 501]
    assert pending[0].order_spec is not None
    assert pending[0].order_spec["symbol"] == "SPY"
    assert pending[0].order_spec["action"] == "SELL"
    assert pending[1].order_spec is not None
    assert pending[1].order_spec["symbol"] == "QQQ"
    assert pending[1].order_spec["action"] == "BUY"


# ──────────────────────────── Failure path ───────────────────────────


def test_emergency_flatten_returns_3_and_logs_on_broker_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Broker raises (e.g. network failure) → exit 3, audit log records the cause."""

    class _ExplodingBroker(_FakeFlattenBroker):
        async def fetch_positions(self) -> IbkrPositionsSnapshot:
            raise ConnectionError("simulated broker outage")

    broker = _ExplodingBroker()
    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker))
    assert rc == 3
    log = (tmp_path / "emergency_flatten.log").read_text()
    assert "FAILURE" in log
    assert "ConnectionError" in log
    assert "simulated broker outage" in log


# ──────────────────────────── Client lifecycle ───────────────────────


def test_emergency_flatten_connects_and_disconnects_client(tmp_path: Path) -> None:
    """The IBKR client must be connected before any broker call and
    disconnected on exit.

    Reviewer feedback (P1.1): the prior implementation constructed
    ``IbkrClient()`` + ``IbkrBrokerAdapter(client)`` then called
    ``broker.fetch_positions()`` directly. ``fetch_positions`` →
    ``account.fetch_account_summary`` invokes ``client.require_connected()``
    which raises if the client never connected, so
    ``emergency-flatten --confirm`` would fail at the very first broker
    call in production (no orders placed). Fix: wrap the body in
    ``await client.connect() ... try: ... finally: await client.disconnect()``.
    """
    broker = _FakeFlattenBroker(positions=[_pos("SPY", 100)])
    client = _LifecycleTrackingClient()

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker, client=client))

    assert rc == 0
    assert client.connect_calls == 1, "must connect before fetch_positions"
    assert client.disconnect_calls == 1, "must disconnect in finally"
    # And the SPY position was actually liquidated — i.e. the lifecycle
    # didn't block the actual work.
    assert len(broker.placed) == 1
    assert broker.placed[0].action == "SELL"


def test_emergency_flatten_disconnects_client_even_when_broker_raises(tmp_path: Path) -> None:
    """The disconnect must run in ``finally`` so a broker error doesn't
    leak a connected client. Pairs with the P1.1 fix."""

    class _ExplodingBroker(_FakeFlattenBroker):
        async def fetch_positions(self) -> IbkrPositionsSnapshot:
            raise ConnectionError("simulated broker outage")

    broker = _ExplodingBroker()
    client = _LifecycleTrackingClient()

    rc = cmd_emergency_flatten(_args(run_dir=tmp_path, broker=broker, client=client))

    assert rc == 3  # error path
    assert client.connect_calls == 1
    assert client.disconnect_calls == 1, "must disconnect even when broker raises"
