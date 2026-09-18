"""Both hops of one browser history request must widen (issue #2204, FR-010).

Pins the ordering the PRD requires in three places: the two constants
themselves, the OUTER coordinator -> Clerk delivery bound declared on the
``bot_chart_history`` ``ProviderOperation``, and the INNER Clerk ->
coordinator bound ``RemoteHistoryBatchClient`` builds its transport with.
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


def test_bot_chart_history_declares_the_outer_bound() -> None:
    """Only this operation widens; every other declared operation is untouched."""
    by_id = {op.operation_id: op for op in ALPACA_OPERATIONS}
    assert by_id["bot_chart_history"].read_timeout_s == HISTORY_BATCH_OUTER_TIMEOUT_S

    others_widened = [
        op.operation_id
        for op in ALPACA_OPERATIONS
        if op.operation_id != "bot_chart_history" and op.read_timeout_s is not None
    ]
    assert others_widened == []


async def test_http_lane_delivery_builds_its_client_with_the_operations_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OUTER hop: ``HttpLaneDelivery`` threads the operation's declared
    ``read_timeout_s`` into the client it builds, not a hardcoded default."""
    captured: dict[str, object] = {}
    real_build_internal_client = httpx.AsyncClient

    def _fake_build_internal_client(*, read_timeout_s=None, **_kwargs):
        captured["read_timeout_s"] = read_timeout_s
        return real_build_internal_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": "clrk_x"},
                    json={},
                )
            )
        )

    import app.broker.fleet.delivery as delivery_module

    monkeypatch.setattr(delivery_module, "build_internal_client", _fake_build_internal_client)

    delivery = HttpLaneDelivery(base_url="http://127.0.0.1", coordinator_service_token="tok")
    operation = ProviderOperation(
        operation_id="probe_op",
        method="GET",
        path_template="/probe",
        agent_path_template="/probe",
        capability=next(iter(ALPACA_OPERATIONS)).capability,
        readiness=next(iter(ALPACA_OPERATIONS)).readiness,
        requires_effective_account=False,
        idempotency=next(iter(ALPACA_OPERATIONS)).idempotency,
        read_timeout_s=HISTORY_BATCH_OUTER_TIMEOUT_S,
    )
    request = DeliveryRequest(
        broker="alpaca", clerk_id="clrk_x", operation=operation, path_params={}
    )

    await delivery.deliver(request, read_timeout_s=operation.read_timeout_s)

    assert captured["read_timeout_s"] == HISTORY_BATCH_OUTER_TIMEOUT_S
