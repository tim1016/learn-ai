"""Server-owned admission and execution for an account cohort launch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchMemberSchedule,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    CohortBatchLaunchRequestProvenance,
    project_restart_intensity_gate,
    read_account_events,
    record_cohort_batch_launch_outcomes,
    record_cohort_batch_launch_receipt,
)
from app.schemas.cohort_batch_launch import CohortBatchLaunchStatusResponse
from app.schemas.live_runs import (
    BotRollCallOffer,
    BotRollCallResponse,
    HostRunnerActionResponse,
    HostRunnerStartRequest,
)
from app.services.cohort_batch_launch import CohortBatchLaunchService, parse_cohort_evidence_sample
from app.services.cohort_evidence import (
    CohortEvidenceSampler,
    CohortEvidenceSamplerRegistry,
)
from app.services.cohort_evidence_runtime import CohortEvidenceRuntimeObserver

RollCall = Callable[[], Awaitable[BotRollCallResponse]]
StartRun = Callable[[str, HostRunnerStartRequest], Awaitable[HostRunnerActionResponse]]
VisibleRuns = Callable[[Path], dict[str, list[dict[str, object]]]]
RunAccountId = Callable[[Path], str | None]
StartRequestForRun = Callable[[Path], HostRunnerStartRequest | None]
NowMs = Callable[[], int]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PinnedCohortMember:
    """One pinned authorization member and its persisted start settings."""

    pin: CohortBatchLaunchMemberPin
    start_request: HostRunnerStartRequest

_COHORT_EVIDENCE_CADENCE_MS = 5_000
_COHORT_VALIDATION_WINDOW_MS = 60 * 60 * 1_000
_THREE_BOT_STAGGER_MS = 15 * 60 * 1_000
_THREE_BOT_OVERLAP_MS = 15 * 60 * 1_000


class CohortLaunchSchedulerRegistry:
    """Own server-owned V2 start schedulers for the life of this process."""

    def __init__(self) -> None:
        self._schedulers: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = {}

    def start(self, cohort_id: str, scheduler: Callable[[asyncio.Event], Awaitable[None]]) -> None:
        if cohort_id in self._schedulers:
            return
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler(stop))

        def _on_done(finished: asyncio.Task[None]) -> None:
            current = self._schedulers.get(cohort_id)
            if current is not None and current[1] is finished:
                self._schedulers.pop(cohort_id, None)
            if finished.cancelled():
                return
            exception = finished.exception()
            if exception is not None:
                logger.error(
                    "cohort launch scheduler task died",
                    exc_info=(type(exception), exception, exception.__traceback__),
                    extra={"cohort_id": cohort_id},
                )

        task.add_done_callback(_on_done)
        self._schedulers[cohort_id] = (stop, task)

    async def stop_all(self) -> None:
        schedulers = tuple(self._schedulers.values())
        self._schedulers.clear()
        for stop, _task in schedulers:
            stop.set()
        if schedulers:
            await asyncio.gather(*(task for _stop, task in schedulers), return_exceptions=True)


_SCHEDULER_REGISTRY = CohortLaunchSchedulerRegistry()


def get_cohort_launch_scheduler_registry() -> CohortLaunchSchedulerRegistry:
    """Return the process-owned durable-schedule task registry."""

    return _SCHEDULER_REGISTRY


class CohortLaunchCoordinator:
    """Admits a displayed cohort and records server-derived start outcomes."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        live_runs_root: Path,
        run_roll_call: RollCall,
        start_run: StartRun,
        visible_runs_by_instance: VisibleRuns,
        run_account_id: RunAccountId,
        start_request_for_run: StartRequestForRun,
        now_ms: NowMs,
        evidence_samplers: CohortEvidenceSamplerRegistry,
        launch_schedulers: CohortLaunchSchedulerRegistry,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._live_runs_root = live_runs_root
        self._run_roll_call = run_roll_call
        self._start_run = start_run
        self._visible_runs_by_instance = visible_runs_by_instance
        self._run_account_id = run_account_id
        self._start_request_for_run = start_request_for_run
        self._now_ms = now_ms
        self._evidence_samplers = evidence_samplers
        self._launch_schedulers = launch_schedulers

    async def launch(
        self,
        *,
        account_id: str,
        requested_members: tuple[str, ...],
        operator_identity: str,
        identity_header_present: bool,
        client_host: str | None,
        launch_profile: str | None = None,
    ) -> CohortBatchLaunchStatusResponse:
        """Refresh, pin, record, then start without rolling back siblings."""

        roll_call = await self._run_roll_call()
        members = await asyncio.to_thread(
            self._pins,
            account_id,
            requested_members,
            roll_call.offers,
        )
        now_ms = self._now_ms()
        if launch_profile == "paper_three_bot_stagger_v2":
            return await self._launch_three_bot_stagger(
                account_id=account_id,
                members=members,
                operator_identity=operator_identity,
                identity_header_present=identity_header_present,
                client_host=client_host,
                now_ms=now_ms,
            )
        receipt = CohortBatchLaunchReceipt(
            account_id=account_id,
            cohort_id=f"paper-validation-{now_ms}-{uuid4().hex[:12]}",
            member_strategy_instance_ids=tuple(member.pin.strategy_instance_id for member in members),
            window_start_ms=now_ms,
            window_end_ms=now_ms + _COHORT_VALIDATION_WINDOW_MS,
            authorized_by=operator_identity,
            recorded_at_ms=now_ms,
            member_pins=tuple(member.pin for member in members),
            request_provenance=CohortBatchLaunchRequestProvenance(
                operator_identity_header_present=identity_header_present,
                client_host=client_host,
            ),
        )
        await asyncio.to_thread(record_cohort_batch_launch_receipt, self._artifacts_root, receipt)
        outcomes = await asyncio.gather(
            *(self._start_member(member, cohort_id=receipt.cohort_id) for member in members),
        )
        outcomes_receipt = CohortBatchLaunchOutcomesReceipt(
            account_id=account_id,
            cohort_id=receipt.cohort_id,
            outcomes=tuple(outcomes),
            recorded_at_ms=self._now_ms(),
        )
        await asyncio.to_thread(
            record_cohort_batch_launch_outcomes,
            self._artifacts_root,
            outcomes_receipt,
        )
        try:
            await self._start_evidence_sampler(receipt)
        except Exception:
            logger.exception(
                "cohort launch completed but evidence sampler did not start",
                extra={"account_id": account_id, "cohort_id": receipt.cohort_id},
            )
        status_view = await CohortBatchLaunchService(artifacts_root=self._artifacts_root).get_status(
            account_id=account_id,
            cohort_id=receipt.cohort_id,
        )
        if status_view is None:
            raise RuntimeError("newly persisted cohort receipt could not be read")
        return status_view

    async def _launch_three_bot_stagger(
        self,
        *,
        account_id: str,
        members: tuple[_PinnedCohortMember, ...],
        operator_identity: str,
        identity_header_present: bool,
        client_host: str | None,
        now_ms: int,
    ) -> CohortBatchLaunchStatusResponse:
        """Persist the fixed paper-only schedule before its first start is due."""

        if len(members) != 3:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "reason_code": "COHORT_THREE_BOT_PROFILE_REQUIRES_EXACTLY_THREE",
                    "message": "The paper validation profile requires exactly three current ready bots.",
                },
            )
        restart_gate = await asyncio.to_thread(
            project_restart_intensity_gate,
            self._artifacts_root,
            account_id=account_id,
            now_ms=now_ms,
        )
        if restart_gate.status == "freeze":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "COHORT_RESTART_INTENSITY_WOULD_FREEZE",
                    "message": (
                        "This three-bot cohort would breach the account restart-intensity gate. "
                        "No cohort authorization or start was recorded."
                    ),
                    "gate_result": restart_gate.model_dump(mode="json"),
                },
            )
        schedule = tuple(
            CohortBatchLaunchMemberSchedule(
                strategy_instance_id=member.pin.strategy_instance_id,
                run_id=member.pin.run_id,
                scheduled_start_at_ms=now_ms + index * _THREE_BOT_STAGGER_MS,
                start_request=member.start_request.model_dump(mode="json", exclude={"roll_call_offer_id", "cohort_id"}),
            )
            for index, member in enumerate(members)
        )
        validation_start_ms = schedule[-1].scheduled_start_at_ms + _COHORT_EVIDENCE_CADENCE_MS
        receipt = CohortBatchLaunchReceipt(
            schema_version=2,
            launch_profile="paper_three_bot_stagger_v2",
            account_id=account_id,
            cohort_id=f"paper-validation-{now_ms}-{uuid4().hex[:12]}",
            member_strategy_instance_ids=tuple(member.pin.strategy_instance_id for member in members),
            window_start_ms=validation_start_ms,
            window_end_ms=validation_start_ms + _THREE_BOT_OVERLAP_MS,
            authorized_by=operator_identity,
            recorded_at_ms=now_ms,
            member_pins=tuple(member.pin for member in members),
            member_schedule=schedule,
            request_provenance=CohortBatchLaunchRequestProvenance(
                operator_identity_header_present=identity_header_present,
                client_host=client_host,
            ),
        )
        await asyncio.to_thread(record_cohort_batch_launch_receipt, self._artifacts_root, receipt)
        self._launch_schedulers.start(
            receipt.cohort_id,
            lambda stop: self._run_three_bot_stagger(receipt, stop),
        )
        status_view = await CohortBatchLaunchService(artifacts_root=self._artifacts_root).get_status(
            account_id=account_id,
            cohort_id=receipt.cohort_id,
        )
        if status_view is None:
            raise RuntimeError("newly persisted cohort receipt could not be read")
        return status_view

    async def _run_three_bot_stagger(
        self,
        receipt: CohortBatchLaunchReceipt,
        stop: asyncio.Event,
    ) -> None:
        """Dispatch V2 slots from the durable receipt, never a browser timer."""

        service = CohortBatchLaunchService(artifacts_root=self._artifacts_root)
        for slot in receipt.member_schedule:
            delay_seconds = max(0, slot.scheduled_start_at_ms - self._now_ms()) / 1_000
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
            except TimeoutError:
                pass
            else:
                return
            outcomes = await service.scheduled_member_outcomes(
                account_id=receipt.account_id,
                cohort_id=receipt.cohort_id,
            )
            existing = outcomes.get(slot.strategy_instance_id)
            if existing is not None:
                continue
            if any(outcome.state != "accepted" for outcome in outcomes.values()):
                outcome = CohortBatchLaunchMemberOutcome(
                    strategy_instance_id=slot.strategy_instance_id,
                    state="skipped",
                    reason="COHORT_PRIOR_MEMBER_BLOCKED",
                    next_safe_action="Inspect the recorded member blocker before authorizing a new cohort.",
                )
            else:
                outcome = await self._start_scheduled_member(receipt, slot)
            await service.record_scheduled_member_outcome(
                account_id=receipt.account_id,
                cohort_id=receipt.cohort_id,
                outcome=outcome,
                recorded_at_ms=self._now_ms(),
            )
        outcomes = await service.scheduled_member_outcomes(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
        )
        if len(outcomes) == len(receipt.member_schedule) and all(
            outcome.state == "accepted" for outcome in outcomes.values()
        ):
            await self._start_evidence_sampler(receipt)

    async def _start_scheduled_member(
        self,
        receipt: CohortBatchLaunchReceipt,
        slot: CohortBatchLaunchMemberSchedule,
    ) -> CohortBatchLaunchMemberOutcome:
        """Refresh only the ephemeral offer; receipt membership and settings stay pinned."""

        roll_call = await self._run_roll_call()
        offer = next(
            (
                candidate
                for candidate in roll_call.offers
                if candidate.strategy_instance_id == slot.strategy_instance_id and candidate.run_id == slot.run_id
            ),
            None,
        )
        if offer is None:
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=slot.strategy_instance_id,
                state="blocked",
                reason="COHORT_SLOT_PREFLIGHT_NOT_READY",
                next_safe_action="Resolve the current server preflight blocker before authorizing a new cohort.",
            )
        try:
            start_request = HostRunnerStartRequest.model_validate(slot.start_request).model_copy(
                update={"roll_call_offer_id": offer.offer_id, "cohort_id": receipt.cohort_id}
            )
        except ValueError:
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=slot.strategy_instance_id,
                state="blocked",
                reason="COHORT_START_SETTINGS_UNREADABLE",
                next_safe_action="Redeploy this bot with complete persisted start settings before retrying.",
            )
        return await self._start_member(
            _PinnedCohortMember(
                pin=CohortBatchLaunchMemberPin(
                    strategy_instance_id=slot.strategy_instance_id,
                    run_id=slot.run_id,
                    roll_call_offer_id=offer.offer_id,
                ),
                start_request=start_request,
            ),
            cohort_id=receipt.cohort_id,
        )

    async def _start_evidence_sampler(self, receipt: CohortBatchLaunchReceipt) -> None:
        """Persist the first observation and retain a server-owned five-second task."""

        sampler = _evidence_sampler(
            receipt=receipt,
            artifacts_root=self._artifacts_root,
            live_runs_root=self._live_runs_root,
            visible_runs_by_instance=self._visible_runs_by_instance,
            now_ms=self._now_ms,
            first_expected_at_ms=receipt.window_start_ms,
        )
        if receipt.schema_version == 2:
            # V2's proof window begins one cadence after the final scheduled
            # start. Do not backdate a future observation merely because the
            # final Start response returned quickly.
            self._evidence_samplers.start(receipt.cohort_id, sampler)
            return
        await sampler.sample_once()
        self._evidence_samplers.start(receipt.cohort_id, sampler)

    def _pins(
        self,
        account_id: str,
        requested_members: tuple[str, ...],
        offers: list[BotRollCallOffer],
    ) -> tuple[_PinnedCohortMember, ...]:
        runs_by_member = self._visible_runs_by_instance(self._live_runs_root)
        pinned: dict[str, tuple[BotRollCallOffer, dict[str, object]]] = {}
        for offer in offers:
            run = next(
                (item for item in runs_by_member.get(offer.strategy_instance_id, []) if item.get("run_id") == offer.run_id),
                None,
            )
            if run is not None and self._run_account_id(Path(str(run["run_dir"]))) == account_id:
                pinned[offer.strategy_instance_id] = (offer, run)
        requested_member_ids = set(requested_members)
        if not requested_member_ids.issubset(pinned):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "COHORT_CANDIDATES_CHANGED",
                    "message": "The ready cohort changed. Refresh roll call before authorizing a new batch.",
                },
            )
        members: list[_PinnedCohortMember] = []
        for member_id in sorted(requested_member_ids):
            offer, run = pinned[member_id]
            request = self._start_request_for_run(Path(str(run["run_dir"])))
            if request is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "reason_code": "COHORT_START_SETTINGS_INCOMPLETE",
                        "message": "A displayed cohort member has no safe persisted start settings.",
                    },
                )
            members.append(
                _PinnedCohortMember(
                    pin=CohortBatchLaunchMemberPin(
                        strategy_instance_id=member_id,
                        run_id=offer.run_id,
                        roll_call_offer_id=offer.offer_id,
                    ),
                    start_request=request,
                )
            )
        return tuple(members)

    async def _start_member(
        self,
        member: _PinnedCohortMember,
        *,
        cohort_id: str,
    ) -> CohortBatchLaunchMemberOutcome:
        try:
            response = await self._start_run(
                member.pin.run_id,
                member.start_request.model_copy(
                    update={
                        "roll_call_offer_id": member.pin.roll_call_offer_id,
                        "cohort_id": cohort_id,
                    }
                ),
            )
        except HTTPException as exc:
            reason = exc.detail.get("reason_code") if isinstance(exc.detail, dict) else None
            next_safe_action = "Refresh roll call and resolve the backend blocker before authorizing a new cohort."
            if isinstance(exc.detail, dict) and isinstance(exc.detail.get("message"), str):
                next_safe_action = exc.detail["message"]
            elif isinstance(exc.detail, str) and exc.detail:
                next_safe_action = exc.detail
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=member.pin.strategy_instance_id,
                state="blocked",
                reason=reason if isinstance(reason, str) else "COHORT_START_REJECTED",
                next_safe_action=next_safe_action,
            )
        except Exception:
            logger.exception(
                "cohort member start failed unexpectedly",
                extra={
                    "strategy_instance_id": member.pin.strategy_instance_id,
                    "run_id": member.pin.run_id,
                    "cohort_id": cohort_id,
                },
            )
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=member.pin.strategy_instance_id,
                state="blocked",
                reason="COHORT_START_FAILED",
                next_safe_action="Inspect the durable cohort receipt and reconcile this bot before authorizing another cohort.",
            )
        if response.accepted:
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=member.pin.strategy_instance_id,
                state="accepted",
                reason="COHORT_START_ACCEPTED",
                next_safe_action="Monitor the bot receipt state and account exposure.",
            )
        return CohortBatchLaunchMemberOutcome(
            strategy_instance_id=member.pin.strategy_instance_id,
            state="blocked",
            reason="COHORT_START_NOT_ACCEPTED",
            next_safe_action="Review the backend start response before authorizing a new cohort.",
        )


async def resume_open_cohort_launch_schedulers(
    *,
    artifacts_root: Path,
    live_runs_root: Path,
    run_roll_call: RollCall,
    start_run: StartRun,
    visible_runs_by_instance: VisibleRuns,
    run_account_id: RunAccountId,
    start_request_for_run: StartRequestForRun,
    now_ms: NowMs,
    evidence_samplers: CohortEvidenceSamplerRegistry,
    launch_schedulers: CohortLaunchSchedulerRegistry,
) -> None:
    """Rehydrate V2 server schedules before their durable slots are due."""

    coordinator = CohortLaunchCoordinator(
        artifacts_root=artifacts_root,
        live_runs_root=live_runs_root,
        run_roll_call=run_roll_call,
        start_run=start_run,
        visible_runs_by_instance=visible_runs_by_instance,
        run_account_id=run_account_id,
        start_request_for_run=start_request_for_run,
        now_ms=now_ms,
        evidence_samplers=evidence_samplers,
        launch_schedulers=launch_schedulers,
    )
    current_ms = now_ms()
    for account_id in await asyncio.to_thread(_artifact_account_ids, artifacts_root):
        try:
            events = await asyncio.to_thread(read_account_events, artifacts_root, account_id)
        except (OSError, ValueError):
            logger.exception(
                "could not read account events while resuming cohort schedules",
                extra={"account_id": account_id},
            )
            continue
        for receipt in _open_cohort_receipts(events, account_id=account_id, now_ms=current_ms):
            if receipt.schema_version != 2:
                continue
            launch_schedulers.start(
                receipt.cohort_id,
                lambda stop, receipt=receipt: coordinator._run_three_bot_stagger(receipt, stop),
            )


async def resume_open_cohort_evidence_samplers(
    *,
    artifacts_root: Path,
    live_runs_root: Path,
    visible_runs_by_instance: VisibleRuns,
    now_ms: NowMs,
    evidence_samplers: CohortEvidenceSamplerRegistry,
) -> None:
    """Restart server-owned evidence collection for still-open durable receipts.

    A process restart must not silently turn an otherwise active validation
    window into an unobserved one. Any elapsed cadence slots stay absent so the
    certificate's gap detection remains fail-closed; collection resumes at the
    next scheduled slot rather than backdating observations.
    """

    current_ms = now_ms()
    for account_id in await asyncio.to_thread(_artifact_account_ids, artifacts_root):
        try:
            events = await asyncio.to_thread(read_account_events, artifacts_root, account_id)
        except (OSError, ValueError):
            logger.exception(
                "could not read account events while resuming cohort evidence",
                extra={"account_id": account_id},
            )
            continue
        for receipt in _open_cohort_receipts(events, account_id=account_id, now_ms=current_ms):
            if receipt.schema_version == 2:
                outcomes = await CohortBatchLaunchService(artifacts_root=artifacts_root).scheduled_member_outcomes(
                    account_id=account_id,
                    cohort_id=receipt.cohort_id,
                )
                if len(outcomes) != len(receipt.member_schedule) or any(
                    outcome.state != "accepted" for outcome in outcomes.values()
                ):
                    continue
            first_expected_at_ms = _resume_expected_at_ms(receipt, events, current_ms=current_ms)
            if first_expected_at_ms >= receipt.window_end_ms:
                continue
            evidence_samplers.start(
                receipt.cohort_id,
                _evidence_sampler(
                    receipt=receipt,
                    artifacts_root=artifacts_root,
                    live_runs_root=live_runs_root,
                    visible_runs_by_instance=visible_runs_by_instance,
                    now_ms=now_ms,
                    first_expected_at_ms=first_expected_at_ms,
                ),
            )


def _evidence_sampler(
    *,
    receipt: CohortBatchLaunchReceipt,
    artifacts_root: Path,
    live_runs_root: Path,
    visible_runs_by_instance: VisibleRuns,
    now_ms: NowMs,
    first_expected_at_ms: int,
) -> CohortEvidenceSampler:
    service = CohortBatchLaunchService(artifacts_root=artifacts_root)
    observer = CohortEvidenceRuntimeObserver(
        live_runs_root=live_runs_root,
        visible_runs_by_instance=visible_runs_by_instance,
        now_ms=now_ms,
    )
    return CohortEvidenceSampler(
        cadence_ms=_COHORT_EVIDENCE_CADENCE_MS,
        now_ms=now_ms,
        first_expected_at_ms=first_expected_at_ms,
        # ``window_end_ms`` is an exclusive boundary. Each tick credits one
        # cadence, so sampling exactly at the boundary would credit evidence
        # outside the receipt's authorization window.
        last_expected_at_ms=receipt.window_end_ms - _COHORT_EVIDENCE_CADENCE_MS,
        observe=lambda expected_at_ms: observer.observe(receipt, expected_at_ms),
        persist=lambda sample: service.record_evidence_sample(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            sample=sample,
        ),
    )


def _artifact_account_ids(artifacts_root: Path) -> tuple[str, ...]:
    accounts_root = artifacts_root / "accounts"
    try:
        return tuple(
            child.name
            for child in accounts_root.iterdir()
            if child.is_dir() and not child.is_symlink()
        )
    except OSError:
        return ()


def _open_cohort_receipts(
    events: list[dict],
    *,
    account_id: str,
    now_ms: int,
) -> tuple[CohortBatchLaunchReceipt, ...]:
    receipts: dict[str, CohortBatchLaunchReceipt] = {}
    for event in events:
        if event.get("event_type") != "cohort_batch_launch_authorized":
            continue
        try:
            receipt = CohortBatchLaunchReceipt.model_validate(event)
        except ValueError:
            continue
        if receipt.account_id == account_id and (
            (receipt.schema_version == 2 and now_ms < receipt.window_end_ms)
            or (receipt.schema_version == 1 and receipt.window_start_ms <= now_ms < receipt.window_end_ms)
        ):
            receipts[receipt.cohort_id] = receipt
    return tuple(receipts.values())


def _resume_expected_at_ms(
    receipt: CohortBatchLaunchReceipt,
    events: list[dict],
    *,
    current_ms: int,
) -> int:
    observed_ticks = [
        sample.expected_at_ms
        for event in events
        if event.get("event_type") == "cohort_evidence_sample"
        and event.get("cohort_id") == receipt.cohort_id
        and (sample := parse_cohort_evidence_sample(event)) is not None
        and receipt.window_start_ms <= sample.expected_at_ms < receipt.window_end_ms
    ]
    elapsed_ms = max(0, current_ms - receipt.window_start_ms)
    next_after_now = receipt.window_start_ms + (
        (elapsed_ms + _COHORT_EVIDENCE_CADENCE_MS - 1) // _COHORT_EVIDENCE_CADENCE_MS
    ) * _COHORT_EVIDENCE_CADENCE_MS
    next_after_observation = max(
        observed_ticks,
        default=receipt.window_start_ms - _COHORT_EVIDENCE_CADENCE_MS,
    ) + _COHORT_EVIDENCE_CADENCE_MS
    return max(next_after_observation, next_after_now)
