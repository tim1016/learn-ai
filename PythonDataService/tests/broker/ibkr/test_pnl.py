"""Tests for app.broker.ibkr.pnl — tick conversion and stream lifecycle.

ib_async PnL / PnLSingle objects are stubbed with SimpleNamespace; the
stream tests use a fake IB whose reqPnL / reqPnLSingle return mutable
namespaces and whose cancel methods record their calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr.pnl import (
    _account_pnl_to_tick,
    _position_pnl_to_tick,
    stream_account_pnl,
    stream_position_pnl,
)

# ── tick conversion ────────────────────────────────────────────────────


def test_account_pnl_tick_pulls_three_pnl_fields() -> None:
    pnl = SimpleNamespace(dailyPnL=12.5, unrealizedPnL=100.0, realizedPnL=-5.0)
    tick = _account_pnl_to_tick(pnl, "DU1234567")
    assert tick.account_id == "DU1234567"
    assert tick.con_id is None
    assert tick.daily_pnl == 12.5
    assert tick.unrealized_pnl == 100.0
    assert tick.realized_pnl == -5.0
    assert tick.market_value is None
    assert tick.position is None
    assert tick.ts_ms > 0


def test_position_pnl_tick_pulls_market_value_and_position() -> None:
    pnl_single = SimpleNamespace(
        dailyPnL=2.0,
        unrealizedPnL=15.0,
        realizedPnL=0.0,
        value=420.0,
        position=2,
    )
    tick = _position_pnl_to_tick(pnl_single, "DU1234567", con_id=700001)
    assert tick.con_id == 700001
    assert tick.market_value == 420.0
    assert tick.position == 2.0


def test_account_pnl_tick_handles_missing_attributes() -> None:
    """Initial PnL snapshots can be missing fields; coercion returns None."""
    pnl = SimpleNamespace()
    tick = _account_pnl_to_tick(pnl, "DU1234567")
    assert tick.daily_pnl is None
    assert tick.unrealized_pnl is None
    assert tick.realized_pnl is None


# ── stream_account_pnl ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_account_pnl_emits_initial_then_iterates() -> None:
    pnl_obj = SimpleNamespace(dailyPnL=1.0, unrealizedPnL=2.0, realizedPnL=3.0)
    cancel_calls: list[str] = []
    ib = SimpleNamespace(
        reqPnL=lambda account, *args, **kw: pnl_obj,
        cancelPnL=lambda account: cancel_calls.append(account),
    )
    client = SimpleNamespace(
        ib=ib,
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
    )

    out = []
    async for tick in stream_account_pnl(client, debounce_seconds=0.001):
        out.append(tick)
        # Mutate underlying snapshot to verify we re-read
        pnl_obj.dailyPnL = 9.0
        if len(out) == 2:
            break

    assert len(out) == 2
    assert out[0].daily_pnl == 1.0  # initial snapshot
    assert out[1].daily_pnl == 9.0  # after the in-place mutation
    assert cancel_calls == ["DU1234567"]  # finally ran


@pytest.mark.asyncio
async def test_stream_account_pnl_cancels_even_on_consumer_break() -> None:
    pnl_obj = SimpleNamespace(dailyPnL=0.0, unrealizedPnL=0.0, realizedPnL=0.0)
    cancel_calls: list[str] = []
    ib = SimpleNamespace(
        reqPnL=lambda account, *args, **kw: pnl_obj,
        cancelPnL=lambda account: cancel_calls.append(account),
    )
    client = SimpleNamespace(
        ib=ib,
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
    )

    async for _tick in stream_account_pnl(client, debounce_seconds=0.001):
        break

    assert cancel_calls == ["DU1234567"]


# ── stream_position_pnl ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_position_pnl_emits_one_tick_per_contract_per_pass() -> None:
    by_id = {
        700001: SimpleNamespace(
            dailyPnL=1.0, unrealizedPnL=10.0, realizedPnL=0.0, value=100.0, position=1
        ),
        700002: SimpleNamespace(
            dailyPnL=-2.0, unrealizedPnL=20.0, realizedPnL=0.0, value=200.0, position=-2
        ),
    }
    cancel_calls: list[tuple[str, int]] = []

    def fake_req(account: str, model: str, con_id: int):
        return by_id[con_id]

    def fake_cancel(account: str, model: str, con_id: int):
        cancel_calls.append((account, con_id))

    ib = SimpleNamespace(reqPnLSingle=fake_req, cancelPnLSingle=fake_cancel)
    client = SimpleNamespace(
        ib=ib,
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
    )

    out = []
    async for tick in stream_position_pnl(
        client, [700001, 700002], debounce_seconds=0.001
    ):
        out.append(tick)
        if len(out) >= 4:
            break

    # First two ticks are the initial snapshots, one per contract.
    assert {out[0].con_id, out[1].con_id} == {700001, 700002}
    # Next pass also covers both contracts.
    assert {out[2].con_id, out[3].con_id} == {700001, 700002}
    # Finally cancelled both subscriptions.
    assert {c[1] for c in cancel_calls} == {700001, 700002}


@pytest.mark.asyncio
async def test_stream_position_pnl_continues_on_subscribe_failure() -> None:
    """One bad reqPnLSingle must not drop the rest of the subscriptions."""
    good = SimpleNamespace(
        dailyPnL=0.5, unrealizedPnL=1.0, realizedPnL=0.0, value=50.0, position=1
    )
    cancel_calls: list[int] = []

    def fake_req(account: str, model: str, con_id: int):
        if con_id == 700001:
            raise RuntimeError("simulated subscribe failure")
        return good

    def fake_cancel(account: str, model: str, con_id: int):
        cancel_calls.append(con_id)

    ib = SimpleNamespace(reqPnLSingle=fake_req, cancelPnLSingle=fake_cancel)
    client = SimpleNamespace(
        ib=ib,
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
    )

    out = []
    async for tick in stream_position_pnl(
        client, [700001, 700002], debounce_seconds=0.001
    ):
        out.append(tick)
        if len(out) >= 1:
            break

    # Only the second contract emitted.
    assert out[0].con_id == 700002
    # Only the second contract was cancelled (the first never subscribed).
    assert cancel_calls == [700002]
