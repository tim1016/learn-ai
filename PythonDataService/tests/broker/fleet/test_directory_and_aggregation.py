"""The broker-neutral directory and provenance-preserving aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.errors import (
    ClerkUnreachable,
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
#: which ADR 0062 Decision 1 forbids.
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
        # The worker key never crosses the public projection (ADR 0062 Decision 3).
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
    # inferred in either direction (ADR 0062 Decision 6).
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_alpha", capability=Capability.GALLERY_READ)
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_beta", capability=Capability.BOT_ACTION)


def test_lifecycle_projects_from_observations_not_stored_flags(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """A historical acknowledgement never presents as current liveness, and a
    heartbeat alone never presents as a confirmed binding."""
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
    # it did not fail the healthy lane.
    assert lanes[1]["ok"] is False
    assert lanes[1]["broker"] == "fake_beta"
    assert lanes[1]["error_reason"] == "clerk_unreachable"
    # A failed lane carries no value at all — the healthy lane's fact never
    # substitutes for it.
    assert "value" not in lanes[1]


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
    # same clerk is a state only a broken writer could produce. Since v7 the
    # schema refuses it (test below), so the corruption first removes that
    # fence — the projection stays a second line behind it.
    with fleet_service._store.transaction() as conn:
        conn.execute("DROP INDEX ix_account_assignments_owner")
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


def test_the_schema_refuses_a_second_live_assignment_for_one_clerk(
    control_dir: Path, fleet_service
) -> None:
    """v7 (#2154): one live assignment per clerk is structural, so a
    lane-quiet confirmation — which names no account — covers exactly one."""
    import sqlite3

    lane = _live(fleet_service, control_dir.parent, "fake_alpha", "single")
    with (
        pytest.raises(sqlite3.IntegrityError, match=r"account_assignments\.clerk_id"),
        fleet_service._store.transaction() as conn,
    ):
        conn.execute(
            "INSERT INTO account_assignments (broker, canonical_external_account_id, "
            "clerk_id, assignment_generation, state, recorded_at_ms, updated_at_ms) "
            "VALUES ('fake_alpha', 'ACCT-SECOND', ?, 1, 'reserved', 1, 1)",
            (lane.clerk_id,),
        )


# ---- HTTP: GET /broker-clerks/aggregate/directory --------------------------
_TEST_SECRET = "test-aggregate-directory-secret"


def _coordinator_app(fleet_service) -> FastAPI:
    """The minimal coordinator surface this read needs: no lane router, no agent."""
    app = FastAPI()
    app.state.fleet_service = fleet_service
    install_fleet_control_error_handler(app)
    app.include_router(broker_clerks.router)
    return app


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


@pytest.mark.asyncio
async def test_http_directory_carries_the_lanes_own_bot_and_attention_counts(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD #2560: the Accounts page reads each lane's running and attention
    counts from one directory field each; a count the lane did not report is
    absent, never zero."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"alpaca": AlpacaProviderAdapter()},
        clock=clock,
    )
    try:
        lane = provision_lane(service, broker="alpaca", label="count-lane", tmp_path=control_dir.parent)
        session, _confirmed = bind_lane(service, lane, account="acct-counts")
        service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_summary={
                "endpoint_mode": "paper", "authority_state": "real_paper",
                "running_count": 2, "dry_run_count": 1, "attention_count": 0,
            },
        )
        async with AsyncClient(transport=ASGITransport(app=_coordinator_app(service)), base_url="http://test") as client:
            response = await client.get("/api/broker-clerks", headers={CONTROL_SECRET_HEADER: _TEST_SECRET})

        summary = response.json()["clerks"][0]["provider_summary"]
        assert (summary["running_count"], summary["dry_run_count"], summary["attention_count"]) == (2, 1, 0)
        assert "account_nickname" not in summary
    finally:
        service.close()


# ── async partial aggregation: lane-scoped reads that leave the process ─────


async def test_async_partial_aggregation_isolates_one_lanes_exception(
    fleet_service,
) -> None:
    """Async readers: one lane raising is its own ``ok: False`` entry, never an
    omission or a global failure (#2228)."""

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
    never a quiet lane, and never a delay for its siblings."""
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
