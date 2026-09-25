"""The decision's reducing shape is durable with the EXIT, and survives every later pass.

Two durable records carry it, for two different reasons. ``EXIT_ACCEPTED``
holds it from the instant the decision is accepted, so a reduction created on
a *later* pass -- the reconciliation sweep, the watchdog, recovery -- still
builds the deciding program's leg. ``EXIT_REDUCING_ORDER_CREATED`` holds it
once the order exists, so a resumed submission rebuilds the identical leg.
"""

from __future__ import annotations

import logging

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg
from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    ConfirmedRecoveryShape,
)
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    priced_reduction_reference_price,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import OrderSide, OrderType, TimeInForce
from app.schemas.market_liveness import TopOfBookQuote
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    AFTER_HOURS_MS,
    POST_CLOSE_MS,
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
_REGULAR_SELL = LegShape(
    order_type=OrderType.MARKET,
    time_in_force=TimeInForce.DAY,
    limit_price=None,
    extended_hours=False,
    side=OrderSide.SELL,
)


def _accepted_facts(quantity: int | float = 10) -> ExitAcceptedFacts:
    del quantity
    return ExitAcceptedFacts(
        idempotency_key="sid:exit-1",
        payload_hash="a" * 64,
        kind="strategy_decision",
        action="EXIT",
        intended_end_state=None,
        effect_idempotency_key="exit:sid:exit-1",
        effect_kind="EXIT",
        decision_id="exit-1",
        entry_order_ref="entry-1",
        entry_order_refs=["entry-1"],
    )


def test_accepted_facts_omit_the_shape_for_every_exit_that_records_none() -> None:
    """The byte shape every EXIT_ACCEPTED row written before slice 3 has.

    A recovery EXIT (no deciding program) and a regular-session decision both
    record nothing: the regular shape is exactly what a reduction with no
    recorded shape already builds, so writing it out would change the
    canonical JSON -- and every sealed receipt hashed over it -- to say what
    the default already said.
    """
    plain = _accepted_facts()
    expected = (
        '{"action":"EXIT","decision_id":"exit-1","effect_idempotency_key":"exit:sid:exit-1",'
        '"effect_kind":"EXIT","entry_order_ref":"entry-1","entry_order_refs":["entry-1"],'
        '"idempotency_key":"sid:exit-1","intended_end_state":null,"kind":"strategy_decision",'
        '"payload_hash":"' + "a" * 64 + '"}'
    )
    assert plain.to_facts_json() == expected
    assert plain.with_reducing_shape(None).to_facts_json() == expected
    assert plain.with_reducing_shape(_REGULAR_SELL).to_facts_json() == expected
    # A row written before the field existed parses, and asks for no shape.
    assert ExitAcceptedFacts.from_facts_json(expected).reducing_shape() is None


def test_accepted_facts_carry_an_extended_shape_and_round_trip_it() -> None:
    recorded = _accepted_facts().with_reducing_shape(_XH_SELL)

    assert recorded.to_facts_json() == (
        '{"action":"EXIT","decision_id":"exit-1","effect_idempotency_key":"exit:sid:exit-1",'
        '"effect_kind":"EXIT","entry_order_ref":"entry-1","entry_order_refs":["entry-1"],'
        '"extended_hours":true,"idempotency_key":"sid:exit-1","intended_end_state":null,'
        '"kind":"strategy_decision","limit_price":99.8,"order_type":"limit",'
        '"payload_hash":"' + "a" * 64 + '","reducing_side":"sell"}'
    )
    assert ExitAcceptedFacts.from_facts_json(recorded.to_facts_json()) == recorded
    assert ExitAcceptedFacts.from_facts_json(recorded.to_facts_json()).reducing_shape() == _XH_SELL


def test_reducing_facts_omit_the_shape_when_it_is_the_regular_default() -> None:
    regular = ExitReducingOrderCreatedFacts(symbol="SPY", side="SELL", quantity=10)

    # Proves the four shape keys (order_type/time_in_force/limit_price/
    # extended_hours) are absent from the canonical JSON at their regular-
    # session defaults. `quantity=10` here is a Python int, so this pins the
    # int-quantity byte shape specifically; production always folds a float
    # `remaining_qty` (see the next assertion for the shape it actually writes).
    assert regular.to_facts_json() == '{"quantity":10,"side":"SELL","symbol":"SPY"}'
    # `_create_reducing_order` always writes a float quantity (`_resolve_claimed`'s
    # `remaining_qty` is a float subtraction) — this is the byte shape a real
    # reducing-order-created row hashes, so every sealed receipt over a
    # regular-session reducing order still hashes the same.
    float_regular = ExitReducingOrderCreatedFacts(symbol="SPY", side="SELL", quantity=10.0)
    assert float_regular.to_facts_json() == '{"quantity":10.0,"side":"SELL","symbol":"SPY"}'
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


@pytest.mark.parametrize(
    "written_before",
    [
        pytest.param('{"quantity":10.0,"side":"SELL","symbol":"SPY"}', id="a-regular-leg"),
        pytest.param(
            '{"extended_hours":true,"limit_price":99.8,"order_type":"limit",'
            '"quantity":10.0,"side":"SELL","symbol":"SPY","valid_until_ms":1700096400000}',
            id="an-extended-leg-with-its-bound",
        ),
    ],
)
def test_a_reducing_order_row_written_before_send_time_pricing_parses_and_hashes_the_same(
    written_before: str,
) -> None:
    """#2440 review: who priced a leg and its reference quote are omitted at their defaults.

    Every row written before the provenance fields existed parses to "priced
    as its acceptance recorded it" and re-serializes byte-identically, so no
    sealed receipt hashed over one changes.
    """
    parsed = ExitReducingOrderCreatedFacts.from_facts_json(written_before)

    assert parsed.priced_by is None
    assert parsed.reference_bid is None and parsed.reference_ask is None
    assert parsed.to_facts_json() == written_before


def test_a_leg_the_clerk_priced_at_send_carries_its_provenance_in_the_reducing_facts_json() -> None:
    priced = ExitReducingOrderCreatedFacts(
        symbol="SPY",
        side="SELL",
        quantity=10.0,
        order_type="limit",
        limit_price=99.80,
        extended_hours=True,
        valid_until_ms=1_700_096_400_000,
        priced_by="clerk",
        reference_bid=100.00,
        reference_ask=100.05,
        reference_quote_observed_at_ms=1_700_086_860_000,
    )

    assert priced.to_facts_json() == (
        '{"extended_hours":true,"limit_price":99.8,"order_type":"limit","priced_by":"clerk",'
        '"quantity":10.0,"reference_ask":100.05,"reference_bid":100.0,'
        '"reference_quote_observed_at_ms":1700086860000,"side":"SELL","symbol":"SPY",'
        '"valid_until_ms":1700096400000}'
    )
    assert ExitReducingOrderCreatedFacts.from_facts_json(priced.to_facts_json()) == priced


async def test_reducing_order_is_submitted_with_the_decision_shape_and_resubmitted_identically(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    # A POST-session decision, sendable until the declared close (#2440).
    _walk_clock_to(repo, AFTER_HOURS_MS)
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        program_leg=ProgramLeg(_XH_SELL, valid_until_ms=POST_CLOSE_MS),
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
        pricing=UNPRICEABLE_RECOVERY,
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
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=second_trade, pricing=UNPRICEABLE_RECOVERY)

    assert second_trade.submit_calls[0][0] == leg


async def test_a_shape_for_the_other_side_falls_back_to_market(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    wrong_side = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=100.10,
        extended_hours=True,
        side=OrderSide.BUY,
    )
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        program_leg=ProgramLeg(wrong_side, valid_until_ms=POST_CLOSE_MS),
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted"))

    with caplog.at_level(logging.WARNING):
        await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
            pricing=UNPRICEABLE_RECOVERY,
        )

    ((leg, _),) = trade.submit_calls
    assert leg.order_type is OrderType.MARKET
    assert leg.extended_hours is False
    assert any(getattr(r, "action", None) == "reducing_leg_shape_side_mismatch" for r in caplog.records)


async def test_a_replaced_leg_reports_no_slippage_against_the_price_it_never_used(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """Codex review of #2007: R11 can replace an operator's confirmed limit with a
    regular-session market leg on the other side. That leg was never priced from
    the confirmed quote, so it has no reference to have slipped from."""
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    # The replaced leg is a regular-session market order, which only goes out
    # inside the regular session.
    _walk_clock_to(repo, FIXTURE_RTH_MS)
    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="recovery-flatten-abc",
        entry_order_ref=entry_ref,
        # Confirmed to cover a short; the attributed position is long, so
        # cancellation resolves to a SELL and the confirmed leg cannot be used.
        confirmed_shape=ConfirmedRecoveryShape(
            shape=LegShape(
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=100.10,
                extended_hours=True,
                side=OrderSide.BUY,
            ),
            valid_until_ms=repo.clock() + 3_600_000,
            reference_quote=TopOfBookQuote(
                symbol="SPY", bid=100.00, ask=100.05,
                source="ibkr.market_data.status", observed_at_ms=repo.clock(),
            ),
            quantity=10.0,
        ),
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted"))

    resolved = await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY
    )

    ((leg, _),) = trade.submit_calls
    assert (leg.order_type, leg.side, leg.extended_hours) == (
        OrderType.MARKET, OrderSide.SELL, False,
    )
    assert resolved.reducing_order_ref is not None
    assert priced_reduction_reference_price(repo, resolved.reducing_order_ref) is None


async def test_a_deferred_cancel_still_reduces_with_the_decisions_shape(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The finding: cancel-and-prove is not terminal on the first pass.

    The entry is still working after the cancel request is acknowledged, so
    the deciding runner's pass creates no reducing order at all. The 15 s
    reconciliation sweep re-drives the very same EXIT with no shape argument
    -- it has none to give -- and before the shape was durable with the
    acceptance it built a regular-session market DAY reduction, which Alpaca
    queues to the next 09:30 while the extended-hours exposure stands.
    """
    # A POST-session decision, sendable until the declared close (#2440).
    _walk_clock_to(repo, AFTER_HOURS_MS)
    entry_ref = await _make_entry(repo, quantity=10, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        program_leg=ProgramLeg(_XH_SELL, valid_until_ms=POST_CLOSE_MS),
    )
    assert accepted.effect_operation_id is not None

    deferring = _FakeTrade(
        lookup_results=[_broker_order(entry_ref, status="accepted", filled_quantity=0.0)]
    )
    deferred = await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=deferring, pricing=UNPRICEABLE_RECOVERY
    )
    assert deferring.submit_calls == [], "the entry is still working; nothing to reduce yet"
    assert deferred.reducing_order_ref is None

    # The sweep's re-drive: a fresh call with no shape, exactly as
    # `reconcile.py` makes it. The cancel has landed and took 4 shares with
    # it, so this is the pass that creates the reduction.
    terminal = _broker_order(
        entry_ref, status="canceled", filled_quantity=4.0, filled_avg_price=100.0
    )
    sweeping = _FakeTrade(
        lookup_results=[terminal, terminal],
        submit_result=_broker_order("placeholder", side="sell", status="accepted"),
    )
    swept = await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=sweeping, pricing=UNPRICEABLE_RECOVERY
    )

    assert swept.reducing_order_ref is not None
    ((leg, _client_order_id),) = sweeping.submit_calls
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours, leg.side) == (
        OrderType.LIMIT,
        TimeInForce.DAY,
        99.80,
        True,
        OrderSide.SELL,
    )
    assert leg.quantity == pytest.approx(4.0)


async def test_an_exit_accepted_without_a_decision_still_reduces_market_day(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """Ruling R5 is unchanged: safe flatten, the watchdog and recovery accept
    an EXIT with no deciding program, record no shape, and get the
    regular-session market DAY leg."""
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
    trade = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY)

    ((leg, _client_order_id),) = trade.submit_calls
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.MARKET,
        TimeInForce.DAY,
        None,
        False,
    )
