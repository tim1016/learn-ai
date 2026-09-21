"""The broker-neutral directory and provenance-preserving aggregation (PRD §9.9, §10.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.errors import (
    ClerkUnreachable,
    DataPlaneControlSecretRefused,
    FleetControlPlaneNotInstalled,
)
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.config import settings
from app.routers import broker_clerks
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from tests.broker.fleet.conftest import (
    FAKE_ALPHA_CAPABILITIES,
    FAKE_BETA_CAPABILITIES,
    TEST_CHANGE_REF,
    TEST_OPERATOR,
    FrozenClock,
    bind_lane,
    provision_lane,
    release_after_drain,
)

#: Every field the directory may carry. Anything else — especially a balance,
#: position, P&L or exposure quantity — is a fleet-computed financial fact,
#: which FR-034 forbids.
_ALLOWED_DIRECTORY_FIELDS = {
    "broker",
    "clerk_id",
    "display_label",
    "lifecycle_state",
    "volume_id",
    "last_seen_at_ms",
    "routing_epoch",
    "effective_binding_generation",
    "capabilities",
    "provider_summary",
    "observed_at_ms",
    "draining_since_ms",
    "drain_deadline_at_ms",
}


def _live(fleet_service, tmp_path: Path, broker: str, label: str):
    """Provision, register, reserve and confirm one lane so it projects ready."""
    lane = provision_lane(fleet_service, broker=broker, label=label, tmp_path=tmp_path)
    session = fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker=broker, clerk_id=lane.clerk_id, external_account_id=f"acct-{label}"
    )
    fleet_service.confirm_assignment(
        broker=broker,
        clerk_id=lane.clerk_id,
        external_account_id=f"acct-{label}",
        binding_generation=3,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    return lane


def test_every_entry_carries_only_the_allowed_fields_and_never_the_worker_key(
    control_dir: Path, fleet_service
) -> None:
    """Directory entries carry exactly the allowed fields and never the worker key."""
    alpha = _live(fleet_service, control_dir.parent, "fake_alpha", "paper")
    beta = _live(fleet_service, control_dir.parent, "fake_beta", "live")
    directory = fleet_service.directory()

    assert set(directory) == {"observed_at_ms", "clerks"}
    entries = {entry["clerk_id"]: entry for entry in directory["clerks"]}
    assert set(entries) == {alpha.clerk_id, beta.clerk_id}

    for entry in entries.values():
        assert set(entry) == _ALLOWED_DIRECTORY_FIELDS
        rendered = repr(entry)
        # The worker key never crosses the public projection (FR-012).
        for lane in (alpha, beta):
            assert lane.worker_key not in rendered

    alpha_entry = entries[alpha.clerk_id]
    assert alpha_entry["broker"] == "fake_alpha"
    assert alpha_entry["lifecycle_state"] == "ready"
    assert alpha_entry["effective_binding_generation"] == 3
    assert alpha_entry["capabilities"] == sorted(cap.value for cap in FAKE_ALPHA_CAPABILITIES)
    assert alpha_entry["provider_summary"]["provider_id"] == "fake_alpha"
    assert entries[beta.clerk_id]["capabilities"] == sorted(
        cap.value for cap in FAKE_BETA_CAPABILITIES
    )


def test_capability_evidence_differs_by_provider_and_undeclared_actions_refuse(
    fleet_service,
) -> None:
    """Capability evidence is per provider; no parity is inferred in either direction."""
    from app.broker.fleet.errors import BrokerClerkCapabilityUnavailable
    from app.broker.fleet.provider import Capability

    assert FAKE_ALPHA_CAPABILITIES != FAKE_BETA_CAPABILITIES

    # Both declare account_read…
    fleet_service.require_capability(broker="fake_alpha", capability=Capability.ACCOUNT_READ)
    fleet_service.require_capability(broker="fake_beta", capability=Capability.ACCOUNT_READ)
    # …but gallery_read is beta's alone, and bot_action alpha's — no parity is
    # inferred in either direction (FR-006).
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_alpha", capability=Capability.GALLERY_READ)
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_beta", capability=Capability.BOT_ACTION)


def test_lifecycle_projects_from_observations_not_stored_flags(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """FR-081 + audit 2026-09-13 finding 1: a historical acknowledgement never
    presents as current liveness, and a heartbeat alone never presents as a
    confirmed binding."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="projecting", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)

    def entry() -> dict:
        """The directory entry for the projecting clerk, re-read per assertion."""
        return next(
            e for e in fleet_service.directory()["clerks"] if e["clerk_id"] == lane.clerk_id
        )

    assert entry()["lifecycle_state"] == "starting"  # session, nothing confirmed

    # A heartbeat carrying plausible binding facts proves nothing: without a
    # confirmed assignment the lane still projects starting.
    fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=fleet_service._store.read_session(lane.clerk_id).agent_instance_id,
        reported_binding_generation=1,
        reported_state="binding_confirmed",
    )
    assert entry()["lifecycle_state"] == "starting"
    assert entry()["effective_binding_generation"] is None

    # The confirmed assignment is what projects ready, with its generation.
    session = fleet_service._store.read_session(lane.clerk_id)
    assert session is not None
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-projecting"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-projecting",
        binding_generation=1,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    assert entry()["lifecycle_state"] == "ready"
    assert entry()["effective_binding_generation"] == 1

    fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=fleet_service._store.read_session(lane.clerk_id).agent_instance_id,
        reported_state="degraded",
    )
    assert entry()["lifecycle_state"] == "degraded"

    clock.advance(60_000)  # far past the 30 s staleness window
    assert entry()["lifecycle_state"] == "unreachable"


def test_retired_lanes_leave_the_default_directory(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Retired lanes disappear from the default directory and stay marked when
    included. A served lane leaves through the drain ceremony: release after
    the deadline, then the force-retire exit (no provider answers lane quiet
    yet, ADR 0063)."""
    keeper = _live(fleet_service, control_dir.parent, "fake_alpha", "keeper")
    retiring = _live(fleet_service, control_dir.parent, "fake_alpha", "retiring")
    release_after_drain(fleet_service, clock, retiring, account="acct-retiring")
    fleet_service.force_retire_clerk(
        clerk_id=retiring.clerk_id, operator=TEST_OPERATOR, change_ref=TEST_CHANGE_REF
    )
    # The drain's deadline wait out-staled the keeper's heartbeat; refresh it
    # so the directory below reflects the drain ceremony, not staleness.
    keeper_session = fleet_service._store.read_session(keeper.clerk_id)
    assert keeper_session is not None
    fleet_service.observe_session(
        clerk_id=keeper.clerk_id,
        agent_instance_id=keeper_session.agent_instance_id,
    )

    default = fleet_service.directory()
    assert [entry["clerk_id"] for entry in default["clerks"]] == [keeper.clerk_id]
    with_retired = fleet_service.directory(include_retired=True)
    assert {entry["lifecycle_state"] for entry in with_retired["clerks"]} == {"ready", "retired"}


def test_partial_aggregation_reports_each_lane_without_omission_or_substitution(
    control_dir: Path, fleet_service
) -> None:
    """One lane's failure is reported explicitly and never fails the healthy lane."""
    healthy = _live(fleet_service, control_dir.parent, "fake_alpha", "healthy")
    broken = _live(fleet_service, control_dir.parent, "fake_beta", "broken")
    result = fleet_service.aggregate_lane_reads(
        [
            (
                "fake_alpha",
                healthy.clerk_id,
                lambda: {"observation": "alpha-facts"},
            ),
            (
                "fake_beta",
                broken.clerk_id,
                lambda: (_ for _ in ()).throw(ClerkUnreachable("agent timed out")),
            ),
        ]
    )

    lanes = result["lanes"]
    assert [lane["clerk_id"] for lane in lanes] == [healthy.clerk_id, broken.clerk_id]
    assert lanes[0] == {
        "broker": "fake_alpha",
        "clerk_id": healthy.clerk_id,
        "ok": True,
        "value": {"observation": "alpha-facts"},
    }
    # The failed lane is explicit partial failure: reported, not omitted, and
    # it did not fail the healthy lane (FR-084).
    assert lanes[1]["ok"] is False
    assert lanes[1]["broker"] == "fake_beta"
    assert lanes[1]["error_reason"] == "clerk_unreachable"
    # A failed lane carries no value at all — the healthy lane's fact never
    # substitutes for it.
    assert "value" not in lanes[1]


def test_aggregate_directory_reads_mirrors_directory_when_every_clerk_projects_cleanly(
    control_dir: Path, fleet_service
) -> None:
    """The resilient directory read (FR-083/084) carries every registered
    clerk, ``ok: True``, with the same per-lane data ``directory()`` computes."""
    alpha = _live(fleet_service, control_dir.parent, "fake_alpha", "agg-healthy")
    beta = _live(fleet_service, control_dir.parent, "fake_beta", "agg-healthy-2")

    result = fleet_service.aggregate_directory_reads()

    assert set(result) == {"observed_at_ms", "lanes"}
    lanes = {lane["clerk_id"]: lane for lane in result["lanes"]}
    assert set(lanes) == {alpha.clerk_id, beta.clerk_id}
    assert all(lane["ok"] is True for lane in lanes.values())
    directory_entries = {
        entry["clerk_id"]: entry for entry in fleet_service.directory()["clerks"]
    }
    assert lanes[alpha.clerk_id]["value"] == directory_entries[alpha.clerk_id]
    assert lanes[beta.clerk_id]["value"] == directory_entries[beta.clerk_id]


def test_aggregate_directory_reads_isolates_one_clerks_projection_failure(
    control_dir: Path, fleet_service
) -> None:
    """One clerk's ``describe_clerk`` failure is that lane's own ``ok: False``
    entry; the other clerk still reports ``ok: True`` — the actual FR-083/084
    behavior this resilient read exists to prove, unlike ``directory()``'s
    plain loop which has no per-lane exception isolation at all."""
    healthy = _live(fleet_service, control_dir.parent, "fake_alpha", "agg-ok")
    broken = _live(fleet_service, control_dir.parent, "fake_beta", "agg-broken")
    original_describe_clerk = fleet_service.describe_clerk

    def flaky_describe_clerk(clerk_id: str):
        if clerk_id == broken.clerk_id:
            raise ClerkUnreachable("agent timed out")
        return original_describe_clerk(clerk_id)

    fleet_service.describe_clerk = flaky_describe_clerk
    try:
        result = fleet_service.aggregate_directory_reads()
    finally:
        del fleet_service.describe_clerk

    lanes = {lane["clerk_id"]: lane for lane in result["lanes"]}
    assert lanes[healthy.clerk_id]["ok"] is True
    assert "value" in lanes[healthy.clerk_id]
    assert lanes[broken.clerk_id]["ok"] is False
    assert lanes[broken.clerk_id]["error_reason"] == "clerk_unreachable"
    # A failed lane carries no value at all — the healthy lane's fact never
    # substitutes for it (FR-084).
    assert "value" not in lanes[broken.clerk_id]


def test_a_clerk_holding_multiple_effective_assignments_is_surfaced_degraded(
    control_dir: Path, fleet_service
) -> None:
    """Regression (independent review): a corrupted multi-assignment state is
    surfaced — degraded lifecycle, flagged observation — never silently
    first-row-projected, and execution routing refuses it."""
    from app.broker.fleet.errors import ClerkIdentityMismatch
    from app.broker.fleet.records import AssignmentState
    from app.broker.fleet.service import OperationReadiness

    lane = _live(fleet_service, control_dir.parent, "fake_alpha", "anomalous")
    # Corrupt the registry directly: a second effective assignment for the
    # same clerk is a state only a broken writer could produce.
    with fleet_service._store.transaction() as conn:
        conn.execute(
            "INSERT INTO account_assignments (broker, canonical_external_account_id, "
            "clerk_id, assignment_generation, state, confirmed_binding_generation, "
            "confirmed_at_ms, recorded_at_ms, updated_at_ms) VALUES "
            "('fake_alpha', 'ACCT-SHADOW', ?, 1, 'effective', 9, 1, 1, 1)",
            (lane.clerk_id,),
        )
    entry = next(
        e for e in fleet_service.directory()["clerks"] if e["clerk_id"] == lane.clerk_id
    )
    assert entry["lifecycle_state"] == "degraded"
    assert fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-ANOMALOUS"
    ).state == AssignmentState.EFFECTIVE
    with pytest.raises(ClerkIdentityMismatch, match="effective assignments"):
        fleet_service.resolve_route(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            readiness=OperationReadiness.EXECUTION,
        )
    # Configuration access still routes: the repair path stays reachable.
    fleet_service.resolve_route(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        readiness=OperationReadiness.CONFIGURATION_ACCESS,
    )


# ---- HTTP: GET /broker-clerks/aggregate/directory (PRD FR-083/084) --------

_AGGREGATE_ROUTE = "/api/broker-clerks/aggregate/directory"
_TEST_SECRET = "test-aggregate-directory-secret"


def _coordinator_app(fleet_service) -> FastAPI:
    """The minimal coordinator surface this read needs: no lane router, no agent."""
    app = FastAPI()
    app.state.fleet_service = fleet_service
    install_fleet_control_error_handler(app)
    app.include_router(broker_clerks.router)
    return app


@pytest.mark.asyncio
async def test_http_aggregate_directory_returns_every_clerk_ok_true_when_all_project_cleanly(
    control_dir: Path, fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    alpha = _live(fleet_service, control_dir.parent, "fake_alpha", "http-agg-alpha")
    beta = _live(fleet_service, control_dir.parent, "fake_beta", "http-agg-beta")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["observed_at_ms"], int)
    lanes = {lane["clerk_id"]: lane for lane in body["lanes"]}
    assert set(lanes) == {alpha.clerk_id, beta.clerk_id}
    assert all(lane["ok"] is True for lane in lanes.values())


@pytest.mark.asyncio
async def test_http_aggregate_directory_isolates_one_clerks_failure_from_the_other(
    control_dir: Path, fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router-level twin of the service test above: one clerk's exception
    reaches the wire as that lane's ``ok: False``, never a 500 for the page."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    healthy = _live(fleet_service, control_dir.parent, "fake_alpha", "http-agg-ok")
    broken = _live(fleet_service, control_dir.parent, "fake_beta", "http-agg-broken")
    original_describe_clerk = fleet_service.describe_clerk

    def flaky_describe_clerk(clerk_id: str):
        if clerk_id == broken.clerk_id:
            raise ClerkUnreachable("agent timed out")
        return original_describe_clerk(clerk_id)

    fleet_service.describe_clerk = flaky_describe_clerk
    app = _coordinator_app(fleet_service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
            )
    finally:
        del fleet_service.describe_clerk

    assert response.status_code == 200
    lanes = {lane["clerk_id"]: lane for lane in response.json()["lanes"]}
    assert lanes[healthy.clerk_id]["ok"] is True
    assert lanes[broken.clerk_id]["ok"] is False
    assert lanes[broken.clerk_id]["error_reason"] == "clerk_unreachable"
    assert "value" not in lanes[broken.clerk_id]


@pytest.mark.asyncio
async def test_http_aggregate_directory_rejects_the_request_without_the_secret_header(
    fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_AGGREGATE_ROUTE)

    assert response.status_code == 403
    assert response.json()["reason"] == DataPlaneControlSecretRefused.reason


@pytest.mark.asyncio
async def test_http_aggregate_directory_rejects_the_wrong_secret_header(
    fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: "wrong"}
        )

    assert response.status_code == 403
    assert response.json()["reason"] == DataPlaneControlSecretRefused.reason


@pytest.mark.asyncio
async def test_http_aggregate_directory_accepts_the_request_with_the_correct_secret_header(
    fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the two rejections above: the identical route,
    correctly authenticated, succeeds — so the 403s are proven to come from
    the secret gate, not some other shared misconfiguration."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["observed_at_ms"], int)
    assert body["lanes"] == []


@pytest.mark.asyncio
async def test_http_aggregate_directory_503s_when_fleet_service_is_absent_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler carries no local ``try``/``except`` (see its docstring):
    ``aggregate_lane_reads`` already isolates every per-lane exception, so
    the only ``FleetControlError`` this route can ever see is an uninstalled
    fleet service, and that case is left to the coordinator's global handler
    -- exactly like ``GET /broker-clerks`` above it. This proves the global
    handler still turns a missing fleet service into the existing 503
    family, never a raw 500."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = FastAPI()
    install_fleet_control_error_handler(app)
    app.include_router(broker_clerks.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 503
    assert response.json()["reason"] == FleetControlPlaneNotInstalled.reason


@pytest.mark.asyncio
async def test_http_directory_carries_the_alpaca_account_nickname_in_the_provider_summary(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2182: `GET /broker-clerks` is the end-to-end path the frontend reads
    for one account name everywhere — a confirmed lane's reported nickname
    must reach the wire inside Alpaca's own provider-authored summary, not
    just the in-process ``fleet_service.directory()`` projection the other
    tests in this module use with the fake providers."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"alpaca": AlpacaProviderAdapter()},
        clock=clock,
    )
    try:
        lane = provision_lane(
            service, broker="alpaca", label="nickname-lane", tmp_path=control_dir.parent
        )
        session, _confirmed = bind_lane(service, lane, account="acct-nickname")
        service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_summary={
                "endpoint_mode": "paper",
                "authority_state": "real_paper",
                "account_nickname": "Strategy lab",
            },
        )
        app = _coordinator_app(service)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/broker-clerks", headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
            )

        assert response.status_code == 200
        entry = response.json()["clerks"][0]
        assert entry["broker"] == "alpaca"
        assert entry["provider_summary"]["account_nickname"] == "Strategy lab"
    finally:
        service.close()


# ── async partial aggregation: lane-scoped reads that leave the process ─────


async def test_async_partial_aggregation_isolates_one_lanes_exception(
    fleet_service,
) -> None:
    """FR-083/084 for async readers: one lane raising is its own ``ok: False``
    entry, never an omission or a global failure (#2228)."""

    async def healthy() -> dict[str, object]:
        return {"items": ["a"]}

    async def broken() -> dict[str, object]:
        raise RuntimeError("agent unreachable")

    result = await fleet_service.aggregate_lane_reads_async(
        [
            ("fake_alpha", "alpha-1", healthy),
            ("fake_beta", "beta-1", broken),
        ]
    )

    (alpha, beta) = result["lanes"]
    assert (alpha["broker"], alpha["clerk_id"], alpha["ok"]) == ("fake_alpha", "alpha-1", True)
    assert alpha["value"] == {"items": ["a"]}
    assert beta["ok"] is False
    assert beta["error_reason"] == "RuntimeError"
    assert "agent unreachable" in beta["error_message"]


async def test_async_partial_aggregation_surfaces_a_timed_out_lane_explicitly(
    fleet_service,
) -> None:
    """A slow lane is an explicit ``LaneReadTimeout`` failure of its own —
    never a quiet lane, and never a delay for its siblings (FR-093)."""
    import asyncio

    started = asyncio.get_event_loop().time()

    async def slow() -> dict[str, object]:
        await asyncio.sleep(10)
        return {"never": True}

    async def quick() -> dict[str, object]:
        return {"items": []}

    result = await fleet_service.aggregate_lane_reads_async(
        [("fake_alpha", "alpha-1", slow), ("fake_beta", "beta-1", quick)],
        lane_timeout_s=0.05,
    )

    elapsed = asyncio.get_event_loop().time() - started
    (alpha, beta) = result["lanes"]
    assert alpha["ok"] is False
    assert alpha["error_reason"] == "LaneReadTimeout"
    assert beta["ok"] is True
    assert elapsed < 5  # the timeout bounded the whole aggregate, not just one lane


async def test_async_partial_aggregation_awaits_lanes_concurrently(
    fleet_service,
) -> None:
    """Two half-second reads complete in about half a second, not one: the
    aggregate never serializes lane reads behind each other."""
    import asyncio

    async def half_second() -> dict[str, object]:
        await asyncio.sleep(0.5)
        return {"items": []}

    started = asyncio.get_event_loop().time()
    result = await fleet_service.aggregate_lane_reads_async(
        [("fake_alpha", "alpha-1", half_second), ("fake_beta", "beta-1", half_second)]
    )
    elapsed = asyncio.get_event_loop().time() - started

    assert all(lane["ok"] for lane in result["lanes"])
    assert elapsed < 0.9


async def test_async_partial_aggregation_answers_empty_lanes_explicitly(
    fleet_service,
) -> None:
    result = await fleet_service.aggregate_lane_reads_async([])
    assert result["lanes"] == []
    assert isinstance(result["observed_at_ms"], int)
