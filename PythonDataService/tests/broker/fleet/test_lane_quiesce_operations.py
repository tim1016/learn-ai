"""The catalog declares installation migration's two lane operations (#2268).

``migrate-installation export`` reaches every lane through the coordinator's
catalog-routed surface — never a lane's port directly — so the lane-wide
stop and the non-draining account-quiet read are provider operations like any
other, forwarded with the per-clerk coordinator token.
"""

from __future__ import annotations

from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    validate_operation_catalog,
)
from app.broker.fleet_composition import production_provider_adapters


def _operation(operation_id: str):
    for operation in production_provider_adapters()["alpaca"].operations():
        if operation.operation_id == operation_id:
            return operation
    raise AssertionError(f"alpaca declares no {operation_id}")


def test_lane_stop_all_bots_is_a_lane_scoped_one_shot_bot_action() -> None:
    operation = _operation("lane_stop_all_bots")

    assert operation.method == "POST"
    assert operation.path_template == "/lane/stop-all-bots"
    assert operation.agent_path_template == "/api/brokers/alpaca/lane/stop-all-bots"
    assert operation.capability is Capability.BOT_ACTION
    assert operation.idempotency is OperationIdempotency.ONE_SHOT
    # A lane must be stoppable whether or not its binding is confirmed.
    assert operation.readiness is OperationReadiness.CONFIGURATION_ACCESS
    assert operation.requires_effective_account is False


def test_lane_account_quiet_read_is_a_lane_scoped_custody_read() -> None:
    operation = _operation("lane_account_quiet_read")

    assert operation.method == "GET"
    assert operation.path_template == "/lane/account-quiet"
    assert operation.agent_path_template == "/api/brokers/alpaca/lane/account-quiet"
    assert operation.capability is Capability.CUSTODY_READ
    assert operation.idempotency is OperationIdempotency.READ
    assert operation.readiness is OperationReadiness.CONFIGURATION_ACCESS
    assert operation.requires_effective_account is False


def test_the_catalog_with_both_operations_still_validates() -> None:
    validate_operation_catalog(production_provider_adapters()["alpaca"].operations())
