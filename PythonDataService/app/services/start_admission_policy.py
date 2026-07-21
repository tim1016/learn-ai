"""Typed admission policies for interactive and receipt-authorized Starts.

The interactive policy preserves the cockpit's complete fail-closed gate chain.
The cohort policy is deliberately narrower: a durable V2 receipt proves the
member, run, and start settings once; only safety state that can change between
authorization and a scheduled slot is checked again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import HTTPException

from app.broker.ibkr.config import IbkrSettings
from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRepo,
    BotRollCallOfferRecord,
    stable_bot_lifecycle_state_path,
)
from app.schemas.live_runs import HostRunnerStartRequest
from app.services.account_crash_recovery import crash_recovery_block_detail, crash_recovery_blocking_binding
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.daily_session_schedule import StartBoundaryVerdict

StartAdmissionPolicyName = Literal["interactive", "receipt_authorized_cohort"]
StartAdmissionDetail = str | dict[str, object]
RunRows = dict[str, list[dict[str, object]]]


@dataclass(frozen=True)
class StartAdmissionRefusal:
    """One typed policy refusal for the router or cohort outcome writer."""

    status_code: int
    detail: StartAdmissionDetail


@dataclass(frozen=True)
class StartAdmissionDecision:
    """The selected policy and its authoritative admission result."""

    policy: StartAdmissionPolicyName
    strategy_instance_id: str | None
    refusal: StartAdmissionRefusal | None = None
    idempotent_process: dict[str, object] | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True)
class StartAdmissionDependencies:
    """Narrow I/O boundary for start-admission policy evaluation."""

    scan_runs_by_instance: Callable[[Path], RunRows]
    run_is_soft_deleted: Callable[[Path, str, str], bool]
    soft_deleted_detail: Callable[[str, str], dict[str, object]]
    account_freeze: Callable[[Path, list[dict[str, object]]], AccountFreezeEvidence | None]
    run_account_id: Callable[[Path], str | None]
    interactive_observation_guard: Callable[[Path, str, IbkrSettings, int], Awaitable[None]]
    interactive_fleet_guard: Callable[[Path, str], Awaitable[None]]
    fetch_instance_process: Callable[[str, str], Awaitable[tuple[object, dict[str, object] | None]]]
    active_roll_call_offer: Callable[[Path, str, int], BotRollCallOfferRecord | None]
    read_account_events: Callable[[Path, str], list[dict[str, object]]]
    live_config_for_run: Callable[[Path], Mapping[str, object] | None]
    start_boundary_allowed: Callable[[int, Mapping[str, object] | None], StartBoundaryVerdict]
    now_ms: Callable[[], int]


@dataclass(frozen=True)
class _ResolvedStart:
    strategy_instance_id: str
    run_id: str
    run_dir: Path
    account_id: str | None


class StartAdmissionService:
    """Select and evaluate the interactive or durable-receipt start policy."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        live_runs_root: Path,
        settings: IbkrSettings | None = None,
        dependencies: StartAdmissionDependencies,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._live_runs_root = live_runs_root
        self._settings = settings
        self._dependencies = dependencies

    async def admit(
        self,
        run_id: str,
        request: HostRunnerStartRequest,
    ) -> StartAdmissionDecision:
        """Return one typed decision without authoring router conditionals."""

        resolved = self._resolve(run_id)
        if resolved is None:
            # Preserve the existing legacy behaviour: only the daemon can
            # authoritatively reject an unknown run id.
            return StartAdmissionDecision(policy="interactive", strategy_instance_id=None)

        receipt_authorized = self._receipt_authorizes(resolved, request)
        if receipt_authorized:
            return await self._admit_receipt_authorized_cohort(resolved, run_id)
        return await self._admit_interactive(resolved, run_id, request)

    def _resolve(self, run_id: str) -> _ResolvedStart | None:
        for strategy_instance_id, runs in self._dependencies.scan_runs_by_instance(self._live_runs_root).items():
            for run in runs:
                if run.get("run_id") != run_id:
                    continue
                raw_run_dir = run.get("run_dir")
                if not isinstance(raw_run_dir, str):
                    continue
                run_dir = Path(raw_run_dir)
                return _ResolvedStart(
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    run_dir=run_dir,
                    account_id=self._dependencies.run_account_id(run_dir),
                )
        return None

    def _receipt_authorizes(
        self,
        resolved: _ResolvedStart,
        request: HostRunnerStartRequest,
    ) -> bool:
        """Prove that a V2 receipt, including its offer pin, selects the policy."""

        if request.cohort_id is None or resolved.account_id is None:
            return False
        try:
            events = self._dependencies.read_account_events(self._artifacts_root, resolved.account_id)
            authorization = CohortBatchLaunchService.authorization_event(events, request.cohort_id)
        except (OSError, ValueError):
            return False
        if authorization is None:
            return False
        authorization_seq, receipt = authorization
        if receipt.schema_version != 2 or receipt.account_id != resolved.account_id:
            return False
        pin = next(
            (
                candidate
                for candidate in receipt.member_pins
                if candidate.strategy_instance_id == resolved.strategy_instance_id
                and candidate.run_id == resolved.run_id
            ),
            None,
        )
        slot = next(
            (
                candidate
                for candidate in receipt.member_schedule
                if candidate.strategy_instance_id == resolved.strategy_instance_id
                and candidate.run_id == resolved.run_id
            ),
            None,
        )
        if pin is None or slot is None or self._dependencies.now_ms() < slot.scheduled_start_at_ms:
            return False
        for event in events:
            if (
                event.get("event_type") == "cohort_batch_launch_member_start_recorded"
                and event.get("cohort_id") == receipt.cohort_id
                and event.get("strategy_instance_id") == resolved.strategy_instance_id
                and _event_follows_authorization(event, authorization_seq)
            ):
                # A receipt authorizes one scheduled start attempt, not a
                # client-selectable bypass that can revive a finished slot.
                return False
        return pin.roll_call_offer_id == request.roll_call_offer_id and slot.start_request == request.model_dump(
            mode="json",
            exclude={"roll_call_offer_id", "cohort_id"},
        )

    async def _admit_receipt_authorized_cohort(
        self,
        resolved: _ResolvedStart,
        run_id: str,
    ) -> StartAdmissionDecision:
        """Recheck only dynamic cohort safety state at a durable slot."""

        rejection = self._soft_delete_refusal(resolved.strategy_instance_id, run_id)
        if rejection is not None:
            return self._refused("receipt_authorized_cohort", resolved.strategy_instance_id, rejection)
        rejection = self._lifecycle_refusal(resolved.strategy_instance_id)
        if rejection is not None:
            return self._refused("receipt_authorized_cohort", resolved.strategy_instance_id, rejection)
        account_freeze = self._dependencies.account_freeze(
            self._artifacts_root,
            [{"run_dir": str(resolved.run_dir)}],
        )
        if account_freeze is not None:
            return self._refused(
                "receipt_authorized_cohort",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ACCOUNT_FROZEN",
                        "message": "This broker account is frozen until unresolved exposure is reconciled.",
                        "gate_result": account_freeze.to_gate_result().model_dump(mode="json"),
                    },
                ),
            )
        rejection = self._crash_recovery_refusal(resolved)
        if rejection is not None:
            return self._refused("receipt_authorized_cohort", resolved.strategy_instance_id, rejection)
        if (resolved.run_dir / "poisoned.flag").exists():
            return self._refused(
                "receipt_authorized_cohort",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "STOPPED_REQUIRES_REDEPLOY",
                        "message": "This run is permanently retired. Redeploy the bot to trade again.",
                    },
                ),
            )
        _result, daemon = await self._dependencies.fetch_instance_process(
            self._settings.live_runner_daemon_url if self._settings is not None else "",
            resolved.strategy_instance_id,
        )
        if daemon is None:
            return self._refused(
                "receipt_authorized_cohort",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "HOST_SERVICE_OFFLINE",
                        "message": "The bot service is offline. Start it on the host machine first.",
                    },
                ),
            )
        if daemon.get("state") == "running" and daemon.get("run_id") == resolved.run_id:
            return StartAdmissionDecision(
                policy="receipt_authorized_cohort",
                strategy_instance_id=resolved.strategy_instance_id,
                idempotent_process=daemon,
            )
        daemon_refusal = self._daemon_state_refusal(daemon)
        if daemon_refusal is not None:
            return self._refused("receipt_authorized_cohort", resolved.strategy_instance_id, daemon_refusal)
        return StartAdmissionDecision(
            policy="receipt_authorized_cohort",
            strategy_instance_id=resolved.strategy_instance_id,
        )

    async def _admit_interactive(
        self,
        resolved: _ResolvedStart,
        run_id: str,
        request: HostRunnerStartRequest,
    ) -> StartAdmissionDecision:
        """Preserve the existing complete, fail-closed interactive gate chain."""

        rejection = self._soft_delete_refusal(resolved.strategy_instance_id, run_id)
        if rejection is not None:
            return self._refused("interactive", resolved.strategy_instance_id, rejection)
        rejection = self._lifecycle_refusal(resolved.strategy_instance_id)
        if rejection is not None:
            return self._refused("interactive", resolved.strategy_instance_id, rejection)
        account_freeze = self._dependencies.account_freeze(
            self._artifacts_root,
            [{"run_dir": str(resolved.run_dir)}],
        )
        if account_freeze is not None:
            return self._refused(
                "interactive",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ACCOUNT_FROZEN",
                        "message": "This broker account is frozen until unresolved exposure is reconciled.",
                        "gate_result": account_freeze.to_gate_result().model_dump(mode="json"),
                    },
                ),
            )
        if resolved.account_id is None:
            return self._refused(
                "interactive",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ACCOUNT_ID_UNAVAILABLE",
                        "message": "The run ledger has no account identity; broker truth cannot be verified.",
                        "gate_id": "account.broker_truth",
                        "operator_next_step": "WAIT_FOR_BROKER_TRUTH",
                    },
                ),
            )
        now_ms = self._dependencies.now_ms()
        if self._settings is None:
            raise RuntimeError("interactive admission requires IBKR settings")
        rejection = await self._invoke_async_guard(
            lambda: self._dependencies.interactive_observation_guard(
                self._artifacts_root,
                resolved.account_id,
                self._settings,
                now_ms,
            )
        )
        if rejection is not None:
            return self._refused("interactive", resolved.strategy_instance_id, rejection)
        rejection = await self._invoke_async_guard(
            lambda: self._dependencies.interactive_fleet_guard(self._live_runs_root, resolved.account_id or "")
        )
        if rejection is not None:
            return self._refused("interactive", resolved.strategy_instance_id, rejection)
        rejection = self._crash_recovery_refusal(resolved)
        if rejection is not None:
            return self._refused("interactive", resolved.strategy_instance_id, rejection)
        if (resolved.run_dir / "poisoned.flag").exists():
            return self._refused(
                "interactive",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "STOPPED_REQUIRES_REDEPLOY",
                        "message": "This run is permanently retired. Redeploy the bot to trade again.",
                    },
                ),
            )
        _result, daemon = await self._dependencies.fetch_instance_process(
            self._settings.live_runner_daemon_url,
            resolved.strategy_instance_id,
        )
        if daemon is None:
            return self._refused(
                "interactive",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "HOST_SERVICE_OFFLINE",
                        "message": "The bot service is offline. Start it on the host machine first.",
                    },
                ),
            )
        daemon_refusal = self._daemon_state_refusal(daemon)
        if daemon_refusal is not None:
            return self._refused("interactive", resolved.strategy_instance_id, daemon_refusal)
        boundary = self._dependencies.start_boundary_allowed(
            now_ms,
            self._dependencies.live_config_for_run(resolved.run_dir),
        )
        if not boundary.allowed:
            return self._refused(
                "interactive",
                resolved.strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": boundary.reason_code,
                        "message": boundary.message,
                        "gate_id": "daily_lifecycle.effective_stop",
                        "strategy_instance_id": resolved.strategy_instance_id,
                        "session_date": boundary.session_date,
                        "effective_stop_ms": boundary.effective_stop_ms,
                    },
                ),
            )
        return self._roll_call_decision(resolved.strategy_instance_id, run_id, request, now_ms)

    def _roll_call_decision(
        self,
        strategy_instance_id: str,
        run_id: str,
        request: HostRunnerStartRequest,
        now_ms: int,
    ) -> StartAdmissionDecision:
        if request.roll_call_offer_id is None:
            return self._refused(
                "interactive",
                strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ROLL_CALL_OFFER_REQUIRED",
                        "message": "Run roll call and start from the current offer.",
                        "remediation": "Run roll call, wait for this bot to show Ready, then click Start before the offer expires.",
                        "gate_id": "daily_lifecycle.roll_call_offer",
                        "strategy_instance_id": strategy_instance_id,
                    },
                ),
            )
        active = self._dependencies.active_roll_call_offer(
            self._live_runs_root,
            strategy_instance_id,
            now_ms,
        )
        if active is None:
            return self._refused(
                "interactive",
                strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ROLL_CALL_OFFER_EXPIRED",
                        "message": "The roll-call start offer is absent or expired. Run roll call again.",
                        "remediation": "Run roll call again, wait for this bot to show Ready, then click Start before the offer expires.",
                        "gate_id": "daily_lifecycle.roll_call_offer",
                        "strategy_instance_id": strategy_instance_id,
                    },
                ),
            )
        if active.offer_id != request.roll_call_offer_id:
            return self._refused(
                "interactive",
                strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ROLL_CALL_OFFER_STALE",
                        "message": "This start request does not match the current roll-call offer.",
                        "remediation": "Refresh Bot Control, then start from the current roll-call offer.",
                        "gate_id": "daily_lifecycle.roll_call_offer",
                        "strategy_instance_id": strategy_instance_id,
                        "current_offer_id": active.offer_id,
                    },
                ),
            )
        if active.run_id != run_id:
            return self._refused(
                "interactive",
                strategy_instance_id,
                StartAdmissionRefusal(
                    409,
                    {
                        "reason_code": "ROLL_CALL_OFFER_RUN_MISMATCH",
                        "message": "This roll-call offer belongs to a different run. Run roll call again.",
                        "remediation": "Run roll call again, then start the run attached to the new offer.",
                        "gate_id": "daily_lifecycle.roll_call_offer",
                        "strategy_instance_id": strategy_instance_id,
                        "run_id": run_id,
                        "offer_run_id": active.run_id,
                    },
                ),
            )
        return StartAdmissionDecision(policy="interactive", strategy_instance_id=strategy_instance_id)

    def _soft_delete_refusal(self, strategy_instance_id: str, run_id: str) -> StartAdmissionRefusal | None:
        if not self._dependencies.run_is_soft_deleted(
            self._artifacts_root,
            strategy_instance_id,
            run_id,
        ):
            return None
        return StartAdmissionRefusal(
            410,
            self._dependencies.soft_deleted_detail(strategy_instance_id, run_id),
        )

    @staticmethod
    def _daemon_state_refusal(daemon: dict[str, object]) -> StartAdmissionRefusal | None:
        state = str(daemon.get("state") or "idle")
        if state == "running":
            return StartAdmissionRefusal(
                409,
                {"reason_code": "ALREADY_RUNNING", "message": "The bot is already running."},
            )
        if state == "stopping":
            return StartAdmissionRefusal(
                409,
                {
                    "reason_code": "STOPPING",
                    "message": "The bot is shutting down. Wait for it to finish before starting again.",
                },
            )
        return None

    @staticmethod
    async def _invoke_async_guard(guard: Callable[[], Awaitable[None]]) -> StartAdmissionRefusal | None:
        try:
            await guard()
        except HTTPException as exc:
            return StartAdmissionRefusal(exc.status_code, _http_exception_detail(exc))
        return None

    @staticmethod
    def _refused(
        policy: StartAdmissionPolicyName,
        strategy_instance_id: str,
        refusal: StartAdmissionRefusal,
    ) -> StartAdmissionDecision:
        return StartAdmissionDecision(
            policy=policy,
            strategy_instance_id=strategy_instance_id,
            refusal=refusal,
        )

    def _lifecycle_refusal(self, strategy_instance_id: str) -> StartAdmissionRefusal | None:
        try:
            path = stable_bot_lifecycle_state_path(self._artifacts_root, strategy_instance_id)
        except ValueError:
            return StartAdmissionRefusal(400, "invalid strategy_instance_id")
        try:
            record = BotLifecycleStateRepo(path).read()
        except BotLifecycleStateCorruptError:
            return StartAdmissionRefusal(
                409,
                {
                    "reason_code": "BOT_LIFECYCLE_STATE_UNREADABLE",
                    "message": "The bot lifecycle state is unreadable. Repair it before starting.",
                    "gate_id": "daily_lifecycle.phase",
                    "strategy_instance_id": strategy_instance_id,
                },
            )
        if record is None or record.phase != BotLifecyclePhase.RETIRED:
            return None
        return StartAdmissionRefusal(
            409,
            {
                "reason_code": "BOT_RETIRED",
                "message": "This bot is retired. Deploy a replacement bot before starting.",
                "gate_id": "daily_lifecycle.phase",
                "strategy_instance_id": strategy_instance_id,
            },
        )

    def _crash_recovery_refusal(self, resolved: _ResolvedStart) -> StartAdmissionRefusal | None:
        if resolved.account_id is None:
            return None
        binding = crash_recovery_blocking_binding(
            self._artifacts_root,
            account_id=resolved.account_id,
            strategy_instance_id=resolved.strategy_instance_id,
        )
        if binding is None:
            return None
        return StartAdmissionRefusal(
            409,
            crash_recovery_block_detail(resolved.strategy_instance_id, binding),
        )


def _event_follows_authorization(event: Mapping[str, object], authorization_seq: int) -> bool:
    seq = event.get("seq")
    return isinstance(seq, int) and not isinstance(seq, bool) and seq > authorization_seq


def _http_exception_detail(exc: HTTPException) -> StartAdmissionDetail:
    """Translate legacy interactive guards without widening the policy result."""

    if isinstance(exc.detail, str):
        return exc.detail
    if isinstance(exc.detail, dict):
        return exc.detail
    return str(exc.detail)
