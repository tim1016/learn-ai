"""The catalog declares installation migration's two lane operations (#2268).

``migrate-installation export`` reaches every lane through the coordinator's
catalog-routed surface — never a lane's port directly — so the lane-wide
stop and the non-draining account-quiet read are provider operations like any
other, forwarded with the per-clerk coordinator token.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    validate_operation_catalog,
)
from app.broker.fleet.records import AssignmentState, StoredLifecycleState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from tests.broker.fleet.conftest import FrozenClock, bind_lane, provision_lane


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


def test_both_operations_route_to_a_serving_lane_without_draining_it(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Routing resolves a bound, provisioned lane and leaves it exactly so:
    still ``provisioned``, its assignment still ``effective``."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        lane = provision_lane(
            service, broker="alpaca", label="serving", tmp_path=control_dir.parent
        )
        _session, confirmed = bind_lane(service, lane, account="PA3MIGRATE1")
        for operation_id in ("lane_stop_all_bots", "lane_account_quiet_read"):
            _clerk, session, _assignment = service.resolve_route(
                broker="alpaca",
                clerk_id=lane.clerk_id,
                readiness=_operation(operation_id).readiness,
            )
            assert session.clerk_id == lane.clerk_id
        stored = service._store.read_clerk(lane.clerk_id)
        assert stored is not None
        assert stored.lifecycle_state is StoredLifecycleState.PROVISIONED
        [assignment] = service._store.list_active_assignments()
        assert assignment.state is AssignmentState.EFFECTIVE
        assert assignment.assignment_generation == confirmed.assignment_generation
    finally:
        service.close()


def test_lane_stop_all_bots_forward_outlasts_a_stop_and_answers_inside_the_cli_budget() -> None:
    """Each Stop can take more than 5 s (the Clerk STOP, the cancellation wait,
    then a custody proof), so the fleet's 10 s default would cut a multi-bot
    stop off mid-flight; the coordinator must still answer before the
    migration CLI stops waiting for it."""
    from app.broker.fleet.internal_http import (
        DEFAULT_INTERNAL_TIMEOUT_S,
        LANE_STOP_ALL_READ_TIMEOUT_S,
    )
    from app.installation_migration.lanes import STOP_ALL_CLIENT_TIMEOUT_S

    operation = _operation("lane_stop_all_bots")

    assert operation.read_timeout_s == LANE_STOP_ALL_READ_TIMEOUT_S
    assert operation.read_timeout_s > DEFAULT_INTERNAL_TIMEOUT_S
    assert operation.read_timeout_s < STOP_ALL_CLIENT_TIMEOUT_S
