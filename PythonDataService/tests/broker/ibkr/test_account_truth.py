"""Pure tests for the IBKR Account Truth projection."""

from __future__ import annotations

from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderEvent,
    IbkrPosition,
    IbkrPositionsSnapshot,
)


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _open_order(**overrides) -> IbkrOpenOrder:
    base = {
        "account_id": "DU1234567",
        "order_id": 42,
        "perm_id": 9001,
        "client_id": 7,
        "con_id": 12345,
        "symbol": "SPY",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 1.0,
        "order_type": "MKT",
        "limit_price": None,
        "time_in_force": "DAY",
        "status": "Submitted",
        "cumulative_filled": 0.0,
        "remaining": 1.0,
        "avg_fill_price": None,
        "order_ref": "learn-ai/bot-a/v1:intent-a",
        "fetched_at_ms": 1_780_000_000_100,
    }
    base.update(overrides)
    return IbkrOpenOrder(**base)


def _execution(**overrides) -> IbkrOrderEvent:
    base = {
        "account_id": "DU1234567",
        "order_id": 42,
        "perm_id": 9001,
        "con_id": 12345,
        "event_type": "fill",
        "status": "Filled",
        "order_ref": "learn-ai/bot-a/v1:intent-a",
        "symbol": "SPY",
        "side": "BUY",
        "order_type": "MKT",
        "exec_id": "exec-1",
        "client_id": 7,
        "fill_quantity": 1.0,
        "avg_fill_price": 450.0,
        "cumulative_filled": 1.0,
        "remaining": 0.0,
        "last_fill_price": 450.0,
        "exec_time_ms": 1_780_000_000_200,
        "fee": 1.0,
        "ts_ms": 1_780_000_000_300,
    }
    base.update(overrides)
    return IbkrOrderEvent(**base)


def _positions_snapshot(*positions: IbkrPosition) -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id="DU1234567",
        is_paper=True,
        positions=list(positions),
        fetched_at_ms=1_780_000_000_400,
    )


def _position(**overrides) -> IbkrPosition:
    base = {
        "account_id": "DU1234567",
        "con_id": 12345,
        "symbol": "SPY",
        "sec_type": "STK",
        "quantity": 1.0,
        "avg_cost": 450.0,
        "fetched_at_ms": 1_780_000_000_400,
    }
    base.update(overrides)
    return IbkrPosition(**base)


def test_account_truth_passes_when_bot_execution_explains_position() -> None:
    truth = compose_account_truth(
        health=_health(),
        known_strategy_instance_ids=["bot-a"],
        account=None,
        positions_snapshot=_positions_snapshot(_position()),
        open_orders=[],
        completed_orders=[],
        executions=[_execution()],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "clean"
    assert truth.positions[0].owner.owner_class == "bot"
    assert truth.positions[0].owner.owner_key == "bot-a"
    assert {row.key: row.status for row in truth.invariants}[
        "positions_match_known_ownership"
    ] == "pass"


def test_account_truth_defaults_unstamped_open_order_to_foreign_and_blocks() -> None:
    truth = compose_account_truth(
        health=_health(),
        known_strategy_instance_ids=["bot-a"],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order(order_ref=None, client_id=0)],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert truth.orders[0].owner.owner_class == "foreign_or_unclaimed"
    assert truth.orders[0].owner.severity == "critical"
    assert truth.blockers[0].code == "unknown_open_orders"


def test_account_truth_keeps_app_minted_manual_distinct_from_bot() -> None:
    truth = compose_account_truth(
        health=_health(),
        known_strategy_instance_ids=["bot-a"],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[
            _open_order(
                order_ref="manual/operator/v1:BBBBBBBBBBBBBBBBBBBBBB",
                client_id=7,
            )
        ],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.orders[0].owner.owner_class == "manual"
    assert truth.orders[0].owner.evidence_tier == "app_minted_manual"
    assert truth.manual_namespaces_observed == ["manual/operator/v1"]


def test_account_truth_dedupes_exec_id_and_warns_on_missing_commission() -> None:
    truth = compose_account_truth(
        health=_health(),
        known_strategy_instance_ids=["bot-a"],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(exec_id="dup-1", fee=None),
            _execution(exec_id="dup-1", fee=None, ts_ms=1_780_000_000_350),
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert len(truth.executions) == 1
    assert {row.code for row in truth.caveats} == {
        "missing_commission",
        "duplicate_exec_id_suppressed",
    }
    assert {row.key: row.status for row in truth.invariants}[
        "commission_complete"
    ] == "warn"
