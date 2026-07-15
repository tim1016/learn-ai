"""Phase 5C cancel-confirm timeout / VCR-0002.

PRD §5C step 4-5: every managed cancel/flatten path follows cancel →
wait for terminal confirms → reconcile owned fills → liquidate. When the
broker can't confirm cancels within ``CANCEL_CONFIRM_TIMEOUT_S``, the engine writes
``halt.flag`` (CANCEL_CONFIRM_TIMEOUT_HALT) and refuses to liquidate —
submitting market liquidations against a broker we can't confirm
cancel state with would race the just-issued cancels.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.execution.order import Direction, OrderEvent
from app.engine.live.config import LiveConfig
from app.engine.live.halt import FatalHaltError, PoisonedHaltReason, PoisonedHaltTrigger
from app.engine.live.live_engine import (
    CancelConfirmTimeoutHaltError,
    LiveEngine,
)
from tests.engine.live.fixtures.fake_broker import FakeBroker


async def _no_reconcile() -> None:
    return None


class _HangingCancelBroker(FakeBroker):
    """Cancel_open_orders awaits forever — exercises the timeout path."""

    async def cancel_open_orders(self) -> list[int]:
        await asyncio.sleep(10.0)
        return []


class _LateFillDuringCancelBroker(FakeBroker):
    """An owned exit fill becomes terminal while cancellation is in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_confirmed = False

    async def cancel_open_orders(self) -> list[int]:
        self.cancel_confirmed = True
        return [7]


class _AccountNetMustNotSizeInstanceFlattenBroker(FakeBroker):
    """Fails if instance shutdown tries to consume account-net positions."""

    async def fetch_positions(self):
        raise AssertionError("account-net positions are not an instance ownership ledger")


class _DirectCancelForbiddenBroker(FakeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.direct_cancel_attempts = 0

    async def cancel_open_orders(self) -> list[int]:
        self.direct_cancel_attempts += 1
        raise AssertionError("Clerk-mode cancellation must not call the broker directly")


@pytest.mark.asyncio
async def test_flatten_halts_on_cancel_confirm_timeout_vcr_0002(
    tmp_path: Path,
) -> None:
    """The load-bearing safety: a flatten triggered while the broker
    can't confirm cancels writes halt.flag and raises rather than
    liquidating into the uncertain cancel state."""
    broker = _HangingCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.05,  # short timeout for the test
    )
    # Direct exercise of the _flatten path with a hanging cancel.
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    with pytest.raises(CancelConfirmTimeoutHaltError) as exc:
        await engine._flatten(
            portfolio,
            ctx,
            bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
            reconcile_owned_state=_no_reconcile,
        )  # type: ignore[arg-type]

    assert exc.value.timeout_s == 0.05
    # halt.flag carries the timeout marker so the failure list can label it.
    halt_path = tmp_path / "halt.flag"
    assert halt_path.exists()
    payload = halt_path.read_text(encoding="utf-8")
    assert "CANCEL_CONFIRM_TIMEOUT_HALT" in payload
    assert "path=_flatten" in payload
    # No liquidation orders may be submitted — the broker stays untouched.
    assert broker.orders == []


@pytest.mark.asyncio
async def test_flatten_proceeds_when_cancel_completes_in_time(
    tmp_path: Path,
) -> None:
    """The fast path: a broker that confirms cancels within the window
    proceeds to liquidation as before. Verifies the timeout isn't
    overzealous."""
    broker = FakeBroker()  # cancel_open_orders returns immediately
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100  # something to liquidate
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    acks = await engine._flatten(
        portfolio,
        ctx,
        bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        reconcile_owned_state=_no_reconcile,
    )  # type: ignore[arg-type]

    # One liquidation order submitted; halt.flag NOT written.
    assert len(acks) == 1
    assert not (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_flatten_routes_clerk_mode_cancellation_through_namespace_receipt(
    tmp_path: Path,
) -> None:
    broker = _DirectCancelForbiddenBroker()
    received = []

    async def cancel_through_clerk(intent):
        received.append(intent)
        return type("Receipt", (), {"cancelled_order_ids": (1046,)})()

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        strategy_instance_id="bot-a",
        run_id="run-a",
        account_clerk_namespace_canceller=cancel_through_clerk,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    ctx = type("ctx", (), {"log": lambda self_, msg: None})()
    acks = await engine._flatten(
        LivePortfolio(broker),
        ctx,
        bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        reconcile_owned_state=_no_reconcile,
    )  # type: ignore[arg-type]

    assert acks == []
    assert broker.direct_cancel_attempts == 0
    assert len(received) == 1
    assert received[0].intent_kind == "CANCEL_NAMESPACE"
    assert received[0].bot_order_namespace == "learn-ai/bot-a/v1"


@pytest.mark.asyncio
async def test_fatal_halt_routes_cancellation_through_clerk_namespace_receipt(
    tmp_path: Path,
) -> None:
    """Fatal halt shares the Clerk cancel lane with managed flatten."""
    broker = _DirectCancelForbiddenBroker()
    received = []

    async def cancel_through_clerk(intent):
        received.append(intent)
        return type("Receipt", (), {"cancelled_order_ids": (1047,)})()

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        strategy_instance_id="bot-a",
        run_id="run-a",
        account_clerk_namespace_canceller=cancel_through_clerk,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    with pytest.raises(FatalHaltError):
        await engine._fatal_halt(
            PoisonedHaltReason(
                trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
                halted_at_ms=1,
                last_clean_bar_close_ms=0,
            ),
            portfolio=LivePortfolio(broker),
            writers=None,
        )

    assert broker.direct_cancel_attempts == 0
    assert len(received) == 1
    assert received[0].intent_kind == "CANCEL_NAMESPACE"
    assert received[0].bot_order_namespace == "learn-ai/bot-a/v1"


@pytest.mark.asyncio
async def test_flatten_reconciles_terminal_owned_fill_before_sizing(
    tmp_path: Path,
) -> None:
    """A late exit fill must not turn graceful flatten into a duplicate sell."""
    broker = _LateFillDuringCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    async def reconcile_owned_state() -> None:
        assert broker.cancel_confirmed
        portfolio.record_broker_fill(
            OrderEvent(
                order_id=7,
                symbol="SPY",
                time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
                fill_price=Decimal("500"),
                fill_quantity=-100,
                direction=Direction.SHORT,
                fee=Decimal("0"),
                tag="owned-exit",
            )
        )

    acks = await engine._flatten(
        portfolio,
        ctx,
        bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        reconcile_owned_state=reconcile_owned_state,
    )  # type: ignore[arg-type]

    assert acks == []
    assert broker.orders == []
    assert portfolio.positions["SPY"].quantity == 0


@pytest.mark.asyncio
async def test_flatten_uses_namespace_owned_sign_for_short_position(
    tmp_path: Path,
) -> None:
    """A namespace-owned short position must be closed with a buy."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = -50
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    acks = await engine._flatten(
        portfolio,
        ctx,
        bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        reconcile_owned_state=_no_reconcile,
    )  # type: ignore[arg-type]

    assert len(acks) == 1
    [order] = broker.orders
    assert order.action == "BUY"
    assert order.quantity == 50


@pytest.mark.asyncio
async def test_flatten_never_liquidates_same_account_sibling_exposure(
    tmp_path: Path,
) -> None:
    """One bot owns SPY +100 while the account-net position is SPY +150."""
    broker = _AccountNetMustNotSizeInstanceFlattenBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    acks = await engine._flatten(
        portfolio,
        ctx,
        bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        reconcile_owned_state=_no_reconcile,
    )  # type: ignore[arg-type]

    assert len(acks) == 1
    [order] = broker.orders
    assert order.action == "SELL"
    assert order.quantity == 100


@pytest.mark.asyncio
async def test_flatten_fails_closed_when_cancel_confirmation_errors(
    tmp_path: Path,
) -> None:
    """An unconfirmed cancellation must never be followed by liquidation."""

    class _RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("transient broker error")

    broker = _RaisingCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    with pytest.raises(RuntimeError, match="transient broker error"):
        await engine._flatten(
            portfolio,
            ctx,
            bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
            reconcile_owned_state=_no_reconcile,
        )  # type: ignore[arg-type]

    assert broker.orders == []
    assert not (tmp_path / "halt.flag").exists()
