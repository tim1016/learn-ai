"""Tests for app.broker.ibkr.orders — paper-mode safety + dispatch.

The four safety layers all live in ``_enforce_paper_safety``; each test
flips one layer to a bad value and asserts ``OrderRefusedError`` before
``placeOrder`` is reached. ib_async types are stubbed.
"""

from __future__ import annotations

import asyncio
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
    # ADR 0008 / Phase 5B — ``place_paper_order`` refuses a spec without
    # ``order_ref``, so the default test spec carries a syntactically valid
    # token. Tests that exercise the precondition itself pass ``order_ref=None``.
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
        "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
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


@pytest.mark.asyncio
async def test_place_paper_order_refuses_missing_order_ref() -> None:
    """ADR 0008 / Phase 5B / VCR-0002 — a real-broker placement cannot bypass
    durable-submit identity by omitting ``order_ref``. ``place_paper_order``
    refuses before any IBKR call so the protocol exists structurally, not as
    a soft convention."""
    client = _client()
    bad = _spec(order_ref=None)
    with pytest.raises(OrderRefusedError, match="ADR 0008"):
        await place_paper_order(client, bad)
    client.ib.placeOrder.assert_not_called()


# ── safety layers route into place_paper_order ────────────────────────


@pytest.mark.asyncio
async def test_place_paper_order_qualify_timeout_raises_and_does_not_place(monkeypatch) -> None:
    """Regression (B-06): a hung qualifyContractsAsync must time out into a
    BrokerError instead of hanging the caller (and the live bar loop) forever.
    Before the fix the await had no timeout."""
    from app.broker.ibkr import orders as orders_mod
    from app.broker.ibkr.client import BrokerError

    monkeypatch.setattr(orders_mod, "_QUALIFY_TIMEOUT_S", 0.01)
    client = _client()

    async def _hanging_qualify(contract):
        await asyncio.sleep(1.0)  # never completes within the timeout
        return [contract]

    client.ib.qualifyContractsAsync = _hanging_qualify

    with pytest.raises(BrokerError, match="timed out"):
        await place_paper_order(client, _spec())
    client.ib.placeOrder.assert_not_called()


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


@pytest.mark.asyncio
async def test_place_paper_order_refuses_on_soft_connectivity_loss() -> None:
    """Codex P1 on PR #563 — during a TWS 1100 soft loss the socket is
    still open (``is_connected()`` returns True) but the data feed is
    dead. ``require_live`` is the gate that catches this; the old code
    used ``require_connected`` which ignored the soft-loss flag, so a
    paper order could land on a dead feed while the monitor was still
    trying to reconnect."""
    from app.broker.ibkr.client import NotConnectedError

    client = _client()

    def _refuse() -> None:
        raise NotConnectedError(
            "IBKR connectivity lost (TWS error 1100): the API socket is "
            "open but the data feed is down."
        )

    client.require_live = _refuse  # type: ignore[assignment]
    with pytest.raises(NotConnectedError, match="connectivity lost"):
        await place_paper_order(client, _spec())
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


@pytest.mark.asyncio
async def test_place_paper_order_concurrent_same_id_places_once() -> None:
    """Regression (B-01): two concurrent requests carrying the same
    client_order_id must place exactly one order.

    Before the per-key lock, both coroutines passed the empty-cache lookup,
    both awaited qualify (a real suspension point), and both reached
    placeOrder — submitting two real orders for one intended order. The
    sleeping qualify below forces the interleave the production await causes.
    """
    client = _client()

    async def _slow_qualify(contract):
        # A real suspension so the two placements genuinely interleave under
        # asyncio.gather, reproducing the production qualify round-trip.
        await asyncio.sleep(0)
        contract.conId = 12345
        return [contract]

    client.ib.qualifyContractsAsync = _slow_qualify
    spec = _spec(client_order_id="dup-1")

    acks = await asyncio.gather(
        place_paper_order(client, spec),
        place_paper_order(client, spec),
    )

    assert client.ib.placeOrder.call_count == 1
    assert acks[0].order_id == acks[1].order_id == 42


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
async def test_cancel_paper_order_refuses_foreign_order_on_same_account() -> None:
    """Regression (B-05): a matching orderId belonging to another client on the
    same DU account must NOT be cancelled. ib_async's trades() cache can hold
    foreign orders, and orderIds are small per-client integers that collide, so
    cancel must apply the same ownership guard list/stream already use."""
    foreign = _trade_namespace(order_id=42, account="DU9999999")
    client = _client()
    client.ib.trades = MagicMock(return_value=[foreign])
    client.ib.cancelOrder = MagicMock()

    with pytest.raises(OrderNotFoundError):
        await cancel_paper_order(client, order_id=42)
    client.ib.cancelOrder.assert_not_called()


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
async def test_stream_order_events_round_trips_namespaced_order_ref() -> None:
    """ADR 0008 / Phase 5A: the deterministic ``{bot_order_namespace}:{intent_id}``
    token we stamp on outbound orders must round-trip back on both status and
    fill events, so the reconciliation publisher can join broker callbacks
    to engine intents by namespace. Empty-string ``orderRef`` (ib_async's
    "field absent" default) must coerce to ``None`` so a missing echo stays
    distinguishable from a present one."""
    from itertools import chain, repeat

    order_ref = "learn-ai/sid-abc/v1:intent-xyz"

    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    trade_v1.order.orderRef = order_ref
    trade_v2 = _trade_namespace(
        order_id=42, status_str="Filled", filled=10, remaining=0
    )
    trade_v2.order.orderRef = order_ref
    trade_v2.fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="exec-abc-1",
                clientId=42,
                shares=10.0,
                price=500.5,
                orderRef=order_ref,
            )
        )
    ]
    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 3:
            break

    status_events = [e for e in out if e.event_type != "fill"]
    fill_event = next(e for e in out if e.event_type == "fill")
    assert all(e.order_ref == order_ref for e in status_events)
    assert fill_event.order_ref == order_ref


@pytest.mark.asyncio
async def test_stream_order_events_populates_symbol_side_order_type() -> None:
    """ADR 0014 — the broker-activity reconciler treats these three
    fields as authoritative for the operator-facing row. Verify they
    round-trip from the underlying ib_async Trade/Order/Contract to
    every event the publisher consumes (status + fill)."""
    from itertools import chain, repeat

    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    trade_v1.order.orderType = "LMT"
    trade_v2 = _trade_namespace(
        order_id=42, status_str="Filled", filled=10, remaining=0
    )
    trade_v2.order.orderType = "LMT"
    trade_v2.fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="exec-symside-1",
                clientId=42,
                shares=10.0,
                price=500.5,
            )
        )
    ]
    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 3:
            break

    for event in out:
        assert event.symbol == "SPY"
        assert event.side == "BUY"
        assert event.order_type == "LMT"


@pytest.mark.asyncio
async def test_stream_order_events_treats_empty_order_ref_as_none() -> None:
    """ib_async's ``Execution.orderRef`` defaults to ``''`` when the broker
    omits the echo. The event must surface ``None`` (absent) rather than
    propagating the empty string, so the reconciliation publisher can treat
    "no echo" as definitively foreign instead of as a falsy-but-present token."""
    from itertools import chain, repeat

    trade_v1 = _trade_namespace(order_id=42, status_str="Submitted")
    trade_v2 = _trade_namespace(
        order_id=42, status_str="Filled", filled=10, remaining=0
    )
    trade_v2.fills = [
        SimpleNamespace(
            execution=SimpleNamespace(
                execId="exec-foreign-1",
                clientId=99,
                shares=10.0,
                price=500.5,
                orderRef="",
            )
        )
    ]
    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 3:
            break

    assert all(e.order_ref is None for e in out)


@pytest.mark.asyncio
async def test_stream_order_events_partial_fills_report_running_totals() -> None:
    """Regression (B-09): when two executions land in one poll window, each
    fill event must carry the running totals true *after that execution*, not
    the order's terminal orderStatus snapshot.

    Before the fix both collapsed events read cumulative_filled/remaining/
    avg_fill_price off the final orderStatus (200/0), so the first partial was
    mis-stamped as fully filled."""
    from itertools import chain, repeat

    trade_v1 = _trade_namespace(
        order_id=42, quantity=200, status_str="Submitted", filled=0.0, remaining=200.0
    )
    # Terminal snapshot: fully filled via two 100-share executions at 10 and 11.
    trade_v2 = _trade_namespace(
        order_id=42, quantity=200, status_str="Filled", filled=200.0, remaining=0.0
    )
    trade_v2.fills = [
        SimpleNamespace(execution=SimpleNamespace(execId="e1", clientId=1, shares=100.0, price=10.0)),
        SimpleNamespace(execution=SimpleNamespace(execId="e2", clientId=1, shares=100.0, price=11.0)),
    ]
    snapshots = chain([[trade_v1]], repeat([trade_v2]))
    client = _client()
    client.ib.trades = MagicMock(side_effect=lambda: next(snapshots))

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len([e for e in out if e.event_type == "fill"]) >= 2:
            break

    fills = [e for e in out if e.event_type == "fill"]
    # First execution: 100 filled, 100 remaining, avg = 10.
    assert fills[0].cumulative_filled == 100.0
    assert fills[0].remaining == 100.0
    assert fills[0].avg_fill_price == 10.0
    assert fills[0].fill_quantity == 100.0
    # Second execution: 200 filled, 0 remaining, running avg = (10+11)/2 = 10.5.
    assert fills[1].cumulative_filled == 200.0
    assert fills[1].remaining == 0.0
    assert fills[1].avg_fill_price == 10.5


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


# ── Slice 3 / ADR 0011 amendment — reconnect-recovery halt gate ───────


@pytest.mark.asyncio
async def test_place_paper_order_refused_during_reconnect_recovery_sweep(
    monkeypatch,
) -> None:
    """Slice 3: ``place_paper_order`` must refuse when any broker-activity
    publisher is mid reconnect-recovery sweep — the broker is replaying
    history and a new order would race the sweep's exec_id dedupe set.
    The check fires BEFORE ``require_live``, so even a fully-healthy
    connection is gated."""
    from app.broker.ibkr.orders import OrderRefusedDuringReconnectRecoveryError
    from app.services.broker_activity_publisher import get_publisher_registry

    client = _client()
    # Stub the registry so ``any_recovery_active`` returns True without
    # standing up a real publisher.
    registry = get_publisher_registry()
    monkeypatch.setattr(registry, "any_recovery_active", lambda: True)

    with pytest.raises(
        OrderRefusedDuringReconnectRecoveryError,
        match="reconnect-recovery sweep is in progress",
    ):
        await place_paper_order(client, _spec())
    client.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_place_paper_order_proceeds_when_no_sweep_active(
    monkeypatch,
) -> None:
    """Regression: the recovery-halt gate must default to allowing
    submission. A bug in ``any_recovery_active`` that pinned True would
    otherwise silently freeze every submission across every instance."""
    from app.services.broker_activity_publisher import get_publisher_registry

    client = _client()
    registry = get_publisher_registry()
    # Explicit stub to ensure no test ordering pollutes the gate.
    monkeypatch.setattr(registry, "any_recovery_active", lambda: False)

    ack = await place_paper_order(client, _spec())
    assert ack.order_id == 42
    client.ib.placeOrder.assert_called_once()
