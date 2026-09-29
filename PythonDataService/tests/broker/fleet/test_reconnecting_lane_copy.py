"""The owner's copy for a lane with no account authority is the truth (#2582).

2026-09-29: the Live lane's startup could not reach Alpaca for a few seconds,
and every routed account read answered "failed serving account_money_read
with 503. Retry once the lane recovers from the reported server error; the
lane's own log names the cause." Retrying could not help -- nothing retried
the selection -- and the log did not name the cause.

The lane now echoes why it serves no account authority beside its identity,
in closed words, and the coordinator authors the copy from that: reconnecting
recovers on its own; failed needs a restart and never promises a retry.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.broker.alpaca.clerk.active_authority import (
    reset_alpaca_clerk_for_testing,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.active_runtime import (
    ActiveClerkRuntime,
    reconnecting_refusal,
    terminal_startup_recovery,
    unavailable_runtime,
)
from app.broker.alpaca.clerk.fleet_boot import FleetLaneBoot, heartbeat_facts, report_authority_state
from app.broker.contract.errors import BrokerUnavailable
from app.broker.fleet.agent_identity import SERVED_IDENTITY_STATE_KEY, FleetIdentityMiddleware
from app.broker.fleet.delivery import ACCOUNT_AUTHORITY_HEADER, DeliveryRequest, DeliveryResult
from app.broker.fleet.errors import ClerkUnreachable
from app.broker.fleet.presence import SessionInfo
from tests.broker.fleet.conftest import FrozenClock
from tests.broker.fleet.test_provider_conformance import _alpha_operation, _routed_lane_with_handler

UNREACHABLE = BrokerUnavailable("Could not reach Alpaca while fetching positions.", broker="alpaca")


@pytest.fixture(autouse=True)
def _no_primary() -> Iterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


def _lane_answering_503(echo: str | None):
    """A lane handler that refuses a read with 503, echoing its identity as the middleware does."""

    async def _handler(request: DeliveryRequest) -> DeliveryResult:
        headers = {name.lower(): value for name, value in request.pinned_headers().items()}
        if echo is not None:
            headers[ACCOUNT_AUTHORITY_HEADER] = echo
        return DeliveryResult(status_code=503, headers=headers, body=b'{"detail": "no authority"}')

    return _handler


async def _routed_refusal(
    control_dir: Path, clock: FrozenClock, fleet_service, *, echo: str | None
) -> ClerkUnreachable:
    router, lane, _service = _routed_lane_with_handler(
        control_dir, clock, fleet_service, label=f"lane-{echo}", handler=_lane_answering_503(echo)
    )
    with pytest.raises(ClerkUnreachable) as refused:
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=_alpha_operation("read_account"),
            path_params={},
            query={},
        )
    return refused.value


async def test_a_reconnecting_lane_s_503_says_it_will_recover_on_its_own(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    refusal = await _routed_refusal(control_dir, clock, fleet_service, echo="reconnecting")

    assert refusal.status_code == 503
    assert "could not reach its broker when it started and is reconnecting" in refusal.message
    assert refusal.next_step == "It will recover on its own once its broker answers; no restart is needed."


async def test_a_failed_lane_s_503_names_the_fix_and_promises_no_retry(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    refusal = await _routed_refusal(control_dir, clock, fleet_service, echo="failed")

    assert "will not retry on its own" in refusal.message
    assert refusal.next_step == "The Clerk's log names the cause; restart the Clerk once that is fixed."
    assert "Retry" not in refusal.next_step


async def test_any_other_lane_503_keeps_the_generic_copy(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    refusal = await _routed_refusal(control_dir, clock, fleet_service, echo=None)

    assert "failed serving read_account with 503." in refusal.message
    assert "reconnecting" not in refusal.message


def _served_app(identity: dict[str, object]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(FleetIdentityMiddleware)
    setattr(app.state, SERVED_IDENTITY_STATE_KEY, lambda: identity)

    @app.get("/api/brokers/alpaca/clerk/status")
    async def _status() -> None:
        raise HTTPException(status_code=503, detail={"reason": "no_authority"})

    return app


async def _served_503(identity: dict[str, object]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_served_app(identity))
    async with httpx.AsyncClient(transport=transport, base_url="http://lane") as client:
        return await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": "clrk_live"},
        )


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


@pytest.mark.parametrize(
    ("installed", "echo"),
    [
        (reconnecting_refusal(UNREACHABLE, account_id="9LIVE0001"), "reconnecting"),
        (
            unavailable_runtime(
                "ACCOUNT_PIN_MISMATCH", account_id="9LIVE0001", recovery=terminal_startup_recovery("pin")
            ),
            "failed",
        ),
    ],
)
async def test_the_lane_echoes_why_it_serves_no_account_authority(
    installed: ActiveClerkRuntime, echo: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import main

    monkeypatch.setattr(main, "_effective_binding_generation_now", lambda: None)
    set_active_clerk_runtime(installed)

    identity = main.fleet_served_identity(_lane_boot(tmp_path))
    assert identity is not None
    response = await _served_503({**identity, "broker": "alpaca"})

    assert response.status_code == 503
    assert response.headers[ACCOUNT_AUTHORITY_HEADER] == echo
    assert response.headers["x-fleet-clerk-id"] == "clrk_live"


async def test_a_lane_whose_authority_serves_echoes_nothing_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import main

    monkeypatch.setattr(main, "_effective_binding_generation_now", lambda: None)
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=object(), account_id="9LIVE0001"))

    identity = main.fleet_served_identity(_lane_boot(tmp_path))
    assert identity is not None
    response = await _served_503(identity)

    assert ACCOUNT_AUTHORITY_HEADER not in response.headers


def test_the_beat_reports_reconnecting_then_what_the_reconnect_installed(tmp_path: Path) -> None:
    """The directory tells the same truth: reconnecting while it retries, the world once it installs."""
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

    report_authority_state(boot, authority_kind="sqlite")

    assert boot.reported_facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "real_live",
    }
    # The grant it confirmed at boot is what it still reports; nothing is re-confirmed.
    assert boot.reported_facts["reported_state"] == "binding_confirmed"
    assert boot.reported_facts["reported_binding_generation"] == 4
