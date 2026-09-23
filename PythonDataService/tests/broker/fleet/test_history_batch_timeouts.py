"""Both hops of one browser history request must widen (issue #2204, FR-010).

Pins the ordering the PRD requires in two places: the two constants
themselves, and the OUTER coordinator -> Clerk delivery bound declared on the
``bot_chart_history`` ``ProviderOperation``. The INNER Clerk -> coordinator
bound ``RemoteHistoryBatchClient`` builds its transport with is pinned in
``tests/broker/v2panel/test_history_batch_client.py``, not here.
"""

from __future__ import annotations

import httpx
import pytest

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.broker.fleet.delivery import DeliveryRequest, HttpLaneDelivery
from app.broker.fleet.history_batch import (
    HISTORY_BATCH_INNER_TIMEOUT_S,
    HISTORY_BATCH_OUTER_TIMEOUT_S,
)
from app.broker.fleet.internal_http import DEFAULT_INTERNAL_TIMEOUT_S
from app.broker.fleet.provider import ProviderOperation


def test_outer_bound_is_strictly_larger_than_the_inner_bound() -> None:
    """The one assertion FR-010 requires a test to pin."""
    assert HISTORY_BATCH_OUTER_TIMEOUT_S > HISTORY_BATCH_INNER_TIMEOUT_S


def test_both_bounds_exceed_the_fleet_default() -> None:
    """Neither bound is a no-op restatement of the existing 10s default."""
    assert HISTORY_BATCH_INNER_TIMEOUT_S > DEFAULT_INTERNAL_TIMEOUT_S
    assert HISTORY_BATCH_OUTER_TIMEOUT_S > DEFAULT_INTERNAL_TIMEOUT_S


#: Every operation that deliberately widens its forward bound, and why:
#: ``lane_stop_all_bots`` runs one operator Stop per bot (#2268, pinned in
#: ``test_lane_quiesce_operations.py``).
_OTHER_DELIBERATE_WIDENINGS = frozenset({"lane_stop_all_bots"})


def test_bot_chart_history_declares_the_outer_bound() -> None:
    """This operation widens; every other operation stays at the fleet default
    unless it is a named, separately pinned widening."""
    by_id = {op.operation_id: op for op in ALPACA_OPERATIONS}
    assert by_id["bot_chart_history"].read_timeout_s == HISTORY_BATCH_OUTER_TIMEOUT_S

    others_widened = {
        op.operation_id
        for op in ALPACA_OPERATIONS
        if op.operation_id != "bot_chart_history"
        and op.read_timeout_s != DEFAULT_INTERNAL_TIMEOUT_S
    }
    assert others_widened == _OTHER_DELIBERATE_WIDENINGS


_ECHOED_IDENTITY_HEADERS = (
    "x-fleet-broker",
    "x-fleet-clerk-id",
    "x-fleet-routing-epoch",
    "x-fleet-binding-generation",
)


def _echo_identity_response(request: httpx.Request) -> httpx.Response:
    """A 200 that echoes back whatever pinned identity headers it was sent."""
    echoed = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _ECHOED_IDENTITY_HEADERS
    }
    return httpx.Response(200, headers=echoed, json={})


def _probe_delivery(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> HttpLaneDelivery:
    """One ``HttpLaneDelivery`` whose built client's read timeout is captured."""
    real_build_internal_client = httpx.AsyncClient

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        captured["read_timeout_s"] = read_timeout_s
        return real_build_internal_client(
            transport=httpx.MockTransport(_echo_identity_response)
        )

    import app.broker.fleet.delivery as delivery_module

    monkeypatch.setattr(delivery_module, "build_internal_client", _fake_build_internal_client)
    return HttpLaneDelivery(base_url="http://127.0.0.1", coordinator_service_token="tok")


def _probe_operation(*, read_timeout_s: float) -> ProviderOperation:
    return ProviderOperation(
        operation_id="probe_op",
        method="GET",
        path_template="/probe",
        agent_path_template="/probe",
        capability=next(iter(ALPACA_OPERATIONS)).capability,
        readiness=next(iter(ALPACA_OPERATIONS)).readiness,
        requires_effective_account=False,
        idempotency=next(iter(ALPACA_OPERATIONS)).idempotency,
        read_timeout_s=read_timeout_s,
    )


async def test_http_lane_delivery_builds_its_client_with_the_operations_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OUTER hop: ``HttpLaneDelivery`` threads the operation's declared
    ``read_timeout_s`` into the client it builds, not a hardcoded default.

    Read straight off ``request.operation.read_timeout_s`` (issue #2204 gate
    F3) -- ``deliver`` takes no separate ``read_timeout_s`` kwarg to drift
    from it.
    """
    captured: dict[str, object] = {}
    delivery = _probe_delivery(monkeypatch, captured)
    operation = _probe_operation(read_timeout_s=HISTORY_BATCH_OUTER_TIMEOUT_S)
    request = DeliveryRequest(
        broker="alpaca", clerk_id="clrk_x", operation=operation, path_params={}
    )

    await delivery.deliver(request)

    assert captured["read_timeout_s"] == HISTORY_BATCH_OUTER_TIMEOUT_S


async def test_http_lane_delivery_builds_the_default_operations_client_at_10s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operation that never widens still builds its client at the fleet
    default -- pinned explicitly so a regression that silently drops
    ``read_timeout_s`` (falling back to ``build_internal_client``'s own
    default) cannot pass unnoticed."""
    captured: dict[str, object] = {}
    delivery = _probe_delivery(monkeypatch, captured)
    operation = _probe_operation(read_timeout_s=DEFAULT_INTERNAL_TIMEOUT_S)
    request = DeliveryRequest(
        broker="alpaca", clerk_id="clrk_x", operation=operation, path_params={}
    )

    await delivery.deliver(request)

    assert captured["read_timeout_s"] == DEFAULT_INTERNAL_TIMEOUT_S


async def test_lane_router_thread_the_operations_bound_through_to_http_delivery(
    monkeypatch: pytest.MonkeyPatch, fleet_service, control_dir
) -> None:
    """The bound survives the full ``LaneRouter`` -> ``HttpLaneDelivery`` path.

    Not just a unit fact about ``HttpLaneDelivery.deliver`` in isolation:
    ``LaneRouter.deliver_read`` is the real caller, and it no longer passes a
    ``read_timeout_s`` kwarg at all (issue #2204 gate F3) -- this proves the
    operation's own declared bound still reaches the client with that kwarg
    gone.
    """
    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import bind_lane, fake_alpha, provision_lane

    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="timeout-probe", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-timeout-probe")

    captured: dict[str, object] = {}
    delivery = _probe_delivery(monkeypatch, captured)
    router = LaneRouter(service=fleet_service, delivery_for=lambda broker, session: delivery)
    operation = next(
        op for op in fake_alpha().operations() if op.operation_id == "read_account"
    )
    assert operation.read_timeout_s == DEFAULT_INTERNAL_TIMEOUT_S, (
        "this fixture operation must not itself declare a widened bound"
    )

    await router.deliver_read(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation=operation,
        path_params={},
        query={},
    )

    assert captured["read_timeout_s"] == DEFAULT_INTERNAL_TIMEOUT_S
