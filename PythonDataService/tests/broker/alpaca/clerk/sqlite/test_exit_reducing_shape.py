"""The reducing leg carries the decision's shape durably, and a resumed submission rebuilds it identically."""

from __future__ import annotations

import logging

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
    repo,  # noqa: F401 — the shared EXIT-machine repository fixture
)

_XH_SELL = LegShape(
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.DAY,
    limit_price=99.80,
    extended_hours=True,
    side=OrderSide.SELL,
)


def test_reducing_facts_omit_the_shape_when_it_is_the_regular_default() -> None:
    regular = ExitReducingOrderCreatedFacts(symbol="SPY", side="SELL", quantity=10)

    # Byte-identical to what a pre-slice-3 writer produced, so every sealed
    # receipt over a regular-session reducing order still hashes the same.
    assert regular.to_facts_json() == '{"quantity":10,"side":"SELL","symbol":"SPY"}'
    assert (
        ExitReducingOrderCreatedFacts.from_facts_json(regular.to_facts_json()).to_facts_json()
        == regular.to_facts_json()
    )
    # A row written before slice 3 parses to the regular shape.
    legacy = ExitReducingOrderCreatedFacts.from_facts_json(
        '{"quantity": 10, "side": "SELL", "symbol": "SPY"}'
    )
    assert (legacy.order_type, legacy.time_in_force, legacy.limit_price, legacy.extended_hours) == (
        "market",
        "day",
        None,
        False,
    )


def test_an_extended_shape_is_carried_in_the_reducing_facts_json() -> None:
    extended = ExitReducingOrderCreatedFacts(
        symbol="SPY",
        side="SELL",
        quantity=10,
        order_type="limit",
        time_in_force="day",
        limit_price=99.80,
        extended_hours=True,
    )

    assert ExitReducingOrderCreatedFacts.from_facts_json(extended.to_facts_json()) == extended
    assert extended.to_facts_json() == (
        '{"extended_hours":true,"limit_price":99.8,"order_type":"limit",'
        '"quantity":10,"side":"SELL","symbol":"SPY"}'
    )


async def test_reducing_order_is_submitted_with_the_decision_shape_and_resubmitted_identically(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    # A lost submit acknowledgement: the leg reached the port, the answer did
    # not, so the reducing order holds no broker identity and the next pass
    # must rebuild the identical leg from the durable facts alone.
    first_trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await resolve_exit(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        trade=first_trade,
        reducing_shape=_XH_SELL,
    )
    assert first.reducing_order_ref is not None
    ((leg, _client_order_id),) = first_trade.submit_calls
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.80,
        True,
        OrderSide.SELL,
    )

    repo._clock.advance(31_000)  # type: ignore[attr-defined]
    second_trade = _FakeTrade(lookup_results=[None])
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=second_trade)

    assert second_trade.submit_calls[0][0] == leg


async def test_a_shape_for_the_other_side_falls_back_to_market(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    wrong_side = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=100.10,
        extended_hours=True,
        side=OrderSide.BUY,
    )
    trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted"))

    with caplog.at_level(logging.WARNING):
        await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
            reducing_shape=wrong_side,
        )

    ((leg, _),) = trade.submit_calls
    assert leg.order_type is OrderType.MARKET
    assert leg.extended_hours is False
    assert any(getattr(r, "action", None) == "reducing_leg_shape_side_mismatch" for r in caplog.records)
