"""Unit tests for ``_recovery_flatten`` in ``app.engine.live.run``.

The cmd_start integration (exception → recovery flatten → exit 3) is
covered end-to-end in the FakeBroker shutdown integration test; these
tests verify the recovery helper in isolation.
"""

from __future__ import annotations

import pytest

from app.broker.ibkr.models import (
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.run import _is_recovery_readonly, _recovery_flatten, _resolve_recovery_broker
from tests.engine.live.fixtures.fake_broker import FakeBroker


class _Args:
    def __init__(self, *, readonly: bool) -> None:
        self.readonly = readonly


class _ClientWithSettings:
    def __init__(self, *, readonly: bool) -> None:
        self.settings = type("_S", (), {"readonly": readonly})()


def _seed_position(broker: FakeBroker, symbol: str, quantity: float) -> None:
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol=symbol,
                sec_type="STK",
                quantity=quantity,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_sell_per_long_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1
    assert sell_orders[0].symbol == "SPY"
    assert sell_orders[0].quantity == 100


@pytest.mark.asyncio
async def test_recovery_flatten_submits_one_buy_per_short_position() -> None:
    broker = FakeBroker()
    _seed_position(broker, "SPY", -50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    buy_orders = [o for o in broker.orders if o.action == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0].symbol == "SPY"
    assert buy_orders[0].quantity == 50


@pytest.mark.asyncio
async def test_recovery_flatten_no_positions_returns_zero() -> None:
    broker = FakeBroker()
    liquidated = await _recovery_flatten(broker)
    assert liquidated == 0
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_readonly_does_not_place_or_cancel_orders() -> None:
    """In readonly mode the recovery path must NOT touch the broker.

    Regression: a paper-week dry-run with ``--readonly`` crashed
    mid-session (duplicate IBKR 5-second bar timestamp) and
    ``_recovery_flatten`` placed a real SELL MKT against the paper
    account, breaking the documented ``IBKR_READONLY=true`` contract
    on ``IbkrSettings.readonly`` ("every call to place_paper_order
    raises OrderRefusedError before any contract is built"). On a
    flag-flip to live this would have closed a real position on an
    arbitrary engine crash.

    Readonly must enumerate positions for the operator log and return
    the detected count, but must not call ``place_order`` or
    ``cancel_open_orders``.
    """

    class RaisingCancelAndPlaceBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise AssertionError("cancel_open_orders must not be called in readonly mode")

        async def place_order(self, spec):
            raise AssertionError("place_order must not be called in readonly mode")

    broker = RaisingCancelAndPlaceBroker()
    _seed_position(broker, "SPY", 100.0)

    detected = await _recovery_flatten(broker, readonly=True)

    assert detected == 1
    assert broker.orders == []


@pytest.mark.asyncio
async def test_recovery_flatten_cancel_failure_does_not_block_flatten() -> None:
    """A broker failure on cancel_open_orders must not skip the flatten path.

    The whole point of recovery flatten is to leave the account flat
    even when something has misbehaved. Swallowing the cancel failure
    and proceeding to liquidate matches that intent.
    """

    class RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("simulated broker timeout on cancel")

    broker = RaisingCancelBroker()
    _seed_position(broker, "SPY", 50.0)

    liquidated = await _recovery_flatten(broker)

    assert liquidated == 1
    sell_orders = [o for o in broker.orders if o.action == "SELL"]
    assert len(sell_orders) == 1


@pytest.mark.asyncio
async def test_recovery_flatten_per_position_place_order_failure_keeps_loop() -> None:
    """If place_order fails for symbol A, symbol B still gets an attempt."""

    class FailFirstThenSucceedBroker(FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def place_order(self, spec):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("simulated place_order failure on first call")
            return await super().place_order(spec)

    broker = FailFirstThenSucceedBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=1,
                symbol="SPY",
                sec_type="STK",
                quantity=100.0,
                avg_cost=500.0,
                fetched_at_ms=1,
            ),
            IbkrPosition(
                account_id="DU123",
                con_id=2,
                symbol="QQQ",
                sec_type="STK",
                quantity=50.0,
                avg_cost=400.0,
                fetched_at_ms=1,
            ),
        ],
        fetched_at_ms=1,
    )

    liquidated = await _recovery_flatten(broker)

    # First position's place_order raised; second succeeded.
    assert liquidated == 1


def test_is_recovery_readonly_args_true_client_false_returns_true() -> None:
    """CLI --readonly must win even when IBKR_READONLY env var is false.

    Regression (CodeRabbit on PR #237): ``IbkrClient()`` reads settings
    from env only, so ``client.settings.readonly`` does NOT reflect the
    CLI ``--readonly`` flag. The original guard checked only the client
    side, which means an operator who set ``--readonly`` on the command
    line with ``IBKR_READONLY=false`` in ``.env`` would still get a
    real recovery-flatten on engine crash — defeating the explicit CLI
    intent. The helper must OR the two signals.
    """
    assert _is_recovery_readonly(_Args(readonly=True), _ClientWithSettings(readonly=False)) is True


def test_is_recovery_readonly_args_false_client_true_returns_true() -> None:
    """Env-var readonly still applies when no CLI flag is set."""
    assert _is_recovery_readonly(_Args(readonly=False), _ClientWithSettings(readonly=True)) is True


def test_is_recovery_readonly_both_false_returns_false() -> None:
    assert _is_recovery_readonly(_Args(readonly=False), _ClientWithSettings(readonly=False)) is False


def test_is_recovery_readonly_both_true_returns_true() -> None:
    assert _is_recovery_readonly(_Args(readonly=True), _ClientWithSettings(readonly=True)) is True


def test_is_recovery_readonly_no_client_uses_args_only() -> None:
    """When no client is in scope (early test paths) args.readonly is authoritative."""
    assert _is_recovery_readonly(_Args(readonly=True), None) is True
    assert _is_recovery_readonly(_Args(readonly=False), None) is False


def test_is_recovery_readonly_client_without_settings_falls_back_to_args() -> None:
    """A malformed client missing .settings must not raise AttributeError."""

    class _ClientNoSettings:
        pass

    assert _is_recovery_readonly(_Args(readonly=True), _ClientNoSettings()) is True
    assert _is_recovery_readonly(_Args(readonly=False), _ClientNoSettings()) is False


def test_resolve_recovery_broker_prefers_injected_broker() -> None:
    fake = FakeBroker()
    resolved = _resolve_recovery_broker(fake, None)
    assert resolved is fake


def test_resolve_recovery_broker_returns_none_when_no_broker_no_client() -> None:
    assert _resolve_recovery_broker(None, None) is None


def test_resolve_recovery_broker_returns_none_when_client_disconnected() -> None:
    class _DisconnectedClient:
        def is_connected(self) -> bool:
            return False

    # A disconnected client should yield None regardless of broker presence
    # — recovery_flatten can't operate without a live broker session.
    assert _resolve_recovery_broker(None, _DisconnectedClient()) is None
    assert _resolve_recovery_broker(FakeBroker(), _DisconnectedClient()) is None


def test_resolve_recovery_broker_returns_engine_broker_to_preserve_owned_order_ids() -> None:
    """Recovery flatten must use the SAME broker instance the engine ran with.

    Reviewer feedback (P1.2): the prior implementation, when no test
    ``broker_arg`` was supplied, wrapped the client in a FRESH
    ``IbkrBrokerAdapter`` whose ``_owned_order_ids`` was empty. Calling
    ``cancel_open_orders`` on that fresh adapter is a no-op against
    the runner's actual in-flight orders, so recovery would submit
    liquidations while the original orders kept working — double-state
    on the account.

    The fix: cmd_start now constructs the broker explicitly and passes
    the same instance to both ``LiveEngine(broker=...)`` and the
    recovery flatten via ``_resolve_recovery_broker(broker, client)``.
    This test pins the helper's identity contract.
    """

    class _ConnectedClient:
        def is_connected(self) -> bool:
            return True

    engine_broker = FakeBroker()
    resolved = _resolve_recovery_broker(engine_broker, _ConnectedClient())
    assert resolved is engine_broker  # SAME instance, not a fresh adapter
