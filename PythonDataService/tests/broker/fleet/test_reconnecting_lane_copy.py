"""The owner's copy for a lane with no account authority is the truth (#2582).

2026-09-29: the Live lane's startup could not reach Alpaca for a few seconds,
and every routed account read answered "failed serving account_money_read
with 503. Retry once the lane recovers from the reported server error; the
lane's own log names the cause." Retrying could not help -- nothing retried
the selection -- and the log did not name the cause.

The lane's beat reports its account authority on every pass, and the
coordinator authors a routed 5xx's copy from the session it routed through:
reconnecting recovers on its own; a bound lane whose authority is
unavailable needs a restart and never promises a retry. One channel carries
the fact -- the beat -- and nothing rides the identity echo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.fleet_boot import FleetLaneBoot, heartbeat_facts, report_authority_state
from app.broker.fleet.delivery import DeliveryRequest, DeliveryResult, LocalLaneDelivery, StreamDeliveryResult
from app.broker.fleet.errors import ClerkUnreachable
from app.broker.fleet.presence import SessionInfo
from app.broker.fleet.routing import LaneRouter
from app.broker.fleet.service import FleetControlService
from tests.broker.fleet.conftest import FrozenClock, bind_lane, fake_alpha, fake_beta, provision_lane
from tests.broker.fleet.test_provider_conformance import _alpha_operation


async def _lane_answering_503(request: DeliveryRequest) -> DeliveryResult:
    """A lane handler that refuses every read with 503, echoing its identity as the middleware does."""
    headers = {name.lower(): value for name, value in request.pinned_headers().items()}
    return DeliveryResult(status_code=503, headers=headers, body=b'{"detail": "no authority"}')


def _router_over_a_lane_that_beat(
    control_dir: Path,
    clock: FrozenClock,
    fleet_service: FleetControlService,
    *,
    label: str,
    beat: dict[str, object] | None,
    handler: object = _lane_answering_503,
) -> tuple[LaneRouter, str]:
    """One bound lane whose last beat reported ``beat``, behind a real router and delivery."""
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": fake_alpha(), "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(service, broker="fake_alpha", label=label, tmp_path=control_dir.parent)
    session, _confirmed = bind_lane(service, lane, account=f"acct-{label}")
    if beat is not None:
        service.observe_session(
            clerk_id=lane.clerk_id, agent_instance_id=session.agent_instance_id, **beat
        )
    router = LaneRouter(service=service, delivery_for=lambda _broker, _session: LocalLaneDelivery(handler))
    return router, lane.clerk_id


def _beat(authority_state: str, *, reported_state: str = "binding_confirmed") -> dict[str, object]:
    return {
        "reported_binding_generation": 1,
        "reported_state": reported_state,
        "reported_summary": {"endpoint_mode": "live", "authority_state": authority_state},
    }


async def _routed_read_refusal(router: LaneRouter, clerk_id: str) -> ClerkUnreachable:
    with pytest.raises(ClerkUnreachable) as refused:
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=clerk_id,
            operation=_alpha_operation("read_account"),
            path_params={},
            query={},
        )
    return refused.value


async def test_a_reconnecting_lane_s_503_says_it_will_recover_on_its_own(
    control_dir: Path, clock: FrozenClock, fleet_service: FleetControlService
) -> None:
    router, clerk_id = _router_over_a_lane_that_beat(
        control_dir, clock, fleet_service, label="reconnecting", beat=_beat("reconnecting")
    )

    refusal = await _routed_read_refusal(router, clerk_id)

    assert refusal.status_code == 503
    assert "could not reach its broker when it started and is reconnecting" in refusal.message
    assert refusal.next_step == "It will recover on its own once its broker answers; no restart is needed."


async def test_a_bound_lane_whose_authority_is_unavailable_names_the_fix_and_promises_no_retry(
    control_dir: Path, clock: FrozenClock, fleet_service: FleetControlService
) -> None:
    router, clerk_id = _router_over_a_lane_that_beat(
        control_dir, clock, fleet_service, label="unavailable", beat=_beat("unavailable")
    )

    refusal = await _routed_read_refusal(router, clerk_id)

    assert "will not retry on its own" in refusal.message
    assert refusal.next_step == "The Clerk's log names the cause; restart the Clerk once that is fixed."
    assert "Retry" not in refusal.next_step


async def test_a_reconnecting_lane_s_stream_open_tells_the_same_truth(
    control_dir: Path, clock: FrozenClock, fleet_service: FleetControlService
) -> None:
    async def _no_events():
        return
        yield

    async def _stream_answering_503(_request: DeliveryRequest) -> StreamDeliveryResult:
        return StreamDeliveryResult(
            status_code=503, headers={}, events=_no_events(), error_body=b'{"detail": "no authority"}'
        )

    router, clerk_id = _router_over_a_lane_that_beat(
        control_dir, clock, fleet_service, label="streaming", beat=_beat("reconnecting"),
        handler=_stream_answering_503,
    )

    with pytest.raises(ClerkUnreachable) as refused:
        await router.stream_read(
            broker="fake_alpha", clerk_id=clerk_id, operation=_alpha_operation("read_account"),
            path_params={}, query={},
        )

    assert "cannot open read_account yet" in refused.value.message
    assert "no restart is needed" in str(refused.value.next_step)


@pytest.mark.parametrize(
    ("label", "beat"),
    [
        ("serving", _beat("real_live")),
        ("unbound", _beat("unavailable", reported_state="binding_pending")),
        ("silent", None),
    ],
)
async def test_any_other_lane_503_keeps_the_generic_copy(
    control_dir: Path,
    clock: FrozenClock,
    fleet_service: FleetControlService,
    label: str,
    beat: dict[str, object] | None,
) -> None:
    """A serving authority's own 5xx, an unbound lane, a lane that never said: nothing to claim."""
    router, clerk_id = _router_over_a_lane_that_beat(control_dir, clock, fleet_service, label=label, beat=beat)

    refusal = await _routed_read_refusal(router, clerk_id)

    assert "failed serving read_account with 503." in refusal.message
    assert "reconnecting" not in refusal.message
    assert "will not retry" not in refusal.message


def _lane_boot(tmp_path: Path) -> FleetLaneBoot:
    """A registered lane; its presence client is never called by what these tests read."""
    return FleetLaneBoot(
        presence=None,  # type: ignore[arg-type]
        clerk_id="clrk_live",
        worker_key="alpaca-live-clerk",
        volume_root=tmp_path,
        registry_id="registry",
        volume_id="volume",
        session=SessionInfo(agent_instance_id="agent-1", routing_epoch=3),
    )


def test_the_beat_reports_reconnecting_then_what_the_reconnect_ended_in(tmp_path: Path) -> None:
    """The directory tells the same truth: reconnecting while it retries, then what installed."""
    boot = _lane_boot(tmp_path)
    boot.reported_facts = heartbeat_facts(
        account_pin="9LIVE0001",
        effective_binding_generation=4,
        authority_kind="unavailable",
        endpoint_mode="live",
        reconnecting=True,
    )
    assert boot.reported_facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "reconnecting",
    }

    # A retired attempt goes back to reconnecting; the beat says so, not "unavailable".
    report_authority_state(boot, authority_kind="unavailable", reconnecting=True)
    assert boot.reported_facts["reported_summary"]["authority_state"] == "reconnecting"

    report_authority_state(boot, authority_kind="sqlite")

    assert boot.reported_facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "real_live",
    }
    # The grant it confirmed at boot is what it still reports; nothing is re-confirmed.
    assert boot.reported_facts["reported_state"] == "binding_confirmed"
    assert boot.reported_facts["reported_binding_generation"] == 4


def test_a_reconnect_that_ends_final_is_reported_unavailable(tmp_path: Path) -> None:
    boot = _lane_boot(tmp_path)
    boot.reported_facts = heartbeat_facts(
        account_pin="9LIVE0001",
        effective_binding_generation=4,
        authority_kind="unavailable",
        endpoint_mode="live",
        reconnecting=True,
    )

    report_authority_state(boot, authority_kind="unavailable", reconnecting=False)

    assert boot.reported_facts["reported_summary"]["authority_state"] == "unavailable"
