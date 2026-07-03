"""Pure tests for the IBKR Account Truth projection."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr import account_truth as account_truth_module
from app.broker.ibkr.account_truth import compose_account_truth, fetch_account_truth
from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.models import (
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderEvent,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountInstanceBinding,
    write_account_instance_binding,
)
from app.schemas.account_truth import AccountTruthEvidenceGap


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


def _binding(
    strategy_instance_id: str = "bot-a",
    *,
    account_id: str = "DU1234567",
    bot_order_namespace: str | None = None,
    lifecycle_state: Literal["DEPLOYED", "ACTIVE", "RETIRED"] = "ACTIVE",
    recorded_at_ms: int = 1_780_000_000_000,
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        run_id=f"run-{strategy_instance_id}",
        bot_order_namespace=bot_order_namespace
        or f"learn-ai/{strategy_instance_id}/v1",
        lifecycle_state=lifecycle_state,
        recorded_at_ms=recorded_at_ms,
        source="test",
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
        account_instance_bindings=[_binding()],
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
    assert truth.executions[0].uncertainty_codes == []
    assert {row.key: row.status for row in truth.invariants}[
        "positions_match_known_ownership"
    ] == "pass"


def test_account_truth_defaults_unstamped_open_order_to_foreign_and_blocks() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
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
    assert truth.orders[0].cancel_action.enabled is False
    assert truth.orders[0].cancel_action.reason_code == "FOREIGN_OR_UNCLAIMED"
    assert truth.blockers[0].code == "unknown_open_orders"


@pytest.mark.parametrize("lifecycle_state", ["DEPLOYED", "ACTIVE"])
def test_account_truth_active_known_live_order_stays_clean(
    lifecycle_state: Literal["DEPLOYED", "ACTIVE"],
) -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding(lifecycle_state=lifecycle_state),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "clean"
    assert truth.orders[0].owner.owner_class == "bot"
    assert truth.orders[0].owner.owner_binding_state == lifecycle_state
    assert truth.orders[0].owner.severity == "ok"
    assert truth.orders[0].cancel_action.visible is True
    assert truth.orders[0].cancel_action.enabled is True
    assert truth.orders[0].cancel_action.reason_code is None
    assert truth.orders[0].cancel_action.label == "Cancel"
    assert truth.blockers == []
    assert {row.key: row.status for row in truth.invariants}[
        "open_orders_known"
    ] == "pass"


def test_account_truth_authors_order_cancel_action_reasons() -> None:
    non_paper = compose_account_truth(
        health=_health().model_copy(update={"mode": "live", "is_paper": False}),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )
    assert non_paper.orders[0].cancel_action.visible is True
    assert non_paper.orders[0].cancel_action.enabled is False
    assert non_paper.orders[0].cancel_action.reason_code == "BROKER_NOT_PAPER_CONNECTED"

    frozen = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
        account_freeze_active=True,
    )
    assert frozen.orders[0].cancel_action.visible is True
    assert frozen.orders[0].cancel_action.enabled is False
    assert frozen.orders[0].cancel_action.reason_code == "ACCOUNT_FROZEN"

    terminal = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order(status="Filled", remaining=0.0)],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )
    assert terminal.orders[0].cancel_action.visible is True
    assert terminal.orders[0].cancel_action.enabled is False
    assert terminal.orders[0].cancel_action.reason_code == "ORDER_TERMINAL"

    completed = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[_open_order(status="Cancelled", remaining=0.0)],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )
    assert completed.orders[0].cancel_action.visible is False
    assert completed.orders[0].cancel_action.enabled is False
    assert completed.orders[0].cancel_action.reason_code == "NOT_OPEN_ORDER"


def test_account_truth_authors_execution_uncertainty_codes() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(
                order_ref=None,
                exec_time_ms=None,
                fee=None,
                fill_quantity=None,
                avg_fill_price=None,
                last_fill_price=None,
            )
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.executions[0].uncertainty_codes == [
        "missing_order_ref",
        "observed_time_only",
        "commission_pending",
        "missing_quantity",
        "missing_price",
    ]


def test_account_truth_execution_uncertainty_preserves_zero_price() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(
                avg_fill_price=450.0,
                last_fill_price=0.0,
            )
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.executions[0].price == 0.0
    assert "missing_price" not in truth.executions[0].uncertainty_codes


def test_account_truth_never_registered_namespace_stays_foreign() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[
            _open_order(order_ref="learn-ai/never-registered/v1:intent-unknown"),
        ],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.orders[0].owner.owner_class == "foreign_or_unclaimed"
    assert truth.orders[0].owner.owner_binding_state == "UNKNOWN"
    assert truth.orders[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"unknown_open_orders"}


def test_account_truth_registry_gap_preserves_bot_stamped_live_order() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        evidence_gaps=[
            AccountTruthEvidenceGap(
                source="instance_registry",
                severity="critical",
                message="Account instance registry unavailable: corrupt registry line",
            )
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.orders[0].owner.owner_class == "bot"
    assert truth.orders[0].owner.owner_key == "bot-a"
    assert truth.orders[0].owner.owner_binding_state == "UNKNOWN"
    assert truth.orders[0].owner.severity == "critical"
    assert "evidence_gap_instance_registry" in {row.code for row in truth.blockers}
    assert "unknown_open_orders" not in {row.code for row in truth.blockers}
    assert {row.key: row.status for row in truth.invariants}[
        "open_orders_known"
    ] == "pass"


def test_account_truth_mixed_active_and_retired_position_is_not_retired_live_exposure() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding("bot-a", lifecycle_state="ACTIVE"),
            _binding("bot-b", lifecycle_state="RETIRED"),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(_position()),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(order_ref="learn-ai/bot-a/v1:intent-a", exec_id="exec-a"),
            _execution(
                order_id=43,
                perm_id=9002,
                order_ref="learn-ai/bot-b/v1:intent-b",
                exec_id="exec-b",
            ),
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "clean"
    assert truth.positions[0].owner.owner_class == "mixed_known"
    assert truth.positions[0].owner.owner_binding_state == "ACTIVE"
    assert truth.positions[0].owner.severity == "warning"
    assert "retired_owner_live_exposure" not in {row.code for row in truth.blockers}
    assert {row.key: row.status for row in truth.invariants}[
        "positions_match_known_ownership"
    ] == "pass"


def test_account_truth_all_retired_mixed_position_is_retired_live_exposure() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding("bot-a", lifecycle_state="RETIRED"),
            _binding("bot-b", lifecycle_state="RETIRED"),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(_position()),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(order_ref="learn-ai/bot-a/v1:intent-a", exec_id="exec-a"),
            _execution(
                order_id=43,
                perm_id=9002,
                order_ref="learn-ai/bot-b/v1:intent-b",
                exec_id="exec-b",
            ),
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.positions[0].owner.owner_class == "mixed_known"
    assert truth.positions[0].owner.owner_binding_state == "RETIRED"
    assert truth.positions[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"retired_owner_live_exposure"}


def test_account_truth_duplicate_active_namespace_fails_closed() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding("bot-a", bot_order_namespace="learn-ai/shared/v1"),
            _binding("bot-b", bot_order_namespace="learn-ai/shared/v1"),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order(order_ref="learn-ai/shared/v1:intent-shared")],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.orders[0].owner.owner_class == "mixed_known"
    assert truth.orders[0].owner.owner_key == "duplicate_active_namespace"
    assert truth.orders[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"duplicate_active_namespace"}


def test_account_truth_derives_binding_account_scope_from_order_facts() -> None:
    truth = compose_account_truth(
        health=_health().model_copy(update={"account_id": None}),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=None,
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "clean"
    assert truth.account_id == "DU1234567"
    assert truth.orders[0].owner.owner_class == "bot"
    assert truth.orders[0].owner.owner_binding_state == "ACTIVE"


def test_account_truth_conflicting_account_scope_does_not_wildcard_bindings() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding(account_id="DU7654321")],
        account=None,
        positions_snapshot=None,
        open_orders=[_open_order(account_id="DU7654321")],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.account_id is None
    assert truth.orders[0].owner.owner_class == "foreign_or_unclaimed"
    assert {gap.source for gap in truth.evidence_gaps} == {"account_scope"}
    assert {row.code for row in truth.blockers} == {
        "unknown_open_orders",
        "evidence_gap_account_scope",
    }


def test_account_truth_retired_terminal_evidence_stays_attributed_and_clean() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding(lifecycle_state="ACTIVE", recorded_at_ms=1_780_000_000_000),
            _binding(lifecycle_state="RETIRED", recorded_at_ms=1_780_000_000_500),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[
            _open_order(status="Filled", cumulative_filled=1.0, remaining=0.0),
        ],
        executions=[_execution()],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "clean"
    assert truth.known_bot_namespaces == ["learn-ai/bot-a/v1"]
    assert truth.orders[0].owner.owner_class == "bot"
    assert truth.orders[0].owner.owner_binding_state == "RETIRED"
    assert truth.orders[0].owner.severity == "ok"
    assert truth.executions[0].owner.owner_binding_state == "RETIRED"
    assert truth.owner_summaries[0].owner_binding_state == "RETIRED"
    assert {row.key: row.status for row in truth.invariants}[
        "completed_orders_known"
    ] == "pass"
    assert {row.key: row.status for row in truth.invariants}[
        "all_executions_assigned"
    ] == "pass"


def test_account_truth_retired_live_order_is_distinct_critical_anomaly() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding(lifecycle_state="RETIRED", recorded_at_ms=1_780_000_000_500),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order()],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.orders[0].owner.owner_class == "bot"
    assert truth.orders[0].owner.owner_binding_state == "RETIRED"
    assert truth.orders[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"retired_owner_live_exposure"}
    assert {row.key: row.status for row in truth.invariants}[
        "open_orders_known"
    ] == "fail"


def test_account_truth_retired_current_position_is_distinct_critical_anomaly() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[
            _binding(lifecycle_state="RETIRED", recorded_at_ms=1_780_000_000_500),
        ],
        account=None,
        positions_snapshot=_positions_snapshot(_position()),
        open_orders=[],
        completed_orders=[],
        executions=[_execution()],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.positions[0].owner.owner_class == "bot"
    assert truth.positions[0].owner.owner_binding_state == "RETIRED"
    assert truth.positions[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"retired_owner_live_exposure"}
    assert {row.key: row.status for row in truth.invariants}[
        "positions_match_known_ownership"
    ] == "fail"


def test_account_truth_keeps_app_minted_manual_distinct_from_bot() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
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


def test_account_truth_terminal_cancel_and_inactive_lifecycles_are_not_filled() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[
            _open_order(order_id=1, perm_id=9001, status="Cancelled", remaining=0.0),
            _open_order(order_id=2, perm_id=9002, status="Inactive", remaining=0.0),
        ],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert [row.lifecycle for row in truth.orders] == ["cancelled", "rejected"]


def test_account_truth_dedupes_exec_id_and_warns_on_missing_commission() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
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
    assert truth.executions[0].uncertainty_codes == ["commission_pending"]
    assert {row.code for row in truth.caveats} == {
        "missing_commission",
        "duplicate_exec_id_suppressed",
    }
    assert {row.key: row.status for row in truth.invariants}[
        "commission_complete"
    ] == "warn"


def test_account_truth_duplicate_exec_backfills_later_commission() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[
            _execution(exec_id="dup-1", fee=None),
            _execution(exec_id="dup-1", fee=1.25, ts_ms=1_780_000_000_350),
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert len(truth.executions) == 1
    assert truth.executions[0].fee == 1.25
    assert truth.executions[0].uncertainty_codes == []
    assert {row.code for row in truth.caveats} == {"duplicate_exec_id_suppressed"}
    assert {row.key: row.status for row in truth.invariants}[
        "commission_complete"
    ] == "pass"


def test_account_truth_open_and_completed_evidence_counts_are_separate() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[_open_order(order_id=1, perm_id=9001)],
        completed_orders=[
            _open_order(
                order_id=2,
                perm_id=9002,
                status="Filled",
                cumulative_filled=1.0,
                remaining=0.0,
            )
        ],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    counts = {row.key: row.evidence_count for row in truth.invariants}
    assert counts["open_orders_known"] == 1
    assert counts["completed_orders_known"] == 1


def test_account_truth_unfilled_known_order_does_not_explain_position() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(_position()),
        open_orders=[_open_order(cumulative_filled=0.0, remaining=1.0)],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert truth.positions[0].owner.owner_class == "foreign_or_unclaimed"
    assert {row.code for row in truth.blockers} == {"unknown_positions"}


def test_account_truth_fallback_lifecycle_id_includes_account_and_client() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[
            _open_order(order_id=42, perm_id=None, client_id=7),
            _open_order(order_id=42, perm_id=None, client_id=8),
        ],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert [row.lifecycle_id for row in truth.orders] == [
        "account:DU1234567:client:7:order:42",
        "account:DU1234567:client:8:order:42",
    ]


def test_account_truth_recovering_connection_fails_liveness() -> None:
    truth = compose_account_truth(
        health=_health().model_copy(update={"connection_state": "recovering"}),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert {row.key: row.status for row in truth.invariants}[
        "broker_liveness_proven"
    ] == "fail"


def test_account_truth_critical_account_summary_gap_forces_not_proven() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[],
        evidence_gaps=[
            AccountTruthEvidenceGap(
                source="account_summary",
                severity="critical",
                message="IBKR account summary unavailable: timeout",
            )
        ],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert truth.status_label == "Not proven"
    assert {row.code for row in truth.blockers} == {"evidence_gap_account_summary"}


def test_account_truth_unclaimed_position_blocks_bot_submits() -> None:
    truth = compose_account_truth(
        health=_health(),
        account_instance_bindings=[_binding()],
        account=None,
        positions_snapshot=_positions_snapshot(
            _position(con_id=54321, symbol="QQQ", quantity=2.0)
        ),
        open_orders=[],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert truth.positions[0].owner.owner_class == "foreign_or_unclaimed"
    assert truth.positions[0].owner.severity == "critical"
    assert {row.code for row in truth.blockers} == {"unknown_positions"}
    assert {row.key: row.status for row in truth.invariants}[
        "positions_match_known_ownership"
    ] == "fail"


@pytest.mark.asyncio
async def test_fetch_account_truth_collects_account_summary_gap_into_final_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    monkeypatch.setattr(
        account_truth_module.ibkr_account,
        "fetch_account_summary",
        AsyncMock(side_effect=BrokerError("summary timeout")),
    )
    monkeypatch.setattr(
        account_truth_module.ibkr_account,
        "fetch_positions",
        AsyncMock(return_value=_positions_snapshot()),
    )
    monkeypatch.setattr(
        account_truth_module,
        "list_open_orders",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        account_truth_module,
        "list_completed_orders",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        account_truth_module,
        "executions_for_reconnect_recovery",
        AsyncMock(return_value=[]),
    )

    truth = await fetch_account_truth(
        client,  # type: ignore[arg-type]
        health=_health(),
        account_instance_bindings=[_binding()],
    )

    assert truth.final_verdict == "not_proven"
    assert truth.final_severity == "critical"
    assert truth.evidence_gaps[0].source == "account_summary"
    assert truth.blockers[0].code == "evidence_gap_account_summary"


def test_account_truth_router_reads_durable_instance_registry(
    tmp_path: Path,
) -> None:
    from app.broker.ibkr.account_truth import load_account_instance_registry_evidence

    binding = _binding()
    write_account_instance_binding(tmp_path, binding)
    evidence = load_account_instance_registry_evidence(
        artifacts_root=tmp_path,
        account_id="DU1234567",
        context="account truth test",
    )

    assert evidence.bindings == [binding]
    assert evidence.evidence_gaps == []


def test_account_truth_router_surfaces_registry_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.ibkr import account_truth

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise AccountArtifactError("corrupt registry line")

    monkeypatch.setattr(account_truth, "read_account_instance_registry", fail_read)

    evidence = account_truth.load_account_instance_registry_evidence(
        artifacts_root=tmp_path,
        account_id="DU1234567",
        context="account truth test",
    )

    assert evidence.bindings == []
    assert len(evidence.evidence_gaps) == 1
    gap = evidence.evidence_gaps[0]
    assert gap.source == "instance_registry"
    assert gap.severity == "critical"
    assert "corrupt registry line" in gap.message


def test_account_truth_router_surfaces_missing_account_id_as_registry_gap() -> None:
    from app.broker.ibkr.account_truth import load_account_instance_registry_evidence

    evidence = load_account_instance_registry_evidence(
        artifacts_root=Path("/unused"),
        account_id=None,
        context="account truth test",
    )

    assert evidence.bindings == []
    assert len(evidence.evidence_gaps) == 1
    gap = evidence.evidence_gaps[0]
    assert gap.source == "instance_registry"
    assert gap.severity == "critical"
    assert "broker account id is unknown" in gap.message
