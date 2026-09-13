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
    ClerkIdentityMismatch,
    ClerkNotFound,
    ClerkRoutingOutcomeUnknown,
    ClerkUnreachable,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeCloneDetected,
    ClerkVolumeIdentityMismatch,
    ClerkVolumeIdentityMissing,
    ClerkVolumeMountUnproven,
    FleetControlError,
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
    FleetRegistryUnavailable,
]


def test_every_minted_identity_is_unique_and_nonsemantic() -> None:
    clerk_ids = {identity.new_clerk_id() for _ in range(500)}
    assert len(clerk_ids) == 500
    for value in clerk_ids:
        # Format-only parsing: nothing about a clerk can be learned from it.
        assert re.fullmatch(r"clrk_[0-9a-f]{24}", value)
        assert not value.startswith("clrk_account")
        assert identity.is_clerk_id(value)


def test_identity_validation_rejects_forged_and_wrong_family_values() -> None:
    assert not identity.is_clerk_id("alpaca-paper")
    assert not identity.is_clerk_id("clrk_ZZZZ")
    assert not identity.is_clerk_id(identity.new_volume_id())
    assert not identity.is_clerk_id("")
    assert not identity.is_clerk_id(42)
    assert not identity.is_worker_key(identity.new_clerk_id())
    assert identity.is_agent_instance_id(identity.new_agent_instance_id())
    assert identity.is_correlation_id(identity.new_correlation_id())


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_every_refusal_family_pins_reason_status_and_detail(family: type[FleetControlError]) -> None:
    error = family("message", next_step="step")
    detail = error.detail()
    assert detail["reason"] == family.reason
    assert detail["message"] == "message"
    assert detail["next_step"] == "step"
    assert family.status_code in (400, 404, 409, 503)
    # The reason is snake_case and stable: it is a wire contract, not prose.
    assert re.fullmatch(r"[a-z0-9_]+", family.reason)
    # No two families share a reason — a rendered label must be unambiguous.
    reasons = [f.reason for f in ALL_FAMILIES]
    assert len(reasons) == len(set(reasons))


def test_status_codes_pin_the_retry_semantics() -> None:
    terminal = {BrokerNotSupported.status_code, ClerkNotFound.status_code}
    conflict = {ClerkBrokerMismatch.status_code}
    retry_safe = {ClerkUnreachable.status_code, ClerkRoutingOutcomeUnknown.status_code}
    assert terminal == {404}
    assert conflict == {409}
    assert retry_safe == {503}
    assert BrokerAndClerkRequired.status_code == 400
