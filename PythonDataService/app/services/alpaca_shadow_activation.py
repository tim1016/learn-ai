"""Configuration-owned activation of an isolated Shadow account, never Live."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

from app.broker.alpaca.active_binding import get_active_alpaca_binding
from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id, shadow_account_id_for_live_account
from app.broker.alpaca.clerk.active_authority import activate_shadow_clerk_authority, get_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.money import normalize_money
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid
from app.broker.alpaca.clerk.shadow_broker import (
    ShadowNamespacePoisoned,
    ShadowNamespaceUnproven,
    verify_shadow_namespace_empty,
)
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid, ActivationStore
from app.broker.alpaca.clerk.sqlite.budget_authority import authority_review_token, commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection
from app.broker.contract.errors import BrokerError
from app.broker.contract.registry import get_broker_registry
from app.broker.ibkr.config import live_artifacts_root
from app.broker_configuration.runtime import get_broker_configuration_service
from app.config import fleet_settings, settings
from app.schemas.alpaca_live_graduation import LiveGraduationStatus, ShadowActivationOutcome
from app.services.alpaca_live_graduation_gate import graduation_mutation_fence


@dataclass
class ShadowActivationRefused(RuntimeError):
    message: str
    next_action: str = "Reload Settings and resolve the account evidence before activating Shadow."
    reason: str = "shadow_activation_refused"

    def __str__(self) -> str:
        return self.message


def _inactive_account(account_id: str, runtime: ActiveClerkRuntime | None) -> str:
    binding = get_active_alpaca_binding()
    failure = None if runtime is None else runtime.startup_failure
    if binding is None or binding.settings.mode != "live" or binding.account_pin is None:
        raise ShadowActivationRefused("Apply a verified Live profile with this account pinned first.")
    served = binding.account_pin
    if canonical_alpaca_account_id(served) != canonical_alpaca_account_id(account_id):
        raise ShadowActivationRefused("This worker is bound to a different account.")
    if failure is None or failure.reason_code != "SHADOW_ACTIVATION_REQUIRED" or failure.account_id != shadow_account_id_for_live_account(served):
        raise ShadowActivationRefused("This account is not awaiting its first Shadow activation.")
    if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL or fleet_settings.WORKER_SERVICE is None:
        raise ShadowActivationRefused("Authenticated control and a supervised restart are required for account activation.")
    root = live_artifacts_root()
    if ActivationStore(root / "accounts" / "alpaca").latest(served) is not None:
        raise ShadowActivationRefused("Live graduation is already recorded. Shadow cannot be reactivated.")
    return served


def inactive_shadow_status(account_id: str, runtime: ActiveClerkRuntime) -> LiveGraduationStatus | None:
    failure = runtime.startup_failure
    if failure is None or failure.reason_code != "SHADOW_ACTIVATION_REQUIRED":
        return None
    try:
        served = _inactive_account(account_id, runtime)
    except (ShadowActivationRefused, ActivationRecordInvalid, OSError, ValueError) as exc:
        return LiveGraduationStatus(account_id=account_id, configured_mode="live", authority="unavailable", state="blocked",
            headline="Shadow activation is unavailable", detail=str(exc), next_action="Review the effective account and restore its activation evidence.",
            restart_managed=fleet_settings.WORKER_SERVICE is not None)
    return LiveGraduationStatus(account_id=served, configured_mode="live", authority="unavailable", state="activation_available",
        headline="Activate Shadow for this account", detail="Shadow simulates orders in one isolated account. It cannot submit real orders or reserve real cash.",
        next_action="Activate Shadow and restart this clerk, then review a dollar budget in Deploy.", restart_managed=True)


async def _activate_shadow_from_configuration(account_id: str) -> ShadowActivationOutcome:
    """Recheck profile, broker identity and the existing no-submit namespace fence."""
    async with graduation_mutation_fence():
        runtime = get_active_clerk_runtime()
        served = _inactive_account(account_id, runtime)
        binding = get_active_alpaca_binding()
        port = get_broker_registry().resolve("alpaca")
        try:
            account = await asyncio.wait_for(port.get_account(), timeout=20)
            if account.account_id != served or account.account_mode != "live":
                raise ShadowActivationRefused("The fresh broker account does not match this Live workspace.")
            initial_reference = normalize_money(account.cash)
            if initial_reference <= 0:
                raise ShadowActivationRefused("Positive observed cash is required to establish Shadow’s initial risk capital.")
            await asyncio.wait_for(verify_shadow_namespace_empty(port), timeout=20)
        except (BrokerError, TimeoutError, ShadowNamespacePoisoned, ShadowNamespaceUnproven) as exc:
            raise ShadowActivationRefused(str(exc), "Restore the Alpaca read connection and refresh activation evidence.") from exc
        service = get_broker_configuration_service()
        with service.selection_handover():
            if get_active_clerk_runtime() is not runtime or get_active_alpaca_binding() is not binding:
                raise ShadowActivationRefused("The account authority changed during activation review.")
            if service.selection().effective_account_id != served:
                raise ShadowActivationRefused("The effective account selection changed during activation review.")
            _inactive_account(account_id, runtime)
            root = live_artifacts_root()
            record = await activate_shadow_clerk_authority(live_account_id=served, artifacts_root=root)
            repo = ClerkSqliteRepository.open(account_id=record.account_id, artifacts_root=root)
            try:
                with repo.write_fence() as conn:
                    # Existing history needs the visible guarded budget upgrade.
                    # A fresh empty account can adopt budgets as part of this act.
                    history = any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("runs", "orders", "fills", "deployment_budgets"))
                    if not history:
                        SimulatedAccountProjection(repo=repo, artifacts_root=root).establish_session_baseline(
                            reference_cash=initial_reference, now_ms=repo.clock())
                        commit_budget_authority_cutover(repo, actor=service.owner().owner_id,
                            reviewed_token=authority_review_token(repo), stop_receipt=record.activation_sha256)
            finally:
                repo.close()
    return ShadowActivationOutcome(account_id=served, state="restart_scheduled", activation_sha256=record.activation_sha256,
        activated_at_ms=record.activated_at_ms,
        message="Shadow activation is recorded. The clerk will restart into isolated simulation; no strategy or real order was created.")


async def activate_shadow_from_configuration(account_id: str) -> ShadowActivationOutcome:
    try:
        return await _activate_shadow_from_configuration(account_id)
    except (ActivationRecordInvalid, ShadowActivationInvalid, BrokerError, OSError, ValueError, sqlite3.Error) as exc:
        raise ShadowActivationRefused("Shadow activation evidence could not be committed: " + str(exc)) from exc
