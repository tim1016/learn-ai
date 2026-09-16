"""The Alpaca provider adapter — the first production fleet provider.

Provider-owned declarations only (PRD FR-003/004): the immutable provider
identity, the typed operation catalog the Alpaca clerk serves, account ID
canonicalization, and the provider-authored summary projection. Every
execution, custody, arming and recovery decision stays in the existing Alpaca
authority machinery this adapter never imports — the fleet spine routes and
verifies, it does not trade (ADR 0062 Decision 6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id
from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    OperationStream,
    ProviderOperation,
    ServedContext,
)

_ADAPTER_VERSION = "alpaca-fleet.5"


def _op(
    operation_id: str,
    method: str,
    path: str,
    *,
    capability: Capability,
    idempotency: OperationIdempotency = OperationIdempotency.READ,
    readiness: OperationReadiness = OperationReadiness.EXECUTION,
    account: bool = False,
    stream: OperationStream = OperationStream.NONE,
    agent_path: str | None = None,
) -> ProviderOperation:
    """Declare one operation; agent paths default to the Alpaca prefix.

    The default mirrors the dominant shape — the agent serves the public
    clerk-relative path under ``/api/brokers/alpaca`` — and the custody family
    names its agent path explicitly because its public home
    (``…/custody/…``) is new while the agent still serves
    ``/api/alpaca-clerk-sqlite/…`` and ``/api/accounts/…``.
    """
    return ProviderOperation(
        operation_id=operation_id,
        method=method,
        path_template=path,
        agent_path_template=agent_path or f"/api/brokers/alpaca{path}",
        capability=capability,
        readiness=readiness,
        requires_effective_account=account,
        idempotency=idempotency,
        stream=stream,
    )


_CONFIGURATION = OperationReadiness.CONFIGURATION_ACCESS
_EXECUTE = OperationReadiness.EXECUTION
_READ = OperationIdempotency.READ
_DURABLE = OperationIdempotency.DURABLE_KEY
_ONE_SHOT = OperationIdempotency.ONE_SHOT

#: The complete Alpaca operation catalog (delivery B, PRD §10.2). The catalog
#: is the single routing contract (ADR 0062 addendum, item 4): the
#: coordinator's forwarding allowlist, the public clerk-scoped routes, the
#: exported contract and the frontend builders all derive from these
#: declarations, and the catalog grows only by reviewed change. Public paths
#: are clerk-scope-relative (the coordinator prefixes
#: ``/api/brokers/{broker}/clerks/{clerk_id}``); agent paths are what the
#: agent process serves today. See docs/design/fleet-b-route-inventory.md for
#: the per-operation dispositions and the retained-legacy surface.
ALPACA_OPERATIONS: frozenset[ProviderOperation] = frozenset(
    {
        # ── Lane reads ────────────────────────────────────────────────────
        _op("account_read", "GET", "/account", capability=Capability.ACCOUNT_READ),
        _op("positions_read", "GET", "/positions", capability=Capability.POSITIONS_READ),
        _op("orders_read", "GET", "/orders", capability=Capability.ORDERS_READ),
        # The desk's account evidence is lane-scoped even where the
        # pre-fleet clerk handler still exposes its compatibility alias under
        # ``/api/brokers/alpaca``.  These declarations are the one migration
        # seam: the coordinator pins broker, clerk, epoch and binding before
        # forwarding to that established handler.
        _op(
            "activities_read",
            "GET",
            "/activities",
            capability=Capability.ACCOUNT_READ,
            agent_path="/api/brokers/alpaca/activities",
        ),
        _op(
            "portfolio_history_read",
            "GET",
            "/portfolio-history",
            capability=Capability.ACCOUNT_READ,
            agent_path="/api/brokers/alpaca/portfolio-history",
        ),
        _op(
            "portfolio_history_proof_read",
            "GET",
            "/portfolio-history-proof",
            capability=Capability.ACCOUNT_READ,
            agent_path="/api/brokers/alpaca/portfolio-history-proof",
        ),
        _op(
            "clerk_status_read",
            "GET",
            "/clerk/status",
            capability=Capability.CUSTODY_READ,
            agent_path="/api/brokers/alpaca/clerk/status",
        ),
        _op(
            "custody_diagnosis_read",
            "GET",
            "/clerk/custody-diagnosis",
            capability=Capability.CUSTODY_READ,
            agent_path="/api/brokers/alpaca/clerk/custody-diagnosis",
        ),
        # Lane-scoped verdict (#2140): the coordinator has no clerk runtime of
        # its own to answer this from, so it must be routed to a named lane,
        # never answered in-process. Declared at CONFIGURATION_ACCESS, not
        # EXECUTION -- see resolve_route's docstring (app/broker/fleet/
        # service.py). A lane that is up but refusing (an unactivated paper
        # lane returning ACTIVATION_REQUIRED) must stay readable so the
        # operator can see exactly that refusal reason; EXECUTION readiness
        # would refuse the route itself before the refusal reason was ever
        # reached.
        _op(
            "live_verdict",
            "GET",
            "/live-verdict",
            capability=Capability.CUSTODY_READ,
            readiness=_CONFIGURATION,
            agent_path="/api/brokers/alpaca/live-verdict",
        ),
        _op(
            "market_status_read",
            "GET",
            "/market-status-snapshot",
            capability=Capability.MARKET_STATUS_READ,
        ),
        # ── Configuration family (repair stays reachable without a binding) ──
        _op(
            "configuration_desk_state",
            "GET",
            "/configuration/desk-state",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_owner_read",
            "GET",
            "/configuration/owner",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_owner_update",
            "PATCH",
            "/configuration/owner",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_credential_slots",
            "GET",
            "/configuration/credential-slots",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_nicknames_read",
            "GET",
            "/configuration/account-nicknames",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_nicknames_set",
            "PUT",
            "/configuration/account-nicknames/{account_id}",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_profiles_list",
            "GET",
            "/configuration/profiles",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_profile_create",
            "POST",
            "/configuration/profiles",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_profile_read",
            "GET",
            "/configuration/profiles/{profile_id}",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_profile_update",
            "PATCH",
            "/configuration/profiles/{profile_id}",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_profile_clone",
            "POST",
            "/configuration/profiles/{profile_id}/clone",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_revisions_list",
            "GET",
            "/configuration/profiles/{profile_id}/revisions",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_revision_create",
            "POST",
            "/configuration/profiles/{profile_id}/revisions",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_revision_read",
            "GET",
            "/configuration/profiles/{profile_id}/revisions/{revision}",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_account_pin",
            "POST",
            "/configuration/profiles/{profile_id}/revisions/{revision}/account-pin",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_verify_account",
            "POST",
            "/configuration/profiles/{profile_id}/revisions/{revision}/verify-account",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_selection_read",
            "GET",
            "/configuration/selection",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_selection_write",
            "PUT",
            "/configuration/selection",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_selection_apply",
            "POST",
            "/configuration/selection/apply",
            capability=Capability.CONFIGURATION_MANAGE,
            idempotency=_ONE_SHOT,
            readiness=_CONFIGURATION,
        ),
        _op(
            "configuration_events",
            "GET",
            "/configuration/events",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=_CONFIGURATION,
        ),
        # ── Bot panel family (account-scoped execution) ──────────────────
        _op(
            "bots_catalog_read",
            "GET",
            "/accounts/{account_id}/bots/catalog",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_create",
            "POST",
            "/accounts/{account_id}/bots",
            capability=Capability.BOT_ACTION,
            idempotency=_ONE_SHOT,
            account=True,
        ),
        _op(
            "bot_admission_plan",
            "POST",
            "/accounts/{account_id}/bots/admission",
            capability=Capability.DEPLOY,
            account=True,
        ),
        _op(
            "bot_cohort_archive_read",
            "GET",
            "/accounts/{account_id}/bots/cohort-archive",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_cohort_archive",
            "POST",
            "/accounts/{account_id}/bots/cohort-archive",
            capability=Capability.BOT_ACTION,
            idempotency=_DURABLE,
            account=True,
        ),
        _op(
            "bot_cohort_flatten_read",
            "GET",
            "/accounts/{account_id}/bots/cohort-flatten",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_cohort_flatten",
            "POST",
            "/accounts/{account_id}/bots/cohort-flatten",
            capability=Capability.BOT_ACTION,
            idempotency=_DURABLE,
            account=True,
        ),
        _op(
            "bots_deploy_read",
            "GET",
            "/accounts/{account_id}/bots/deploy",
            capability=Capability.DEPLOY,
            account=True,
        ),
        _op(
            "bot_panel_read",
            "GET",
            "/accounts/{account_id}/bots/{sid}/panel",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_panel_action",
            "POST",
            "/accounts/{account_id}/bots/{sid}/actions",
            capability=Capability.BOT_ACTION,
            idempotency=_DURABLE,
            account=True,
        ),
        _op(
            "bot_authority_facts",
            "GET",
            "/accounts/{account_id}/bots/{sid}/authority-facts",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_chart_history",
            "GET",
            "/accounts/{account_id}/bots/{sid}/chart/history",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_chart_live",
            "GET",
            "/accounts/{account_id}/bots/{sid}/chart/live",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_evidence",
            "GET",
            "/accounts/{account_id}/bots/{sid}/evidence",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_live_snapshot",
            "GET",
            "/accounts/{account_id}/bots/{sid}/live-snapshot",
            capability=Capability.BOT_PANEL_READ,
            account=True,
        ),
        _op(
            "bot_live_stream",
            "GET",
            "/accounts/{account_id}/bots/{sid}/live-stream",
            capability=Capability.BOT_PANEL_READ,
            account=True,
            stream=OperationStream.SSE,
        ),
        _op(
            "bot_run_current_read",
            "GET",
            "/accounts/{account_id}/bots/{sid}/runs/current",
            capability=Capability.BOT_PANEL_READ,
            account=True,
            agent_path="/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/current",
        ),
        _op(
            "bot_run_history_read",
            "GET",
            "/accounts/{account_id}/bots/{sid}/runs/history",
            capability=Capability.BOT_PANEL_READ,
            account=True,
            agent_path="/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/history",
        ),
        _op(
            "paper_access_plan",
            "POST",
            "/accounts/{account_id}/strategies/{program_key}/paper-access/plan",
            capability=Capability.DEPLOY,
            account=True,
        ),
        _op(
            "paper_access_confirm",
            "POST",
            "/accounts/{account_id}/strategies/{program_key}/paper-access/confirm",
            capability=Capability.DEPLOY,
            idempotency=_DURABLE,
            account=True,
        ),
        # ── Gallery family ────────────────────────────────────────────────
        _op(
            "gallery_snapshot",
            "GET",
            "/accounts/{account_id}/gallery/snapshot",
            capability=Capability.GALLERY_READ,
            account=True,
        ),
        _op(
            "gallery_stream",
            "GET",
            "/accounts/{account_id}/gallery/stream",
            capability=Capability.GALLERY_READ,
            account=True,
            stream=OperationStream.SSE,
        ),
        # ── Custody family: new public home, agent serves the legacy paths ──
        _op(
            "custody_account_snapshot",
            "GET",
            "/accounts/{account_id}/custody/snapshot",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path="/api/alpaca-clerk-sqlite/accounts/{account_id}/snapshot",
        ),
        _op(
            "custody_account_timeline",
            "GET",
            "/accounts/{account_id}/custody/timeline",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path="/api/alpaca-clerk-sqlite/accounts/{account_id}/timeline",
        ),
        _op(
            "custody_bot_snapshot",
            "GET",
            "/accounts/{account_id}/custody/bots/{sid}/snapshot",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/snapshot"
            ),
        ),
        _op(
            "custody_bot_timeline",
            "GET",
            "/accounts/{account_id}/custody/bots/{sid}/timeline",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/timeline"
            ),
        ),
        _op(
            "custody_command_read",
            "GET",
            "/accounts/{account_id}/custody/commands/{command_id}",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/commands/{command_id}"
            ),
        ),
        _op(
            "custody_runs_start",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/runs/start",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/runs/start"
            ),
        ),
        _op(
            "custody_runs_stop",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/runs/stop",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/runs/stop"
            ),
        ),
        _op(
            "custody_reconcile",
            "POST",
            "/accounts/{account_id}/custody/reconcile",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path="/api/alpaca-clerk-sqlite/accounts/{account_id}/reconcile",
        ),
        _op(
            "custody_recovery_check",
            "POST",
            "/accounts/{account_id}/custody/recovery-actions/check",
            capability=Capability.CUSTODY_COMMAND,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/recovery-actions/check"
            ),
        ),
        _op(
            "custody_recovery_execute",
            "POST",
            "/accounts/{account_id}/custody/recovery-actions/execute",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/recovery-actions/execute"
            ),
        ),
        _op(
            "custody_bot_recovery_check",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/recovery-actions/check",
            capability=Capability.CUSTODY_COMMAND,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/"
                "recovery-actions/check"
            ),
        ),
        _op(
            "custody_bot_recovery_execute",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/recovery-actions/execute",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/"
                "recovery-actions/execute"
            ),
        ),
        _op(
            "custody_historical_recovery_prepare",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/historical-execution-recovery/prepare",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/"
                "historical-execution-recovery/prepare"
            ),
        ),
        _op(
            "custody_historical_recovery_confirm",
            "POST",
            "/accounts/{account_id}/custody/bots/{sid}/historical-execution-recovery/confirm",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{sid}/"
                "historical-execution-recovery/confirm"
            ),
        ),
        _op(
            "custody_transactions",
            "GET",
            "/accounts/{account_id}/custody/transactions",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path="/api/accounts/{account_id}/transactions",
        ),
        _op(
            "custody_transaction_read",
            "GET",
            "/accounts/{account_id}/custody/transactions/{transaction_id}",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path="/api/accounts/{account_id}/transactions/{transaction_id}",
        ),
        _op(
            "custody_external_order_ack",
            "POST",
            "/accounts/{account_id}/custody/transactions/external-orders/"
            "{external_order_id}/acknowledge",
            capability=Capability.CUSTODY_COMMAND,
            idempotency=_DURABLE,
            account=True,
            agent_path=(
                "/api/accounts/{account_id}/transactions/external-orders/"
                "{external_order_id}/acknowledge"
            ),
        ),
        _op(
            "custody_pnl_attribution",
            "GET",
            "/accounts/{account_id}/custody/pnl-attribution",
            capability=Capability.CUSTODY_READ,
            account=True,
            agent_path="/api/accounts/{account_id}/pnl-attribution",
        ),
        # ── Manual orders family ──────────────────────────────────────────
        _op(
            "manual_orders_capability",
            "GET",
            "/accounts/{account_id}/manual-orders/capability",
            capability=Capability.MANUAL_ORDERS,
            account=True,
        ),
        _op(
            "manual_orders_preview",
            "POST",
            "/accounts/{account_id}/manual-orders/preview",
            capability=Capability.MANUAL_ORDERS,
            account=True,
        ),
        _op(
            "manual_order_ticket_read",
            "GET",
            "/accounts/{account_id}/manual-order-tickets/{ticket_id}",
            capability=Capability.MANUAL_ORDERS,
            account=True,
        ),
        _op(
            "manual_order_ticket_put",
            "PUT",
            "/accounts/{account_id}/manual-order-tickets/{ticket_id}",
            capability=Capability.MANUAL_ORDERS,
            idempotency=_ONE_SHOT,
            account=True,
        ),
        _op(
            "manual_order_ticket_cancel",
            "POST",
            "/accounts/{account_id}/manual-order-tickets/{ticket_id}/cancel",
            capability=Capability.MANUAL_ORDERS,
            idempotency=_DURABLE,
            account=True,
        ),
        _op(
            "manual_order_ticket_continue",
            "POST",
            "/accounts/{account_id}/manual-order-tickets/{ticket_id}/continue",
            capability=Capability.MANUAL_ORDERS,
            idempotency=_DURABLE,
            account=True,
        ),
        _op(
            "manual_order_cancel",
            "POST",
            "/accounts/{account_id}/manual-orders/{order_ref:path}/cancel",
            capability=Capability.MANUAL_ORDERS,
            idempotency=_DURABLE,
            account=True,
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
        """Exactly the capabilities the operation catalog backs today.

        Advertised equals served: a capability without a declared operation
        is evidence of nothing, and the directory must not promise behavior
        no route delivers. The set grows as delivery B's operation families
        land.
        """
        return frozenset(operation.capability for operation in ALPACA_OPERATIONS)

    def operations(self) -> frozenset[ProviderOperation]:
        """The typed operation catalog the Alpaca clerk serves."""
        return ALPACA_OPERATIONS

    def canonical_account_id(self, external_account_id: str) -> str:
        """Canonicalize one external Alpaca account ID.

        The Alpaca account identity this system binds is the account number
        the clerk reports (``app/broker/alpaca/adapter.py``'s ``account_id``,
        set from ``account_number``), not a UUID; canonicity is strip +
        lowercase, so `` ABC123 `` and ``abc123`` are one broker-qualified
        account. No shape is required or checked here — the registry treats
        the result as opaque and provider verification owns real account
        discovery (PRD FR-051).
        """
        return canonical_alpaca_account_id(external_account_id)

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
            "confirmed_by_current_session": observation.get("confirmed_by_current_session"),
            "multiple_effective_assignments": observation.get("multiple_effective_assignments"),
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
        already run inside the clerk's own handlers; this hook adds the one
        fleet-visible invariant the generic layer cannot check: a bot action
        names the effective account it targets.

        In production this invariant is unreachable from the routing seam
        today: `LaneRouter._resolve`, the only caller, builds every
        `ServedContext` from a resolved assignment whose
        `canonical_external_account_id` is a non-optional `str`, so
        `context.account_id` is never `None` there. The check is kept anyway
        as the contract for the generic fleet layer, and for hand-built
        contexts in tests — this adapter declares no refusal the seam can
        currently trigger.
        """
        if (
            context.capability == Capability.BOT_ACTION
            and context.account_id is None
        ):
            raise LookupError(
                "an Alpaca bot action requires the effective account it targets"
            )


__all__ = [
    "ALPACA_OPERATIONS",
    "AlpacaProviderAdapter",
]
