"""The typed operation catalog: one contract for routing, contracts and codegen.

Audit 2026-09-13, finding 6: strings cannot express method, capability,
readiness, idempotency, stream shape or account requirements. The catalog
validation here is what every adapter's conformance suite runs, so no
provider enters a registry with an ambiguous or drifting operation set.
"""

from __future__ import annotations

import pytest

from app.broker.fleet.errors import FleetProtocolIncompatible
from app.broker.fleet.provider import (
    FLEET_PROTOCOL_VERSION,
    Capability,
    OperationIdempotency,
    OperationReadiness,
    OperationStream,
    ProviderOperation,
    require_protocol_compatible,
    validate_operation_catalog,
)
from tests.broker.fleet.conftest import fake_alpha, fake_beta


def _operation(**overrides) -> ProviderOperation:
    """One well-formed operation, overridable per negative case."""
    fields = {
        "operation_id": "account_read",
        "method": "GET",
        "path_template": "/account",
        "agent_path_template": "/api/provider/account",
        "capability": Capability.ACCOUNT_READ,
        "readiness": OperationReadiness.EXECUTION,
        "requires_effective_account": False,
        "idempotency": OperationIdempotency.READ,
    }
    fields.update(overrides)
    return ProviderOperation(**fields)


def test_a_well_formed_catalog_passes_and_derives_route_keys() -> None:
    """The happy path: unique ids, unique routes, coherent method semantics."""
    catalog = frozenset(
        {
            _operation(),
            _operation(
                operation_id="bot_action",
                method="POST",
                path_template="/bots/{sid}/actions",
                agent_path_template="/api/provider/bots/{sid}/actions",
                capability=Capability.BOT_ACTION,
                requires_effective_account=True,
                idempotency=OperationIdempotency.DURABLE_KEY,
            ),
            _operation(
                operation_id="gallery_stream",
                path_template="/gallery/stream",
                agent_path_template="/api/provider/gallery/stream",
                capability=Capability.GALLERY_READ,
                stream=OperationStream.SSE,
            ),
        }
    )
    validate_operation_catalog(catalog)
    by_route = {operation.route_key(): operation for operation in catalog}
    assert by_route[("GET", "/account")].operation_id == "account_read"
    assert by_route[("POST", "/bots/{sid}/actions")].idempotency == (
        OperationIdempotency.DURABLE_KEY
    )


def test_ambiguous_or_malformed_catalogs_refuse() -> None:
    """Duplicate ids, duplicate routes and incoherent facts all refuse."""
    with pytest.raises(ValueError, match="declared twice"):
        validate_operation_catalog(
            frozenset({_operation(), _operation(path_template="/other")})
        )
    with pytest.raises(ValueError, match="declared twice"):
        validate_operation_catalog(
            frozenset(
                {
                    _operation(),
                    _operation(operation_id="also_account_read"),
                }
            )
        )
    with pytest.raises(ValueError, match="must be an absolute path"):
        validate_operation_catalog(frozenset({_operation(path_template="account")}))
    with pytest.raises(ValueError, match="repeats path parameter"):
        validate_operation_catalog(
            frozenset({_operation(path_template="/{sid}/{sid}")})
        )
    with pytest.raises(ValueError, match="cannot declare read idempotency"):
        validate_operation_catalog(
            frozenset({_operation(method="POST")})
        )
    with pytest.raises(ValueError, match="streaming operation"):
        validate_operation_catalog(
            frozenset({_operation(method="POST", idempotency=OperationIdempotency.ONE_SHOT, stream=OperationStream.SSE)})
        )
    with pytest.raises(ValueError, match="snake_case token"):
        validate_operation_catalog(frozenset({_operation(operation_id="Account Read")}))


def test_the_fake_providers_declare_valid_catalogs() -> None:
    """The conformance fakes themselves model a well-formed provider."""
    for adapter in (fake_alpha(), fake_beta()):
        validate_operation_catalog(adapter.operations())
        declared = {operation.capability for operation in adapter.operations()}
        assert declared <= adapter.capabilities
        for operation in adapter.operations():
            assert operation.agent_path_template.startswith("/api/")


def test_protocol_compatibility_refuses_mismatched_builds() -> None:
    """An agent's protocol version must equal the coordinator's, exactly."""
    require_protocol_compatible(reported=None)
    require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION + 1)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION - 1)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(
            reported=FLEET_PROTOCOL_VERSION,
            coordinator_version=FLEET_PROTOCOL_VERSION + 1,
        )
