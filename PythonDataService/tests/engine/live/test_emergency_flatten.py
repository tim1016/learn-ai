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
from app.engine.live.run import cmd_emergency_flatten


class _FakeFlattenBroker:
    """Just enough surface for cmd_emergency_flatten — fetch_positions
    + place_order. No event stream, no portfolio refresh."""

    def __init__(
        self,
        *,
        account_id: str = "DU123",
        positions: list[IbkrPosition] | None = None,
    ) -> None:
        self._account_id = account_id
        self._positions = positions or []
        self.placed: list[IbkrOrderSpec] = []
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

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        self.placed.append(spec)
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
) -> argparse.Namespace:
    return argparse.Namespace(
        run_dir=run_dir,
        account=account,
        confirm=confirm,
        broker=broker,
        client=client,
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
