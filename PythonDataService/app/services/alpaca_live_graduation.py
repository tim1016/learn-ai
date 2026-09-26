"""Application service for browser-driven Alpaca Live graduation.

The domain cutover remains the authority.  This service contributes only the
online orchestration the browser needs: broker-authored evidence, server-owned
paths, a verified backup, durable plan storage, and the shared bot-start fence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.active_binding import resolved_alpaca_settings
from app.broker.alpaca.clerk.account_authority import (
    canonical_alpaca_account_id,
    live_account_id_for_shadow_account,
)
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverPlan,
    CutoverRefused,
    apply_cutover,
    decode_cutover_plan,
    initialize_cutover_authority,
    plan_cutover,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    relative_reference,
)
from app.broker.alpaca.clerk.sqlite.recovery import (
    RecoveryRefused,
    create_verified_backup,
    verify_backup_bundle,
)
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.paths import fsync_directory_chain
from app.broker.contract.errors import BrokerError
from app.broker.contract.registry import get_broker_registry
from app.broker.ibkr.config import live_artifacts_root
from app.broker_configuration.envelope import InvalidLiveEnvelope, require_whole_cent_loss_cap
from app.config import fleet_settings, settings
from app.schemas.alpaca_live_graduation import (
    LiveGraduationApplyOutcome,
    LiveGraduationPlanView,
    LiveGraduationStatus,
)
from app.services.alpaca_live_graduation_gate import graduation_mutation_fence
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_EVIDENCE_MAX_AGE_MS = 300_000
_CONFIRMATION_TTL_MS = 300_000
_PLAN_DIRECTORY = "live-graduation-plans"
_PROOF_DIRECTORY = "live-graduation-evidence"


class LiveGraduationRefused(RuntimeError):
    """A reviewed graduation prerequisite was absent or changed."""

    def __init__(self, reason: str, message: str, next_action: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.next_action = next_action


def _blocked_status(
    account_id: str,
    *,
    headline: str,
    detail: str,
    next_action: str,
) -> LiveGraduationStatus:
    return LiveGraduationStatus(
        account_id=account_id,
        configured_mode="live",
        authority="unavailable",
        state="blocked",
        headline=headline,
        detail=detail,
        next_action=next_action,
        restart_managed=fleet_settings.WORKER_SERVICE is not None,
    )


class AlpacaLiveGraduationService:
    """Compose the cutover ceremony from server-resolved runtime facts only."""

    def status(self, account_id: str) -> LiveGraduationStatus:
        configured = resolved_alpaca_settings()
        if configured.mode != "live":
            raise LiveGraduationRefused(
                "live_configuration_required",
                "Graduation is available only for an effective Live profile.",
                "Apply a verified Live profile and complete its controlled restart first.",
            )
        if settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
            return _blocked_status(
                account_id,
                headline="Authenticated control is required",
                detail="The worker cannot install a real-money authority while unauthenticated control is allowed.",
                next_action="Turn off unauthenticated data-plane control and restart this clerk.",
            )
        if fleet_settings.WORKER_SERVICE is None:
            return _blocked_status(
                account_id,
                headline="A supervised worker restart is not declared",
                detail="Graduation changes boot-time custody authority and must finish through a managed restart.",
                next_action="Declare this lane's worker service in its deployment and restart it.",
            )
        runtime = get_active_clerk_runtime()
        if runtime is None:
            return _blocked_status(
                account_id,
                headline="The Account Clerk is unavailable",
                detail="No active authority can prove which account world this worker currently serves.",
                next_action="Restore the clerk and refresh this page.",
            )
        if runtime.selected_account_authority_kind == "real_live":
            return LiveGraduationStatus(
                account_id=self._served_account(runtime.selected_account_id, account_id),
                configured_mode="live",
                authority="live",
                state="graduated",
                headline="Live authority is active",
                detail="Graduation is complete. It did not deploy or arm a strategy.",
                next_action="Deploy a new Live-sealed strategy instance, then review its arming ceremony.",
                restart_managed=True,
            )
        if runtime.selected_account_authority_kind != "shadow":
            return _blocked_status(
                account_id,
                headline="The worker is not serving the Shadow authority",
                detail="Graduation begins only from the isolated Shadow account world.",
                next_action="Restore the Live profile's Shadow authority and refresh this page.",
            )
        return LiveGraduationStatus(
            account_id=self._served_account(runtime.selected_account_id, account_id),
            configured_mode="live",
            authority="shadow",
            state="review_available",
            headline="Shadow authority is active",
            detail="Reviewing graduation will obtain fresh flatness and open-order evidence, verify a backup, and bind the stopped-bot roster.",
            next_action="Review the graduation evidence. No authority changes until the final confirmation.",
            restart_managed=True,
        )

    async def prepare(self, account_id: str) -> LiveGraduationPlanView:
        status = self.status(account_id)
        if status.state != "review_available":
            raise LiveGraduationRefused(
                "graduation_not_available",
                status.headline,
                status.next_action or "Refresh the account authority state.",
            )
        served_account_id = status.account_id
        evidence = await self._capture_broker_evidence(served_account_id)
        async with graduation_mutation_fence():
            try:
                plan, backup_reference = await asyncio.to_thread(
                    self._prepare_domain_plan,
                    served_account_id,
                    evidence,
                )
            except (CutoverRefused, RecoveryRefused, OSError, ValueError) as exc:
                raise LiveGraduationRefused(
                    "graduation_plan_refused",
                    str(exc),
                    "Keep every bot stopped, leave the account flat and order-free, then prepare a fresh review.",
                ) from exc
        configured = resolved_alpaca_settings()
        values = (
            configured.live_loss_fraction,
            configured.live_loss_usd,
            configured.live_arming_max_sessions,
            configured.live_xh_entry_bps,
            configured.live_xh_exit_bps,
        )
        if any(value is None for value in values):  # guarded by live settings; fail closed if drifted
            raise LiveGraduationRefused(
                "live_envelope_missing",
                "The effective Live profile has no complete risk envelope.",
                "Repair and apply the Live profile before preparing graduation again.",
            )
        try:
            require_whole_cent_loss_cap(float(configured.live_loss_usd))
        except InvalidLiveEnvelope as exc:
            raise LiveGraduationRefused(
                "live_envelope_invalid", str(exc), "Save and apply a loss cap in whole cents before graduation."
            ) from exc
        return LiveGraduationPlanView(
            plan_id=plan.plan_id,
            confirmation_token=plan.confirmation_token,
            account_id=plan.account_id,
            created_at_ms=plan.created_at_ms,
            expires_at_ms=plan.expires_at_ms,
            broker_observed_at_ms=plan.broker_evidence.observed_at_ms,
            position_count=len(plan.broker_evidence.positions),
            open_order_count=len(plan.broker_evidence.open_order_ids),
            stopped_bot_ids=tuple(item.strategy_instance_id for item in plan.runner_roster),
            backup_reference=backup_reference,
            daily_loss_fraction=float(configured.live_loss_fraction),
            daily_loss_usd=float(configured.live_loss_usd),
            arming_max_sessions=int(configured.live_arming_max_sessions),
            extended_hours_entry_bps=float(configured.live_xh_entry_bps),
            extended_hours_exit_bps=float(configured.live_xh_exit_bps),
            consequence=(
                "Confirmation activates real-money custody and restarts this clerk. "
                "Every existing Shadow bot stays stopped and foreign to the Live authority; "
                "nothing is deployed or armed by graduation."
            ),
        )

    async def apply(
        self,
        account_id: str,
        *,
        plan_id: str,
        confirmation_token: str,
    ) -> LiveGraduationApplyOutcome:
        current = self.status(account_id)
        if current.state != "review_available":
            raise LiveGraduationRefused(
                "graduation_not_available",
                current.headline,
                current.next_action or "Refresh the account authority state.",
            )
        served_account_id = current.account_id
        try:
            plan, backup_path = await asyncio.to_thread(
                self._read_persisted_plan, served_account_id, plan_id
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LiveGraduationRefused(
                "graduation_plan_unavailable",
                "The reviewed graduation plan is missing or unreadable.",
                "Prepare a fresh graduation review.",
            ) from exc
        if plan.account_id != served_account_id or plan.plan_id != plan_id:
            raise LiveGraduationRefused(
                "graduation_plan_mismatch",
                "The reviewed plan does not belong to this account.",
                "Prepare a fresh review from this account's Configuration page.",
            )
        # Re-observe broker state now, at apply time — never reuse prepare()'s
        # snapshot. apply_cutover() refuses when this evidence disagrees with
        # the plan's stored evidence, which is the only thing that catches a
        # position or order opened during the confirmation window.
        evidence = await self._capture_broker_evidence(served_account_id)
        async with graduation_mutation_fence():
            try:
                receipt = await asyncio.to_thread(
                    self._apply_domain_plan,
                    plan,
                    backup_path,
                    confirmation_token,
                    evidence,
                )
            except (CutoverRefused, RecoveryRefused, OSError, ValueError) as exc:
                raise LiveGraduationRefused(
                    "graduation_apply_refused",
                    str(exc),
                    "Nothing was forced. Prepare a fresh review after resolving the changed evidence.",
                ) from exc
        return LiveGraduationApplyOutcome(
            account_id=served_account_id,
            plan_id=plan.plan_id,
            state="restart_scheduled",
            receipt_reference=receipt.receipt_reference,
            activated_at_ms=receipt.activation.activated_at_ms,
            message=(
                "Live activation is durable. This clerk will restart into the Live authority; "
                "no strategy was deployed or armed."
            ),
        )

    async def _capture_broker_evidence(self, account_id: str) -> BrokerCutoverEvidence:
        port = get_broker_registry().resolve("alpaca")
        try:
            account, positions, orders = await asyncio.wait_for(
                asyncio.gather(
                    port.get_account(),
                    port.list_positions(),
                    port.list_orders(status="open", limit=500),
                ),
                timeout=20.0,
            )
        except (BrokerError, TimeoutError) as exc:
            raise LiveGraduationRefused(
                "broker_evidence_unavailable",
                "Fresh broker account, position, and open-order evidence could not be obtained.",
                "Restore the Alpaca read connection and prepare the review again.",
            ) from exc
        if account.account_id != account_id or account.account_mode != "live":
            raise LiveGraduationRefused(
                "live_mode_disagreement",
                "The broker-observed Live account does not match this workspace.",
                "Repair the effective profile or account pin before graduation.",
            )
        observed_at_ms = now_ms_utc()
        payload = {
            "schema_version": 1,
            "account_id": account.account_id,
            "account_mode": account.account_mode,
            "observed_at_ms": observed_at_ms,
            "positions": {position.symbol: position.quantity for position in positions},
            "open_order_ids": [order.order_id for order in orders],
        }
        proof_reference = await asyncio.to_thread(
            self._write_broker_evidence_proof, account_id, observed_at_ms, payload
        )
        return BrokerCutoverEvidence(
            account_id=account.account_id,
            account_mode="live",
            observed_at_ms=observed_at_ms,
            proof_reference=proof_reference,
            positions=payload["positions"],
            open_order_ids=tuple(payload["open_order_ids"]),
        )

    def _write_broker_evidence_proof(
        self,
        account_id: str,
        observed_at_ms: int,
        payload: dict[str, Any],
    ) -> str:
        artifacts_root = resolved_alpaca_settings().clerk_dir
        _accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
        proof_dir = account_dir / _PROOF_DIRECTORY
        if proof_dir.is_symlink():
            raise LiveGraduationRefused(
                "graduation_evidence_unsafe",
                "The graduation evidence directory is not safe to use.",
                "Inspect the clerk volume before attempting graduation again.",
            )
        proof_dir.mkdir(parents=True, exist_ok=True)
        fsync_directory_chain(proof_dir, artifacts_root)
        proof_path = proof_dir / (
            f"broker-observation-{observed_at_ms}-{secrets.token_hex(6)}.json"
        )
        atomic_write_json(proof_path, payload)
        return relative_reference(artifacts_root, proof_path)

    def _prepare_domain_plan(
        self,
        account_id: str,
        evidence: BrokerCutoverEvidence,
    ) -> tuple[CutoverPlan, str]:
        artifacts_root = resolved_alpaca_settings().clerk_dir
        runner_root = live_artifacts_root()
        accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
        if EstablishedAccountsRegistry(accounts_root).latest(account_id) is None:
            initialize_cutover_authority(
                account_id=account_id,
                artifacts_root=artifacts_root,
                runner_artifacts_root=runner_root,
                broker_evidence=evidence,
                max_broker_evidence_age_ms=_EVIDENCE_MAX_AGE_MS,
            )
        plan = plan_cutover(
            account_id=account_id,
            artifacts_root=artifacts_root,
            runner_artifacts_root=runner_root,
            broker_evidence=evidence,
            max_broker_evidence_age_ms=_EVIDENCE_MAX_AGE_MS,
            confirmation_ttl_ms=_CONFIRMATION_TTL_MS,
        )
        backup = create_verified_backup(
            account_id=account_id,
            artifacts_root=artifacts_root,
            source_immutable=True,
        )
        backup_reference = relative_reference(artifacts_root, backup.bundle_path)
        plan_dir = account_dir / _PLAN_DIRECTORY
        if plan_dir.is_symlink():
            raise CutoverRefused("live graduation plan directory must not be a symbolic link")
        plan_dir.mkdir(parents=True, exist_ok=True)
        fsync_directory_chain(plan_dir, artifacts_root)
        atomic_write_json(
            plan_dir / f"{plan.plan_id}.json",
            {
                "schema_version": 1,
                "plan": asdict(plan),
                "backup_reference": backup_reference,
            },
        )
        return plan, backup_reference

    def _apply_domain_plan(
        self,
        plan: CutoverPlan,
        backup_path: Path,
        confirmation_token: str,
        broker_evidence: BrokerCutoverEvidence,
    ) -> Any:
        artifacts_root = resolved_alpaca_settings().clerk_dir
        verify_backup_bundle(
            account_id=plan.account_id,
            artifacts_root=artifacts_root,
            bundle_path=backup_path,
        )
        return apply_cutover(
            plan=plan,
            confirmation_token=confirmation_token,
            artifacts_root=artifacts_root,
            runner_artifacts_root=live_artifacts_root(),
            broker_evidence=broker_evidence,
            max_broker_evidence_age_ms=_EVIDENCE_MAX_AGE_MS,
        )

    def _read_persisted_plan(self, account_id: str, plan_id: str) -> tuple[CutoverPlan, Path]:
        artifacts_root = resolved_alpaca_settings().clerk_dir
        _accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
        plan_path = account_dir / _PLAN_DIRECTORY / f"{plan_id}.json"
        if plan_path.is_symlink() or not plan_path.is_file():
            raise ValueError("graduation plan must be a regular file")
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        if set(payload) != {"schema_version", "plan", "backup_reference"} or payload["schema_version"] != 1:
            raise ValueError("graduation plan record fields do not match schema version 1")
        plan = decode_cutover_plan(payload["plan"])
        backup_reference = payload["backup_reference"]
        if not isinstance(backup_reference, str) or not backup_reference:
            raise ValueError("graduation plan backup reference is invalid")
        backup_path = (artifacts_root / backup_reference).resolve()
        try:
            backup_path.relative_to(artifacts_root.resolve())
        except ValueError as exc:
            raise ValueError("graduation plan backup escapes the clerk root") from exc
        return plan, backup_path

    @classmethod
    def _served_account(cls, selected_account_id: str | None, account_id: str) -> str:
        """The live account this worker serves, in Alpaca's spelling, when the route names it.

        Public routes carry the canonical (lowercase) account while custody keeps
        Alpaca's spelling, so the route is matched case-insensitively against the
        live account the selected authority serves (a ``shadow:`` custody id
        observes its live account). Every later step -- broker evidence, the
        account folder, the cutover plan -- uses the returned spelling, never the
        route's: the broker reports Alpaca's spelling and the folders are named
        by it.
        """
        if selected_account_id is None:
            raise cls._wrong_account(account_id)
        served_account_id = live_account_id_for_shadow_account(selected_account_id)
        if canonical_alpaca_account_id(served_account_id) != canonical_alpaca_account_id(account_id):
            raise cls._wrong_account(account_id)
        return served_account_id

    @staticmethod
    def _wrong_account(account_id: str) -> LiveGraduationRefused:
        return LiveGraduationRefused(
            "graduation_account_mismatch",
            f"This worker does not serve account {account_id}.",
            "Return to the Alpaca account list and open the lane bound to this account.",
        )


_service = AlpacaLiveGraduationService()


def get_alpaca_live_graduation_service() -> AlpacaLiveGraduationService:
    return _service


async def restart_after_graduation() -> None:
    """Let the 202 body flush, then enter Uvicorn's graceful shutdown path.

    The deployed clerk service uses a supervisor restart policy. SIGTERM is
    intentional here: lifespan shutdown drains the shadow Clerk and releases
    its lease before the supervisor boots the activation-selected Live Clerk.
    """
    await asyncio.sleep(0.35)
    logger.warning(
        "Live graduation activation committed; requesting supervised clerk restart",
        extra={"action": "live_graduation_restart_requested"},
    )
    os.kill(os.getpid(), signal.SIGTERM)
