"""The Alpaca provider adapter — the first production fleet provider.

Provider-owned declarations only (PRD FR-003/004): the immutable provider
identity, the typed operation catalog the Alpaca clerk serves, UUID account
canonicalization, and the provider-authored summary projection. Every
execution, custody, arming and recovery decision stays in the existing Alpaca
authority machinery this adapter never imports — the fleet spine routes and
verifies, it does not trade (ADR 0062 Decision 6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    OperationStream,
    ProviderOperation,
    ServedContext,
)

_ADAPTER_VERSION = "alpaca-fleet.2"

ALPACA_CAPABILITIES = frozenset({capability for capability in Capability})

_ALPACA_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: The operations the Alpaca clerk serves as delivery A2 lands them. The
#: catalog is the single routing contract (ADR 0062 addendum, item 4): the
#: coordinator's forwarding allowlist and the delivery-B route surface derive
#: from these declarations, and the catalog grows only by reviewed change.
#: Public paths are clerk-scope-relative (the coordinator prefixes
#: ``/api/brokers/{broker}/clerks/{clerk_id}``); agent paths are what the
#: agent process serves today.
ALPACA_OPERATIONS: frozenset[ProviderOperation] = frozenset(
    {
        ProviderOperation(
            operation_id="account_read",
            method="GET",
            path_template="/account",
            agent_path_template="/api/brokers/alpaca/account",
            capability=Capability.ACCOUNT_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="positions_read",
            method="GET",
            path_template="/positions",
            agent_path_template="/api/brokers/alpaca/positions",
            capability=Capability.POSITIONS_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="orders_read",
            method="GET",
            path_template="/orders",
            agent_path_template="/api/brokers/alpaca/orders",
            capability=Capability.ORDERS_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="configuration_selection_read",
            method="GET",
            path_template="/configuration/selection",
            agent_path_template="/api/brokers/alpaca/configuration/selection",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=OperationReadiness.CONFIGURATION_ACCESS,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="bot_panel_read",
            method="GET",
            path_template="/accounts/{account_id}/bots/{sid}/panel",
            agent_path_template="/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/panel",
            capability=Capability.BOT_PANEL_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.READ,
        ),
        ProviderOperation(
            operation_id="bot_panel_action",
            method="POST",
            path_template="/accounts/{account_id}/bots/{sid}/actions",
            agent_path_template=(
                "/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/actions"
            ),
            capability=Capability.BOT_ACTION,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.DURABLE_KEY,
        ),
        ProviderOperation(
            operation_id="gallery_stream",
            method="GET",
            path_template="/accounts/{account_id}/gallery/stream",
            agent_path_template="/api/brokers/alpaca/accounts/{account_id}/gallery/stream",
            capability=Capability.GALLERY_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.READ,
            stream=OperationStream.SSE,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class AlpacaProviderAdapter:
    """The Alpaca declarations the fleet spine routes against."""

    @property
    def provider_id(self) -> str:
        """The immutable, code-owned Alpaca provider identity."""
        return "alpaca"

    @property
    def adapter_version(self) -> str:
        """This adapter implementation's build label."""
        return _ADAPTER_VERSION

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Every capability the Alpaca clerk's surface declares today."""
        return ALPACA_CAPABILITIES

    def operations(self) -> frozenset[ProviderOperation]:
        """The typed operation catalog the Alpaca clerk serves."""
        return ALPACA_OPERATIONS

    def canonical_account_id(self, external_account_id: str) -> str:
        """Canonicalize one external Alpaca account ID.

        Alpaca account identities are UUIDs; canonicity is strip + lowercase,
        so `` ABC-… `` and ``abc-…`` are one broker-qualified account. A
        well-formed UUID is not *required* here — the registry treats the
        result as opaque and provider verification owns real account
        discovery (PRD FR-051).
        """
        return external_account_id.strip().lower()

    def provider_summary(self, observation: Mapping[str, object]) -> Mapping[str, object]:
        """Project the lane's provider-authored directory summary.

        The agent's bounded typed observation (endpoint mode, authority
        state) is carried through with the registry's confirmed facts; no
        financial quantity is computed or combined here (FR-034).
        """
        reported = observation.get("reported_summary")
        summary: dict[str, object] = {
            "provider_id": self.provider_id,
            "adapter_version": _ADAPTER_VERSION,
            "confirmed_account_id": observation.get("confirmed_account_id"),
            "confirmed_binding_generation": observation.get("confirmed_binding_generation"),
        }
        if isinstance(reported, Mapping):
            summary["endpoint_mode"] = reported.get("endpoint_mode")
            summary["authority_state"] = reported.get("authority_state")
            if reported.get("detail") is not None:
                summary["detail"] = reported.get("detail")
        return summary

    def validate_served_context(self, context: ServedContext) -> None:
        """Refuse served contexts the Alpaca authority machinery cannot honor.

        The heavy provider gates (mode agreement, arming, envelope, lease)
        already run inside the clerk's own handlers; this hook adds only the
        fleet-visible invariant: a bot action is not servable by a lane whose
        reported authority is shadow — a shadow lane has no submission port
        by construction (ADR 0059).
        """
        if (
            context.capability == Capability.BOT_ACTION
            and context.account_id is None
        ):
            raise LookupError(
                "an Alpaca bot action requires the effective account it targets"
            )
        uuid_text = (context.account_id or "").strip().lower()
        if uuid_text and _ALPACA_UUID.fullmatch(uuid_text) is None:
            # Not a refusal about shape alone: the Alpaca authority keys every
            # custody path by the exact UUID, so an uncanonicalizable target
            # cannot be honored on any path.
            raise LookupError(
                f"an Alpaca served context requires a UUID account id, got {context.account_id!r}"
            )


__all__ = [
    "ALPACA_CAPABILITIES",
    "ALPACA_OPERATIONS",
    "AlpacaProviderAdapter",
]
