"""Server-owned admission and execution for an account cohort launch."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    CohortBatchLaunchRequestProvenance,
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
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.cohort_evidence import (
    CohortEvidenceSampler,
    CohortEvidenceSamplerRegistry,
)
from app.services.cohort_evidence_runtime import CohortEvidenceRuntimeObserver

RollCall = Callable[[], Awaitable[BotRollCallResponse]]
StartRun = Callable[[str, HostRunnerStartRequest], Awaitable[HostRunnerActionResponse]]
VisibleRuns = Callable[[Path], dict[str, list[dict[str, object]]]]
RunAccountId = Callable[[Path], str | None]
NowMs = Callable[[], int]

_COHORT_EVIDENCE_CADENCE_MS = 5_000
_COHORT_VALIDATION_WINDOW_MS = 60 * 60 * 1_000


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
        now_ms: NowMs,
        evidence_samplers: CohortEvidenceSamplerRegistry,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._live_runs_root = live_runs_root
        self._run_roll_call = run_roll_call
        self._start_run = start_run
        self._visible_runs_by_instance = visible_runs_by_instance
        self._run_account_id = run_account_id
        self._now_ms = now_ms
        self._evidence_samplers = evidence_samplers

    async def launch(
        self,
        *,
        account_id: str,
        requested_members: tuple[str, ...],
        operator_identity: str,
        identity_header_present: bool,
        client_host: str | None,
    ) -> CohortBatchLaunchStatusResponse:
        """Refresh, pin, record, then start without rolling back siblings."""

        roll_call = await self._run_roll_call()
        pins = await asyncio.to_thread(self._pins, account_id, requested_members, roll_call.offers)
        now_ms = self._now_ms()
        receipt = CohortBatchLaunchReceipt(
            account_id=account_id,
            cohort_id=f"paper-validation-{now_ms}-{uuid4().hex[:12]}",
            member_strategy_instance_ids=tuple(pin.strategy_instance_id for pin in pins),
            window_start_ms=now_ms,
            window_end_ms=now_ms + _COHORT_VALIDATION_WINDOW_MS,
            authorized_by=operator_identity,
            recorded_at_ms=now_ms,
            member_pins=pins,
            request_provenance=CohortBatchLaunchRequestProvenance(
                operator_identity_header_present=identity_header_present,
                client_host=client_host,
            ),
        )
        await asyncio.to_thread(record_cohort_batch_launch_receipt, self._artifacts_root, receipt)
        outcomes = await asyncio.gather(
            *(self._start_member(pin, cohort_id=receipt.cohort_id) for pin in pins),
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
        await self._start_evidence_sampler(receipt)
        status_view = await CohortBatchLaunchService(artifacts_root=self._artifacts_root).get_status(
            account_id=account_id,
            cohort_id=receipt.cohort_id,
        )
        if status_view is None:
            raise RuntimeError("newly persisted cohort receipt could not be read")
        return status_view

    async def _start_evidence_sampler(self, receipt: CohortBatchLaunchReceipt) -> None:
        """Persist the first observation and retain a server-owned five-second task."""

        service = CohortBatchLaunchService(artifacts_root=self._artifacts_root)
        observer = CohortEvidenceRuntimeObserver(
            live_runs_root=self._live_runs_root,
            visible_runs_by_instance=self._visible_runs_by_instance,
            now_ms=self._now_ms,
        )
        sampler = CohortEvidenceSampler(
            cadence_ms=_COHORT_EVIDENCE_CADENCE_MS,
            now_ms=self._now_ms,
            first_expected_at_ms=receipt.window_start_ms,
            # ``window_end_ms`` is an exclusive boundary.  Each tick credits
            # one cadence, so sampling exactly at the boundary would credit
            # evidence outside the receipt's authorization window.
            last_expected_at_ms=receipt.window_end_ms - _COHORT_EVIDENCE_CADENCE_MS,
            observe=lambda expected_at_ms: observer.observe(receipt, expected_at_ms),
            persist=lambda sample: service.record_evidence_sample(
                account_id=receipt.account_id,
                cohort_id=receipt.cohort_id,
                sample=sample,
            ),
        )
        await sampler.sample_once()
        self._evidence_samplers.start(receipt.cohort_id, sampler)

    def _pins(
        self,
        account_id: str,
        requested_members: tuple[str, ...],
        offers: list[BotRollCallOffer],
    ) -> tuple[CohortBatchLaunchMemberPin, ...]:
        runs_by_member = self._visible_runs_by_instance(self._live_runs_root)
        pinned: dict[str, tuple[BotRollCallOffer, dict[str, object]]] = {}
        for offer in offers:
            run = next(
                (item for item in runs_by_member.get(offer.strategy_instance_id, []) if item.get("run_id") == offer.run_id),
                None,
            )
            if run is not None and self._run_account_id(Path(str(run["run_dir"]))) == account_id:
                pinned[offer.strategy_instance_id] = (offer, run)
        if set(requested_members) != set(pinned):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "COHORT_CANDIDATES_CHANGED",
                    "message": "The ready cohort changed. Refresh roll call before authorizing a new batch.",
                },
            )
        return tuple(
            CohortBatchLaunchMemberPin(
                strategy_instance_id=member_id,
                run_id=pinned[member_id][0].run_id,
                roll_call_offer_id=pinned[member_id][0].offer_id,
            )
            for member_id in sorted(set(requested_members))
        )

    async def _start_member(
        self,
        pin: CohortBatchLaunchMemberPin,
        *,
        cohort_id: str,
    ) -> CohortBatchLaunchMemberOutcome:
        try:
            response = await self._start_run(
                pin.run_id,
                HostRunnerStartRequest(
                    roll_call_offer_id=pin.roll_call_offer_id,
                    cohort_id=cohort_id,
                ),
            )
        except HTTPException as exc:
            reason = exc.detail.get("reason_code") if isinstance(exc.detail, dict) else None
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=pin.strategy_instance_id,
                state="blocked",
                reason=reason if isinstance(reason, str) else "COHORT_START_REJECTED",
                next_safe_action="Refresh roll call and resolve the backend blocker before authorizing a new cohort.",
            )
        if response.accepted:
            return CohortBatchLaunchMemberOutcome(
                strategy_instance_id=pin.strategy_instance_id,
                state="accepted",
                reason="COHORT_START_ACCEPTED",
                next_safe_action="Monitor the bot receipt state and account exposure.",
            )
        return CohortBatchLaunchMemberOutcome(
            strategy_instance_id=pin.strategy_instance_id,
            state="blocked",
            reason="COHORT_START_NOT_ACCEPTED",
            next_safe_action="Review the backend start response before authorizing a new cohort.",
        )
