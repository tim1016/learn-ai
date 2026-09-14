"""Opaque identity minting and the stable refusal families (PRD §10.4)."""

from __future__ import annotations

import re

import pytest

from app.broker.fleet import identity
from app.broker.fleet.errors import (
    BrokerAndClerkRequired,
    BrokerClerkCapabilityUnavailable,
    BrokerNotSupported,
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    ClerkBindingGenerationConflict,
    ClerkBrokerMismatch,
    ClerkEndpointNotApproved,
    ClerkIdentityMismatch,
    ClerkNotFound,
    ClerkRoutingAttemptConflict,
    ClerkRoutingOutcomeUnknown,
    ClerkUnreachable,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeCloneDetected,
    ClerkVolumeIdentityMismatch,
    ClerkVolumeIdentityMissing,
    ClerkVolumeMountUnproven,
    FleetControlError,
    FleetProtocolIncompatible,
    FleetRegistryUnavailable,
)

ALL_FAMILIES: list[type[FleetControlError]] = [
    BrokerAndClerkRequired,
    BrokerNotSupported,
    ClerkNotFound,
    ClerkBrokerMismatch,
    ClerkUnreachable,
    ClerkIdentityMismatch,
    ClerkVolumeIdentityMissing,
    ClerkVolumeIdentityMismatch,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeMountUnproven,
    ClerkVolumeCloneDetected,
    ClerkBindingGenerationConflict,
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    BrokerClerkCapabilityUnavailable,
    ClerkRoutingOutcomeUnknown,
    ClerkRoutingAttemptConflict,
    ClerkEndpointNotApproved,
    FleetProtocolIncompatible,
    FleetRegistryUnavailable,
]


def test_every_minted_identity_is_unique_and_nonsemantic() -> None:
    """Minted identities are unique and carry format only, never meaning."""
    clerk_ids = {identity.new_clerk_id() for _ in range(500)}
    assert len(clerk_ids) == 500
    for value in clerk_ids:
        # Format-only parsing: nothing about a clerk can be learned from it.
        assert re.fullmatch(r"clrk_[0-9a-f]{24}", value)
        assert not value.startswith("clrk_account")
        assert identity.is_clerk_id(value)


def test_identity_validation_rejects_forged_and_wrong_family_values() -> None:
    """Validation rejects forged, wrong-family and non-string values."""
    assert not identity.is_clerk_id("alpaca-paper")
    assert not identity.is_clerk_id("clrk_ZZZZ")
    assert not identity.is_clerk_id(identity.new_volume_id())
    assert not identity.is_clerk_id("")
    assert not identity.is_clerk_id(42)
    assert not identity.is_worker_key(identity.new_clerk_id())
    assert identity.is_agent_instance_id(identity.new_agent_instance_id())
    assert identity.is_correlation_id(identity.new_correlation_id())
    # The transport token family is distinct from every durable identity.
    token = identity.new_service_token()
    assert identity.is_service_token(token)
    assert not identity.is_service_token(identity.new_worker_key())
    assert not identity.is_worker_key(token)
    assert len({identity.new_service_token() for _ in range(100)}) == 100


#: The retry-semantics pin for the families whose exact status code is a
#: documented contract, not merely one of the four valid buckets (audit
#: 2026-09-13; folded from the former standalone
#: test_status_codes_pin_the_retry_semantics).
_PINNED_RETRY_SEMANTICS: dict[type[FleetControlError], int] = {
    BrokerAndClerkRequired: 400,
    BrokerNotSupported: 404,
    ClerkNotFound: 404,
    ClerkBrokerMismatch: 409,
    ClerkUnreachable: 503,
    ClerkRoutingOutcomeUnknown: 503,
}


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_every_refusal_family_pins_reason_status_and_detail(family: type[FleetControlError]) -> None:
    """Every family pins a unique snake_case reason, a status code, and its detail body."""
    error = family("message", next_step="step")
    detail = error.detail()
    assert detail["reason"] == family.reason
    assert detail["message"] == "message"
    assert detail["next_step"] == "step"
    assert family.status_code in (400, 404, 409, 503)
    if family in _PINNED_RETRY_SEMANTICS:
        assert family.status_code == _PINNED_RETRY_SEMANTICS[family]
    # The reason is snake_case and stable: it is a wire contract, not prose.
    assert re.fullmatch(r"[a-z0-9_]+", family.reason)


def test_no_two_refusal_families_share_a_reason() -> None:
    """A rendered label must be unambiguous: no two families share a reason."""
    reasons = [f.reason for f in ALL_FAMILIES]
    assert len(reasons) == len(set(reasons))
