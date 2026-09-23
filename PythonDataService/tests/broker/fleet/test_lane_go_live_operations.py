"""The catalog declares installation migration's two go-live lane operations (#2269).

``migrate-installation go-live`` reaches every lane through the coordinator's
catalog-routed surface — never a lane's port directly — to prove IB Gateway
returns bars there and then to release the lane's go-live hold.
"""

from __future__ import annotations

from app.broker.fleet.internal_http import (
    DEFAULT_INTERNAL_TIMEOUT_S,
    LANE_IBKR_BAR_CHECK_READ_TIMEOUT_S,
)
from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    validate_operation_catalog,
)
from app.broker.fleet_composition import production_provider_adapters
from app.installation_migration.lanes import BAR_CHECK_CLIENT_TIMEOUT_S
from app.services.lane_go_live import IBKR_BAR_CHECK_TIMEOUT_S


def _operation(operation_id: str):
    for operation in production_provider_adapters()["alpaca"].operations():
        if operation.operation_id == operation_id:
            return operation
    raise AssertionError(f"alpaca declares no {operation_id}")


def test_lane_ibkr_bar_check_is_a_lane_scoped_market_read() -> None:
    operation = _operation("lane_ibkr_bar_check")

    assert operation.method == "GET"
    assert operation.path_template == "/lane/ibkr-bar-check"
    assert operation.agent_path_template == "/api/brokers/alpaca/lane/ibkr-bar-check"
    assert operation.capability is Capability.MARKET_STATUS_READ
    assert operation.idempotency is OperationIdempotency.READ
    # A freshly migrated lane is checkable whether or not its binding is confirmed.
    assert operation.readiness is OperationReadiness.CONFIGURATION_ACCESS
    assert operation.requires_effective_account is False


def test_lane_go_live_release_is_a_lane_scoped_one_shot_bot_action() -> None:
    operation = _operation("lane_go_live_release")

    assert operation.method == "POST"
    assert operation.path_template == "/lane/go-live/release"
    assert operation.agent_path_template == "/api/brokers/alpaca/lane/go-live/release"
    assert operation.capability is Capability.BOT_ACTION
    assert operation.idempotency is OperationIdempotency.ONE_SHOT
    assert operation.readiness is OperationReadiness.CONFIGURATION_ACCESS
    assert operation.requires_effective_account is False


def test_the_catalog_with_both_operations_still_validates() -> None:
    validate_operation_catalog(production_provider_adapters()["alpaca"].operations())


def test_the_bar_check_forward_outlasts_the_lanes_own_bound_and_answers_inside_the_cli() -> None:
    """The lane bounds its IB Gateway work itself; the coordinator must wait
    longer than that, and the CLI longer than the coordinator, so the lane's
    own answer — a pass or its named failure — always reaches the operator."""
    operation = _operation("lane_ibkr_bar_check")

    assert operation.read_timeout_s == LANE_IBKR_BAR_CHECK_READ_TIMEOUT_S
    assert IBKR_BAR_CHECK_TIMEOUT_S < operation.read_timeout_s < BAR_CHECK_CLIENT_TIMEOUT_S
    assert operation.read_timeout_s > DEFAULT_INTERNAL_TIMEOUT_S
