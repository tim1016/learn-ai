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
from app.engine.live.live_state_sidecar import LiveStateEnvelope, LiveStateSidecarRepo
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


class DeferredPermIdBroker(FakeBroker):
    """Models real IBKR permId timing.

    ``IB.placeOrder`` returns synchronously while the order is still
    ``PendingSubmit`` — IBKR has not assigned a ``permId`` yet; it arrives a
    beat later on the ``openOrder`` callback (together with the move out of
    ``PendingSubmit``). So a placement that does NOT wait sees ``perm_id is
    None``; only a caller that opts into the permId wait (``perm_id_wait_s >
    0``) gets the stable id. The recovery path must opt in, otherwise it
    records ``None`` and the next same-account relaunch cannot recognize the
    replayed recovery fill as its own.
    """

    REAL_PERM_ID = 1176469133

    async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
        ack = await super().place_order(spec)  # perm_id=None, PendingSubmit
        if perm_id_wait_s > 0:
            return ack.model_copy(
                update={"perm_id": self.REAL_PERM_ID, "status": "PreSubmitted"}
            )
        return ack


@pytest.mark.asyncio
async def test_recovery_flatten_records_real_perm_id_to_live_state_sidecar(tmp_path) -> None:
    """Recovery-flatten orders must be recognizable on same-account relaunch.

    Regression: the recovery path recorded the *synchronous* ack's permId —
    which IBKR has not assigned yet at PendingSubmit, so it persisted
    ``None``. IBKR then replayed the resulting execution on relaunch with a
    real permId and no client_order_id, and the outside-mutation guard had
    nothing to match it against, fatal-halting the bot on its own recovery
    fill until the session date rolled.

    The fix: the recovery path opts into the permId wait, so the durable trail
    captures the stable permId the replayed execution will carry.
    """
    sidecar_path = tmp_path / "live_state.json"
    repo = LiveStateSidecarRepo(sidecar_path)
    repo.write(
        LiveStateEnvelope(
            strategy_instance_id="spy_ema_crossover",
            run_id="run-fixture",
            bot_order_namespace="learn-ai/spy_ema_crossover/v1",
            ib_client_id=42,
            last_processed_bar_ms=1_780_000_000_000,
            last_artifact_flush_ms=1_780_000_000_500,
        )
    )
    broker = DeferredPermIdBroker()
    _seed_position(broker, "SPY", 100.0)

    liquidated = await _recovery_flatten(broker, live_state_path=sidecar_path)

    assert liquidated == 1
    loaded = repo.read()
    assert loaded is not None
    assert loaded.known_perm_ids == [DeferredPermIdBroker.REAL_PERM_ID]
    [client_order_id] = loaded.submitted_orders.keys()
    assert client_order_id.startswith("recovery-flatten-SPY-")
    assert (
        loaded.submitted_orders[client_order_id]["perm_id"]
        == DeferredPermIdBroker.REAL_PERM_ID
    )


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

        async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
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

        async def place_order(self, spec, *, perm_id_wait_s: float = 0.0):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("simulated place_order failure on first call")
            return await super().place_order(spec, perm_id_wait_s=perm_id_wait_s)

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
