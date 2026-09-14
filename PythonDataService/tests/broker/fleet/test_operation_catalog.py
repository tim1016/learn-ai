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
            frozenset({_operation(method="PUT")})
        )
    # Delivery B: a POST may be a query over a body (plan and diagnostic
    # shapes) — read idempotency on POST is legal; PUT/PATCH/DELETE stay
    # refused above.
    validate_operation_catalog(frozenset({_operation(method="POST")}))
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
    # The guard that stops the two fakes' canonicalization from re-collapsing.
    assert fake_alpha().canonical_account_id("acct-1") != fake_beta().canonical_account_id("acct-1")


def test_alpaca_catalog_covers_every_canonical_desk_read() -> None:
    """C's desk has no operational fallback to a broker-global route."""
    from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter

    operations = {
        operation.operation_id: operation
        for operation in AlpacaProviderAdapter().operations()
    }
    expected = {
        "activities_read": ("GET", "/activities"),
        "portfolio_history_read": ("GET", "/portfolio-history"),
        "portfolio_history_proof_read": ("GET", "/portfolio-history-proof"),
        "clerk_status_read": ("GET", "/clerk/status"),
        "custody_diagnosis_read": ("GET", "/clerk/custody-diagnosis"),
        "bot_run_current_read": (
            "GET",
            "/accounts/{account_id}/bots/{sid}/runs/current",
        ),
        "bot_run_history_read": (
            "GET",
            "/accounts/{account_id}/bots/{sid}/runs/history",
        ),
    }
    assert {
        operation_id: (operations[operation_id].method, operations[operation_id].path_template)
        for operation_id in expected
    } == expected


def test_protocol_compatibility_refuses_mismatched_builds() -> None:
    """An agent's protocol version must equal the coordinator's, exactly —
    and an unversioned caller is exactly the older build the fence exists for."""
    require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(reported=None)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION + 1)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(reported=FLEET_PROTOCOL_VERSION - 1)
    with pytest.raises(FleetProtocolIncompatible):
        require_protocol_compatible(
            reported=FLEET_PROTOCOL_VERSION,
            coordinator_version=FLEET_PROTOCOL_VERSION + 1,
        )


def test_catalog_validation_refuses_ambiguous_and_incoherent_declarations() -> None:
    """Parameter-normalized route uniqueness, public/agent parameter
    agreement, and the configuration/account combination all refuse."""
    with pytest.raises(ValueError, match="declared twice"):
        validate_operation_catalog(
            frozenset(
                {
                    _operation(
                        operation_id="bots_a",
                        path_template="/bots/{sid}",
                        agent_path_template="/api/provider/bots/{sid}",
                    ),
                    _operation(
                        operation_id="bots_b",
                        path_template="/bots/{bot_id}",
                        agent_path_template="/api/provider/bots/{bot_id}",
                    ),
                }
            )
        )
    with pytest.raises(ValueError, match="cannot populate both"):
        validate_operation_catalog(
            frozenset(
                {
                    _operation(
                        operation_id="bot_action",
                        method="POST",
                        path_template="/bots/{sid}/actions",
                        agent_path_template="/api/provider/bots/{bot_id}/actions",
                        capability=Capability.BOT_ACTION,
                        requires_effective_account=True,
                        idempotency=OperationIdempotency.DURABLE_KEY,
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="cannot require an effective account"):
        validate_operation_catalog(
            frozenset(
                {
                    _operation(
                        operation_id="config_apply",
                        method="POST",
                        path_template="/configuration/apply",
                        agent_path_template="/api/provider/configuration/apply",
                        capability=Capability.CONFIGURATION_MANAGE,
                        readiness=OperationReadiness.CONFIGURATION_ACCESS,
                        requires_effective_account=True,
                        idempotency=OperationIdempotency.ONE_SHOT,
                    )
                }
            )
        )
