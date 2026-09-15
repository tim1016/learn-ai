"""Fake-provider conformance: N clerks across two adapters (PRD Phase 6 gate).

``fake_alpha`` and ``fake_beta`` run as the only two providers. What is proved
here is the extension boundary itself: provider-qualified assignment,
wrong-provider refusal, capability differences, distinct volumes, independent
state, and partial aggregation — with the generic spine importing no provider
implementation (``test_import_isolation.py``) and the fakes living only in
tests, never in the production registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    BrokerClerkCapabilityUnavailable,
    ClerkAssignmentConflict,
    ClerkBrokerMismatch,
)
from app.broker.fleet.provider import Capability
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from tests.broker.fleet.conftest import FrozenClock, provision_lane

RELEASE_PROOF = "old-clerk-offline-and-obligations-clear"


def test_six_clerks_across_two_providers_hold_distinct_volumes_and_one_registry(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Six clerks, two providers, distinct volumes, one registry, no cross-talk."""
    lanes = []
    for index in range(3):
        lanes.append(
            provision_lane(
                fleet_service,
                broker="fake_alpha",
                label=f"alpha-{index}",
                tmp_path=control_dir.parent,
            )
        )
        lanes.append(
            provision_lane(
                fleet_service,
                broker="fake_beta",
                label=f"beta-{index}",
                tmp_path=control_dir.parent,
            )
        )

    volume_ids = set()
    attestations = set()
    for lane in lanes:
        session = fleet_service.register_agent_session(
            fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key
        )
        fleet_service.reserve_assignment(
            broker=lane.broker, clerk_id=lane.clerk_id, external_account_id=f"acct-{lane.attestation_id}"
        )
        fleet_service.confirm_assignment(
            broker=lane.broker,
            clerk_id=lane.clerk_id,
            external_account_id=f"acct-{lane.attestation_id}",
            binding_generation=1,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
        )
        volume_ids.add(lane.attestation_id)
        attestations.add(lane.attestation_id)

    assert len(volume_ids) == len(lanes)
    assert len(attestations) == len(lanes)

    directory = fleet_service.directory()
    assert len(directory["clerks"]) == len(lanes)
    assert {entry["broker"] for entry in directory["clerks"]} == {"fake_alpha", "fake_beta"}
    assert all(entry["lifecycle_state"] == "ready" for entry in directory["clerks"])


def test_a_fake_alpha_clerk_refuses_a_fake_beta_route(
    control_dir: Path, fleet_service
) -> None:
    """A clerk is routable and assignable only under its own immutable provider."""
    alpha = provision_lane(
        fleet_service, broker="fake_alpha", label="cross", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.reserve_assignment(
            broker="fake_beta", clerk_id=alpha.clerk_id, external_account_id="acct-1"
        )
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=alpha.clerk_id)


def test_provider_clients_and_state_share_no_mutable_object(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Each service construction gets its own adapter instances; summaries do
    not leak between providers (the adapters are per-mapping objects)."""
    from tests.broker.fleet.conftest import fake_alpha, fake_beta

    alpha_adapter = fake_alpha()
    beta_adapter = fake_beta()
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": alpha_adapter, "fake_beta": beta_adapter},
        clock=clock,
    )
    try:
        alpha_lane = provision_lane(
            service, broker="fake_alpha", label="iso-a", tmp_path=control_dir.parent
        )
        beta_lane = provision_lane(
            service, broker="fake_beta", label="iso-b", tmp_path=control_dir.parent
        )
        service.register_agent_session(fleet_protocol_version=2, clerk_id=alpha_lane.clerk_id, worker_key=alpha_lane.worker_key)
        service.register_agent_session(fleet_protocol_version=2, clerk_id=beta_lane.clerk_id, worker_key=beta_lane.worker_key)
        entries = {
            entry["clerk_id"]: entry
            for entry in service.directory()["clerks"]
        }
        assert entries[alpha_lane.clerk_id]["provider_summary"]["provider_id"] == "fake_alpha"
        assert entries[beta_lane.clerk_id]["provider_summary"]["provider_id"] == "fake_beta"
        # The summary calls recorded on one adapter never appear on the other.
        assert len(alpha_adapter.summaries) == 1
        assert len(beta_adapter.summaries) == 1
    finally:
        service.close()


def test_killing_one_clerks_volume_does_not_mutate_another(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Corrupting one lane's volume leaves the other lane verified, routable and owning its assignment."""
    survivor = provision_lane(
        fleet_service, broker="fake_alpha", label="survivor", tmp_path=control_dir.parent
    )
    casualty = provision_lane(
        fleet_service, broker="fake_beta", label="casualty", tmp_path=control_dir.parent
    )
    for lane in (survivor, casualty):
        fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
        fleet_service.reserve_assignment(
            broker=lane.broker, clerk_id=lane.clerk_id, external_account_id="acct-same"
        )

    # "Kill" the casualty: corrupt its volume marker beyond recognition.
    (casualty.volume_root / ".learn-ai-clerk-volume.json").write_text("{corrupt")
    with pytest.raises(Exception):
        fleet_service.verify_clerk_volume(
            clerk_id=casualty.clerk_id, volume_root=casualty.volume_root
        )

    # The survivor verifies, routes and keeps its assignment untouched.
    fleet_service.verify_clerk_volume(
        clerk_id=survivor.clerk_id, volume_root=survivor.volume_root
    )
    survivor_session = fleet_service._store.read_session(survivor.clerk_id)
    assert survivor_session is not None
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=survivor.clerk_id,
        external_account_id="acct-same",
        binding_generation=1,
        agent_instance_id=survivor_session.agent_instance_id,
        routing_epoch=survivor_session.routing_epoch,
    )
    fleet_service.resolve_route(broker="fake_alpha", clerk_id=survivor.clerk_id)
    survivor_assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-SAME"
    )
    assert survivor_assignment is not None
    assert survivor_assignment.clerk_id == survivor.clerk_id


def test_racing_the_same_account_yields_one_winner_and_a_durable_refusal(
    control_dir: Path, fleet_service
) -> None:
    """Provider-qualified keys coexist; within one provider the rival is durably refused."""
    first = provision_lane(
        fleet_service, broker="fake_alpha", label="race-a", tmp_path=control_dir.parent
    )
    second = provision_lane(
        fleet_service, broker="fake_beta", label="race-b", tmp_path=control_dir.parent
    )
    # Same raw account, but the providers canonicalize it differently: this
    # only shows two different keys don't collide, not that one canonical key
    # coexists across providers (see
    # test_one_canonical_key_belongs_to_each_provider_independently in
    # test_assignments.py for that proof).
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=first.clerk_id, external_account_id="acct-dup"
    )
    beta_reservation = fleet_service.reserve_assignment(
        broker="fake_beta", clerk_id=second.clerk_id, external_account_id="acct-dup"
    )
    assert beta_reservation.canonical_external_account_id == "acct_dup"
    # Within one provider, the second clerk's reservation is durably refused.
    rival = provision_lane(
        fleet_service, broker="fake_alpha", label="race-c", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.reserve_assignment(
            broker="fake_alpha", clerk_id=rival.clerk_id, external_account_id="acct-dup"
        )
    assert (
        fleet_service._store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-DUP"
        ).clerk_id
        == first.clerk_id
    )
    # Beta's own canonical form is not what alpha reserved under.
    assert (
        fleet_service._store.read_assignment(
            broker="fake_beta", canonical_account_id="ACCT-DUP"
        )
        is None
    )


def test_undeclared_capabilities_refuse_with_evidence_not_emulation(
    control_dir: Path, fleet_service
) -> None:
    """An undeclared capability refuses, naming the provider and the capability."""
    with pytest.raises(BrokerClerkCapabilityUnavailable) as excinfo:
        fleet_service.require_capability(
            broker="fake_beta", capability=Capability.CUSTODY_READ
        )
    assert "fake_beta" in str(excinfo.value)
    assert "custody_read" in str(excinfo.value)


def test_the_generic_spine_survives_a_provider_adapter_refusal(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """A provider raising inside canonicalization surfaces as its own error,
    not as registry corruption."""
    from dataclasses import replace

    from tests.broker.fleet.conftest import fake_alpha, fake_beta

    strict = replace(fake_alpha(), refused_accounts=frozenset({"acct-bad"}))
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": strict, "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="strict", tmp_path=control_dir.parent
    )
    with pytest.raises(LookupError):
        service.reserve_assignment(
            broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-bad"
        )
    # The registry recorded nothing for the refused canonicalization.
    assert (
        service._store.read_assignment(broker="fake_alpha", canonical_account_id="ACCT-BAD")
        is None
    )


def test_test_fakes_are_absent_from_production_composition_and_openapi() -> None:
    """The two conformance fakes never become deployable providers or API surface."""
    from app.broker.fleet_composition import production_provider_adapters

    repository_root = Path(__file__).resolve().parents[4]
    assert set(production_provider_adapters()) == {"alpaca"}
    for relative_path in (
        "compose.yaml",
        "compose.fleet.yaml",
        "PythonDataService/app/broker/fleet_composition.py",
        "contracts/openapi/python-data-service.openapi.json",
    ):
        artifact = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "fake_alpha" not in artifact
        assert "fake_beta" not in artifact


async def test_a_provider_refusing_the_served_context_closes_the_route(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The provider safety gate runs at the routing seam, before dispatch."""
    import dataclasses

    from app.broker.fleet.errors import BrokerClerkCapabilityUnavailable
    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import bind_lane, fake_alpha, fake_beta, provision_lane

    strict = dataclasses.replace(fake_alpha(), served_context_refusals=["account_read"])
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": strict, "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="served", tmp_path=control_dir.parent
    )
    bind_lane(service, lane, account="acct-served")

    def _never(broker: str, session):
        raise AssertionError("dispatch must not be reached past a provider refusal")

    router = LaneRouter(service=service, delivery_for=_never)
    operation = next(
        op for op in strict.operations() if op.operation_id == "account_read"
    )
    with pytest.raises(BrokerClerkCapabilityUnavailable, match="refuses to serve"):
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=operation,
            path_params={},
            query={},
        )


async def test_a_typed_provider_refusal_from_served_context_passes_through_unchanged(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """A FleetControlError subclass from the provider gate is not re-wrapped."""
    import dataclasses

    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import FakeProviderAdapter, bind_lane, fake_alpha, fake_beta, provision_lane

    class _TypedRefusalAdapter(FakeProviderAdapter):
        def validate_served_context(self, context) -> None:
            raise ClerkAssignmentConflict("already claimed by another clerk")

    base = fake_alpha()
    strict = _TypedRefusalAdapter(
        **{f.name: getattr(base, f.name) for f in dataclasses.fields(base)}
    )
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": strict, "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="typed", tmp_path=control_dir.parent
    )
    bind_lane(service, lane, account="acct-typed")

    def _never(broker: str, session):
        raise AssertionError("dispatch must not be reached past a provider refusal")

    router = LaneRouter(service=service, delivery_for=_never)
    operation = next(
        op for op in strict.operations() if op.operation_id == "account_read"
    )
    with pytest.raises(ClerkAssignmentConflict, match="already claimed"):
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=operation,
            path_params={},
            query={},
        )


async def test_a_delivery_contract_violation_on_a_read_is_not_misdiagnosed_as_identity(
    control_dir: Path, clock: FrozenClock, fleet_service, caplog: pytest.LogCaptureFixture
) -> None:
    """#2119: a handler-shape defect on ``deliver_read`` must not be reported
    to the operator as a lane-identity mismatch with "refresh and retry"
    advice that cannot fix a deterministic bug -- and it must be logged with
    the exception's own detail, which the generic ``except Exception``
    fallback in ``deliver_read`` provides without a dedicated except clause
    (``DeliveryContractViolation`` no longer subclasses
    ``DeliveryIdentityMismatch``, so it never reaches that branch).

    This is the routing-seam half of the #2119 fix; ``test_a2_alpaca_lane.py``'s
    ``test_local_delivery_refuses_a_handler_that_returns_the_wrong_result_type``
    proves the delivery layer raises the distinct ``DeliveryContractViolation``
    in the first place.
    """
    import logging

    from app.broker.fleet.delivery import DeliveryContractViolation
    from app.broker.fleet.errors import ClerkIdentityMismatch, ClerkUnreachable
    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import bind_lane, fake_alpha, fake_beta, provision_lane

    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": fake_alpha(), "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="contract-violation", tmp_path=control_dir.parent
    )
    bind_lane(service, lane, account="acct-contract-violation")

    class _ContractViolatingDelivery:
        async def deliver(self, request: object) -> None:
            del request
            raise DeliveryContractViolation("the in-process handler returned str, not a DeliveryResult")

    router = LaneRouter(service=service, delivery_for=lambda broker, session: _ContractViolatingDelivery())
    operation = next(
        op for op in fake_alpha().operations() if op.operation_id == "account_read"
    )

    caplog.set_level(logging.WARNING, logger="app.broker.fleet.routing")
    with pytest.raises(ClerkUnreachable) as excinfo:
        await router.deliver_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=operation,
            path_params={},
            query={},
        )
    assert not issubclass(excinfo.type, ClerkIdentityMismatch)
    assert "refresh and retry" not in (excinfo.value.next_step or "")
    assert "Lane delivery failed for" in caplog.text
    assert "not a DeliveryResult" in caplog.text


async def test_a_delivery_contract_violation_on_a_stream_is_reported_as_identity_mismatch(
    control_dir: Path, clock: FrozenClock, fleet_service, caplog: pytest.LogCaptureFixture
) -> None:
    """#2119 scoped the fix to ``deliver_read``; ``stream_read`` folds
    ``DeliveryContractViolation`` into the same ``except`` clause as
    ``DeliveryIdentityMismatch`` and stays "Acceptable" because its
    ``ClerkIdentityMismatch`` never carries ``next_step`` at all (so there is
    no misleading "refresh and retry" for the operator to see either way).

    Nothing else in the suite calls ``LaneRouter.stream_read()`` with a
    ``DeliveryContractViolation`` -- an independent review ran the full
    ``tests/broker/fleet`` suite (279 tests) with the widened except tuple
    reverted to ``except DeliveryIdentityMismatch`` and got 279 passed, 0
    failed, unchanged. Without this test, dropping
    ``DeliveryContractViolation`` from that tuple would silently flip a
    handler-shape bug's operator-visible outcome from ``ClerkIdentityMismatch``
    (409, no ``next_step``) to ``ClerkUnreachable`` (503, a different
    ``next_step``) with no red test anywhere.
    """
    import logging

    from app.broker.fleet.delivery import DeliveryContractViolation
    from app.broker.fleet.errors import ClerkIdentityMismatch, ClerkUnreachable
    from app.broker.fleet.routing import LaneRouter
    from tests.broker.fleet.conftest import bind_lane, fake_alpha, fake_beta, provision_lane

    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": fake_alpha(), "fake_beta": fake_beta()},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="stream-contract-violation", tmp_path=control_dir.parent
    )
    bind_lane(service, lane, account="acct-stream-contract-violation")

    class _ContractViolatingStreamDelivery:
        async def stream(self, request: object) -> None:
            del request
            raise DeliveryContractViolation(
                "the in-process handler returned str, not a StreamDeliveryResult"
            )

    router = LaneRouter(
        service=service, delivery_for=lambda broker, session: _ContractViolatingStreamDelivery()
    )
    operation = next(
        op for op in fake_alpha().operations() if op.operation_id == "account_read"
    )

    caplog.set_level(logging.WARNING, logger="app.broker.fleet.routing")
    with pytest.raises(ClerkIdentityMismatch) as excinfo:
        await router.stream_read(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation=operation,
            path_params={},
            query={},
        )
    assert not issubclass(excinfo.type, ClerkUnreachable)
    assert excinfo.value.next_step is None
    assert "next_step" not in excinfo.value.detail()
    assert "Lane stream failed identity verification" in caplog.text
    assert "not a StreamDeliveryResult" in caplog.text


def test_the_two_fakes_canonicalize_the_same_raw_account_differently() -> None:
    """The extension boundary is only provable when the fakes disagree.

    Two adapters that canonicalize identically cannot distinguish a
    provider-qualified key from a globally unique one — the exact bug
    provider-qualified assignment exists to prevent.
    """
    from tests.broker.fleet.conftest import fake_alpha, fake_beta

    raw = "  Acct-XYZ "
    assert fake_alpha().canonical_account_id(raw) == "ACCT-XYZ"
    assert fake_beta().canonical_account_id(raw) == "acct_xyz"
    # Not merely case: a casefold cannot collapse them back together.
    assert (
        fake_alpha().canonical_account_id(raw).casefold()
        != fake_beta().canonical_account_id(raw).casefold()
    )
    # Both still refuse the empty identity, so the service's gate stays reachable.
    assert fake_alpha().canonical_account_id("   ") == ""
    assert fake_beta().canonical_account_id("   ") == ""


def test_a_configuration_operation_routes_on_an_unbound_lane_of_the_declaring_provider(
    control_dir: Path, fleet_service
) -> None:
    """Readiness is per-provider: only beta declares a configuration-access
    operation, and it stays routable before any binding is confirmed."""
    from app.broker.fleet.errors import ClerkUnreachable
    from app.broker.fleet.provider import OperationReadiness

    beta = provision_lane(fleet_service, broker="fake_beta", label="cfg", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=beta.clerk_id, worker_key=beta.worker_key
    )
    fleet_service.require_capability(
        broker="fake_beta", capability=Capability.CONFIGURATION_MANAGE
    )
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(
            broker="fake_alpha", capability=Capability.CONFIGURATION_MANAGE
        )
    # Unbound, so execution refuses…
    with pytest.raises(ClerkUnreachable):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=beta.clerk_id)
    # …but the configuration surface stays reachable (the repair path), and
    # the readiness that gets it there is read off the declared operation
    # itself, not hand-passed.
    configuration_apply = next(
        operation
        for operation in fleet_service.adapters()["fake_beta"].operations()
        if operation.operation_id == "configuration_apply"
    )
    assert configuration_apply.readiness is OperationReadiness.CONFIGURATION_ACCESS
    _clerk, session, assignment = fleet_service.resolve_route(
        broker="fake_beta", clerk_id=beta.clerk_id, readiness=configuration_apply.readiness
    )
    assert assignment is None
    assert session.clerk_id == beta.clerk_id
