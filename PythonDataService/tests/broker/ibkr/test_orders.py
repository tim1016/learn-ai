"""Tests for app.broker.ibkr.orders — paper-mode safety + dispatch.

The four safety layers all live in ``_enforce_paper_safety``; each test
flips one layer to a bad value and asserts ``OrderRefusedError`` before
``placeOrder`` is reached. ib_async types are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.models import IbkrOrderSpec
from app.broker.ibkr.orders import (
    OrderNotFoundError,
    OrderRefusedError,
    _enforce_paper_safety,
    _idempotency_clear_for_testing,
    cancel_paper_order,
    list_open_orders,
    place_paper_order,
    stream_order_events,
)


def _spec(**overrides) -> IbkrOrderSpec:
    base = {
        "symbol": "SPY",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 1.0,
        "order_type": "MKT",
        "limit_price": None,
        "time_in_force": "DAY",
        "expiry_ms": None,
        "strike": None,
        "right": None,
        "multiplier": 100,
        "confirm_paper": True,
    }
    base.update(overrides)
    return IbkrOrderSpec(**base)


def _client(
    *,
    account: str = "DU1234567",
    mode: str = "paper",
    port: int = 4002,
    readonly: bool = False,
    place_order_return=None,
) -> SimpleNamespace:
    settings = IbkrSettings(mode=mode, port=port, readonly=readonly, _env_file=None)
    qualified = SimpleNamespace(conId=12345)
    place_order_return = place_order_return or SimpleNamespace(
        order=SimpleNamespace(orderId=42, permId=99),
        orderStatus=SimpleNamespace(status="PendingSubmit"),
    )
    ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[qualified]),
        placeOrder=MagicMock(return_value=place_order_return),
    )
    return SimpleNamespace(
        ib=ib,
        settings=settings,
        connected_account=account,
        is_connected=lambda: True,
        require_connected=lambda: None,
        require_live=lambda: None,
    )


# ── safety layers ──────────────────────────────────────────────────────


def test_enforce_paper_safety_passes_in_clean_paper_mode() -> None:
    out = _enforce_paper_safety(_client(), _spec())
    assert out == "DU1234567"


def test_enforce_paper_safety_refuses_when_mode_is_live() -> None:
    with pytest.raises(OrderRefusedError, match="IBKR_MODE"):
        _enforce_paper_safety(_client(mode="live", port=4001), _spec())


def test_enforce_paper_safety_refuses_when_account_is_live_despite_paper_mode() -> None:
    """Even if env says paper, a U-prefix account must be refused."""
    with pytest.raises(OrderRefusedError, match="DU"):
        _enforce_paper_safety(_client(account="U7654321"), _spec())


def test_enforce_paper_safety_refuses_when_confirm_paper_is_false() -> None:
    with pytest.raises(OrderRefusedError, match="confirm_paper"):
        _enforce_paper_safety(_client(), _spec(confirm_paper=False))


def test_enforce_paper_safety_refuses_when_readonly_is_true() -> None:
    # Regression: IBKR_READONLY was documented as the protocol-level gate
    # but ib_async's connect-time `readonly` flag does not actually block
    # placeOrder. Enforcement now lives in our own code as Layer 0.
    with pytest.raises(OrderRefusedError, match="IBKR_READONLY"):
        _enforce_paper_safety(_client(readonly=True), _spec())


# ── place_paper_order happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_place_paper_order_market_buy_returns_ack() -> None:
    client = _client()
    ack = await place_paper_order(client, _spec(action="BUY", quantity=10))

    assert ack.account_id == "DU1234567"
    assert ack.is_paper is True
    assert ack.order_id == 42
    assert ack.perm_id == 99
    assert ack.symbol == "SPY"
    assert ack.action == "BUY"
    assert ack.quantity == 10.0
    assert ack.order_type == "MKT"
    assert ack.limit_price is None
    assert ack.status == "PendingSubmit"
    assert ack.placed_at_ms > 0
    client.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_place_paper_order_limit_sell_passes_price_through() -> None:
    client = _client()
    ack = await place_paper_order(
        client,
        _spec(action="SELL", quantity=2, order_type="LMT", limit_price=421.50),
    )
    assert ack.action == "SELL"
    assert ack.order_type == "LMT"
    assert ack.limit_price == 421.50

    # Inspect the order that was submitted
    submitted_order = client.ib.placeOrder.call_args.args[1]
    assert submitted_order.action == "SELL"
    assert submitted_order.totalQuantity == 2
    assert float(submitted_order.lmtPrice) == 421.50
    assert submitted_order.tif == "DAY"


@pytest.mark.asyncio
async def test_place_paper_order_option_requires_expiry_strike_right() -> None:
    client = _client()
    bad = _spec(sec_type="OPT")  # missing expiry_ms / strike / right
    with pytest.raises(OrderRefusedError, match="OPT order requires"):
        await place_paper_order(client, bad)


@pytest.mark.asyncio
async def test_place_paper_order_lmt_requires_limit_price() -> None:
    client = _client()
    bad = _spec(order_type="LMT", limit_price=None)
    # Pydantic itself rejects limit_price=None when order_type=LMT? Actually no,
    # we validate that in code. The model lets it through (gt=0 only when
    # provided), so the OrderRefusedError fires from _build_order.
    with pytest.raises(OrderRefusedError, match="LMT order requires limit_price"):
        await place_paper_order(client, bad)


# ── safety layers route into place_paper_order ────────────────────────


@pytest.mark.asyncio
async def test_place_paper_order_refuses_in_live_mode() -> None:
    client = _client(mode="live", port=4001)
    with pytest.raises(OrderRefusedError):
        await place_paper_order(client, _spec())
    # placeOrder must never have been called
    client.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_place_paper_order_refuses_when_confirm_missing() -> None:
    client = _client()
    with pytest.raises(OrderRefusedError, match="confirm_paper"):
        await place_paper_order(client, _spec(confirm_paper=False))
    client.ib.placeOrder.assert_not_called()


# ── Phase 3b: idempotency ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_idempotency_cache():
    _idempotency_clear_for_testing()
    yield
    _idempotency_clear_for_testing()


@pytest.mark.asyncio
async def test_place_paper_order_idempotency_replay_returns_cached_ack() -> None:
    client = _client()
    spec = _spec(client_order_id="abc-123")

    first = await place_paper_order(client, spec)
    second = await place_paper_order(client, spec)

    assert client.ib.placeOrder.call_count == 1
    assert first.order_id == second.order_id
    assert first.placed_at_ms == second.placed_at_ms


@pytest.mark.asyncio
async def test_place_paper_order_no_client_order_id_does_not_dedupe() -> None:
    client = _client()
    await place_paper_order(client, _spec())
    await place_paper_order(client, _spec())
    assert client.ib.placeOrder.call_count == 2


# ── Phase 3b: list_open_orders / cancel ───────────────────────────────


def _trade_namespace(*, account="DU1234567", order_id=42, perm_id=99,
                     symbol="SPY", sec_type="STK", action="BUY",
                     quantity=10.0, lmt_price=0.0, tif="DAY",
                     status_str="Submitted", filled=0.0, remaining=10.0,
                     avg_fill_price=0.0, con_id=12345):
    contract = SimpleNamespace(secType=sec_type, conId=con_id, symbol=symbol)
    order = SimpleNamespace(
        account=account, orderId=order_id, permId=perm_id,
        action=action, totalQuantity=quantity, lmtPrice=lmt_price, tif=tif,
    )
    order_status = SimpleNamespace(
        status=status_str, filled=filled, remaining=remaining,
        avgFillPrice=avg_fill_price,
    )
    return SimpleNamespace(
        contract=contract, order=order,
        orderStatus=order_status, fills=[],
    )


@pytest.mark.asyncio
async def test_list_open_orders_filters_to_connected_account() -> None:
    trades = [
        _trade_namespace(order_id=42, account="DU1234567"),
        _trade_namespace(order_id=43, account="DU9999999"),
    ]
    client = _client()
    client.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=trades)

    out = await list_open_orders(client)
    assert len(out) == 1
    assert out[0].order_id == 42


@pytest.mark.asyncio
async def test_list_open_orders_includes_own_orders_with_empty_account() -> None:
    """Regression (#441): orders we place don't set order.account (ib_async
    leaves it ""), so an empty account is OUR order and must be included — not
    filtered out as foreign. Previously /orders/open hid our own held orders."""
    trades = [
        _trade_namespace(order_id=44, account=""),  # our own order, account unset
        _trade_namespace(order_id=45, account="DU9999999"),  # genuinely foreign
    ]
    client = _client()
    client.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=trades)

    out = await list_open_orders(client)
    assert [o.order_id for o in out] == [44]


@pytest.mark.asyncio
async def test_cancel_paper_order_calls_cancel_order_with_matching_trade() -> None:
    trade = _trade_namespace(order_id=42)
    client = _client()
    client.ib.trades = MagicMock(return_value=[trade])
    client.ib.cancelOrder = MagicMock()

    out = await cancel_paper_order(client, order_id=42)
    assert out.order_id == 42
    client.ib.cancelOrder.assert_called_once_with(trade.order)


@pytest.mark.asyncio
async def test_cancel_paper_order_raises_when_order_not_found() -> None:
    client = _client()
    client.ib.trades = MagicMock(return_value=[])
    client.ib.cancelOrder = MagicMock()

    with pytest.raises(OrderNotFoundError):
        await cancel_paper_order(client, order_id=42)
    client.ib.cancelOrder.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_paper_order_refuses_in_live_mode() -> None:
    client = _client(mode="live", port=4001)
    client.ib.trades = MagicMock(return_value=[_trade_namespace(order_id=42)])
    client.ib.cancelOrder = MagicMock()

    with pytest.raises(OrderRefusedError):
        await cancel_paper_order(client, order_id=42)
    client.ib.cancelOrder.assert_not_called()


# ── Phase 3b: stream_order_events ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_order_events_emits_status_transitions() -> None:
    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    trade_v2 = _trade_namespace(order_id=42, status_str="Filled", filled=10, remaining=0)
    snapshots = iter([[trade_v1], [trade_v2], [trade_v2]])

    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 2:
            break

    assert out[0].status == "Submitted"
    assert out[1].status == "Filled"


@pytest.mark.asyncio
async def test_stream_order_events_attributes_own_orders_with_empty_account() -> None:
    """Regression (#441): the lost-fill incident. Orders we place leave
    order.account == "" (ib_async default), and the old filter
    ``order.account != account_id`` dropped them ("" != "DU…") — so the engine
    never saw its own fills, the position tally stayed 0 (→ "unattributed"
    contamination), and a false lost-fill fatal halt fired. Our own order
    (empty account) must be streamed, not skipped."""
    trade_v1 = _trade_namespace(order_id=7, account="", status_str="Submitted")
    trade_v2 = _trade_namespace(
        order_id=7, account="", status_str="Filled", filled=1366, remaining=0
    )
    snapshots = iter([[trade_v1], [trade_v2], [trade_v2]])
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 2:
            break

    assert [e.status for e in out] == ["Submitted", "Filled"]


@pytest.mark.asyncio
async def test_stream_order_events_still_skips_genuinely_foreign_account() -> None:
    """A *non-empty* account that differs from the connected one is a real
    foreign order (another client on the gateway) and stays filtered."""
    foreign = _trade_namespace(order_id=99, account="DU9999999", status_str="Filled")
    own = _trade_namespace(order_id=7, account="", status_str="Filled", filled=10, remaining=0)
    snapshots = iter([[foreign, own], [foreign, own], [foreign, own]])
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 1:
            break

    # Only the own (empty-account) order is streamed; the foreign one is skipped.
    assert all(e.order_id == 7 for e in out)


@pytest.mark.asyncio
async def test_stream_order_events_populates_exec_id_and_client_id_on_fill() -> None:
    """Phase C-2c-b2-i: fill events surface execId + clientId so the live
    runtime's § 7 outside-mutation halt can index by broker primary keys."""
    # First snapshot: order placed, no fill yet.
    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    # Second snapshot: order filled with an execution row carrying
    # execId="exec-abc-1" and clientId=42 (our owning client).
    fill_exec = SimpleNamespace(
        execId="exec-abc-1",
        clientId=42,
        shares=10.0,
        price=500.5,
    )
    trade_v2 = _trade_namespace(
        order_id=42, status_str="Filled", filled=10, remaining=0
    )
    trade_v2.fills = [SimpleNamespace(execution=fill_exec)]
    # Use itertools.chain to terminate with infinite repeats of the
    # final snapshot, avoiding StopIteration if extra polls occur.
    from itertools import chain, repeat

    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    # Collect 3 events: Submitted status, Filled status, and the fill event.
    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 3:
            break

    fill_event = next(e for e in out if e.event_type == "fill")
    assert fill_event.exec_id == "exec-abc-1"
    assert fill_event.client_id == 42
    assert fill_event.fill_quantity == 10.0
    assert fill_event.last_fill_price == 500.5


@pytest.mark.asyncio
async def test_stream_order_events_halts_on_disconnect() -> None:
    """Regression (B-03): a mid-stream disconnect must surface, not be hidden
    by an idle poll of a frozen trades() cache.

    ib_async's ``trades()`` never raises when disconnected, so the loop now
    calls ``require_live()`` each iteration. When that raises, the order-event
    stream stops and the engine learns the feed is dead instead of believing
    in-flight orders are still pending forever."""
    from app.broker.ibkr.client import NotConnectedError

    client = _client()
    # Connection drops before the first poll.
    client.require_live = MagicMock(side_effect=NotConnectedError("connection lost"))
    client.ib.trades = MagicMock(return_value=[])

    with pytest.raises(NotConnectedError):
        async for _event in stream_order_events(client, poll_seconds=0.001):
            pass

    # The frozen cache was never polled once liveness failed.
    client.ib.trades.assert_not_called()


@pytest.mark.asyncio
async def test_stream_order_events_handles_fill_with_no_execution_object() -> None:
    """Defensive: a Fill without an execution attribute (degenerate ib_async
    state) yields a fill event with exec_id=None / client_id=None rather
    than crashing. The downstream halt check treats null client_order_id
    as foreign by definition, so this still surfaces correctly."""
    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    trade_v2 = _trade_namespace(order_id=42, status_str="Filled", filled=10, remaining=0)
    trade_v2.fills = [SimpleNamespace(execution=None)]
    # Use itertools.chain to terminate with infinite repeats of the
    # final snapshot, avoiding StopIteration if extra polls occur.
    from itertools import chain, repeat

    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 3:
            break

    fill_event = next(e for e in out if e.event_type == "fill")
    assert fill_event.exec_id is None
    assert fill_event.client_id is None
