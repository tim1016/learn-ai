"""Durable authorization service for deliberate cohort launches."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    append_account_event,
    read_account_events,
)
from app.schemas.cohort_batch_launch import (
    CohortBatchLaunchStatusResponse,
    CohortEvidenceMemberResponse,
    CohortEvidenceSummaryResponse,
)
from app.services.cohort_evidence import CohortEvidenceSample, CohortMemberSample, evaluate_healthy_overlap
from app.utils.timestamps import now_ms_utc

_COHORT_EVIDENCE_EVENT_TYPE = "cohort_evidence_sample"
_COHORT_EVIDENCE_CADENCE_MS = 5_000


class CohortBatchLaunchService:
    """Writes operator-authorized cohort launch receipts to account events."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    async def get_status(
        self,
        *,
        account_id: str,
        cohort_id: str | None,
        events: list[dict] | None = None,
    ) -> CohortBatchLaunchStatusResponse | None:
        """Read the latest durable cohort state without inferring missing outcomes."""

        if events is None:
            events = await asyncio.to_thread(read_account_events, self._artifacts_root, account_id)
        authorization = self.authorization_event(events, cohort_id)
        if authorization is None:
            if cohort_id is None:
                return None
            raise LookupError(f"cohort receipt not found: {cohort_id}")
        authorization_seq, receipt = authorization
        outcomes_receipt, outcomes_error = self._outcomes_after_authorization(
            events,
            authorization_seq=authorization_seq,
            receipt=receipt,
        )
        return CohortBatchLaunchStatusResponse.from_receipts(
            receipt,
            outcomes_receipt,
            outcomes_error=outcomes_error,
            evidence=self._evidence_summary(events, authorization_seq, receipt),
        )

    async def record_evidence_sample(
        self,
        *,
        account_id: str,
        cohort_id: str,
        sample: CohortEvidenceSample,
    ) -> None:
        """Persist one server sampler tick; browser reads never call this path."""

        await asyncio.to_thread(
            append_account_event,
            self._artifacts_root,
            account_id,
            {
                "event_type": _COHORT_EVIDENCE_EVENT_TYPE,
                "cohort_id": cohort_id,
                "expected_at_ms": sample.expected_at_ms,
                "observed_at_ms": sample.observed_at_ms,
                "account_truth": sample.account_truth,
                "fleet": sample.fleet,
                "broker_net_positions": sample.broker_net_positions,
                "broker_residual": sample.broker_residual,
                "members": [
                    {
                        "strategy_instance_id": member.strategy_instance_id,
                        "run_id": member.run_id,
                        "state": member.state,
                        "reason": member.reason,
                        "orders_used": member.orders_used,
                        "orders_cap": member.orders_cap,
                    }
                    for member in sample.members
                ],
            },
        )

    async def record_scheduled_member_outcome(
        self,
        *,
        account_id: str,
        cohort_id: str,
        outcome: CohortBatchLaunchMemberOutcome,
        recorded_at_ms: int,
    ) -> None:
        """Append one immutable V2 staggered-start outcome."""

        await asyncio.to_thread(
            append_account_event,
            self._artifacts_root,
            account_id,
            {
                "event_type": "cohort_batch_launch_member_start_recorded",
                "cohort_id": cohort_id,
                "recorded_at_ms": recorded_at_ms,
                **outcome.model_dump(mode="json"),
            },
        )

    async def scheduled_member_outcomes(
        self,
        *,
        account_id: str,
        cohort_id: str,
    ) -> dict[str, CohortBatchLaunchMemberOutcome]:
        """Read the V2 per-slot outcomes without pretending a partial cohort is complete."""

        events = await asyncio.to_thread(read_account_events, self._artifacts_root, account_id)
        outcomes: dict[str, CohortBatchLaunchMemberOutcome] = {}
        for event in events:
            if event.get("event_type") != "cohort_batch_launch_member_start_recorded" or event.get("cohort_id") != cohort_id:
                continue
            outcome = CohortBatchLaunchMemberOutcome.model_validate(
                {
                    "strategy_instance_id": event.get("strategy_instance_id"),
                    "state": event.get("state"),
                    "reason": event.get("reason"),
                    "next_safe_action": event.get("next_safe_action"),
                }
            )
            if outcome.strategy_instance_id in outcomes:
                raise ValueError("duplicate staggered cohort member outcome")
            outcomes[outcome.strategy_instance_id] = outcome
        return outcomes

    @staticmethod
    def _evidence_summary(
        events: list[dict],
        authorization_seq: int,
        receipt: CohortBatchLaunchReceipt,
    ) -> CohortEvidenceSummaryResponse:
        samples: list[CohortEvidenceSample] = []
        for event in events:
            if event.get("event_type") != _COHORT_EVIDENCE_EVENT_TYPE or event.get("cohort_id") != receipt.cohort_id:
                continue
            try:
                if int(event["seq"]) <= authorization_seq:
                    continue
            except (KeyError, TypeError, ValueError):
                return _unreadable_evidence_summary()
            sample = parse_cohort_evidence_sample(event)
            if sample is None:
                return _unreadable_evidence_summary()
            samples.append(sample)
        evaluation = evaluate_healthy_overlap(
            tuple(samples),
            member_strategy_instance_ids=receipt.member_strategy_instance_ids,
            cadence_ms=_COHORT_EVIDENCE_CADENCE_MS,
            # The launch window is end-exclusive: the sampler's final valid
            # expected tick is one cadence before the boundary.  Evaluating
            # at the exclusive endpoint would manufacture a missing tick.
            evaluated_at_ms=min(
                now_ms_utc(),
                receipt.window_end_ms - _COHORT_EVIDENCE_CADENCE_MS,
            ),
        )
        return CohortEvidenceSummaryResponse(
            sample_count=len(samples),
            cadence_ms=_COHORT_EVIDENCE_CADENCE_MS,
            healthy_overlap_ms=evaluation.healthy_overlap_ms,
            verdict=evaluation.verdict,
            reason=evaluation.reason,
            source="account_event.cohort_evidence_sample",
            members=[
                CohortEvidenceMemberResponse(
                    strategy_instance_id=member.strategy_instance_id,
                    run_id=member.run_id,
                    verdict=member.state,
                    reason=member.reason,
                    orders_used=member.orders_used,
                    orders_cap=member.orders_cap,
                )
                for member in samples[-1].members
            ]
            if samples
            else [],
        )

    @staticmethod
    def authorization_event(
        events: list[dict],
        cohort_id: str | None,
    ) -> tuple[int, CohortBatchLaunchReceipt] | None:
        for event in reversed(events):
            if event.get("event_type") != "cohort_batch_launch_authorized":
                continue
            if cohort_id is not None and event.get("cohort_id") != cohort_id:
                continue
            try:
                return int(event["seq"]), CohortBatchLaunchReceipt.model_validate(event)
            except (KeyError, TypeError, ValueError, ValidationError):
                identifier = cohort_id or event.get("cohort_id") or "latest"
                raise ValueError(f"cohort authorization is unreadable: {identifier}") from None
        return None

    @staticmethod
    def _outcomes_after_authorization(
        events: list[dict],
        *,
        authorization_seq: int,
        receipt: CohortBatchLaunchReceipt,
    ) -> tuple[CohortBatchLaunchOutcomesReceipt | None, str | None]:
        expected_members = set(receipt.member_strategy_instance_ids)
        if receipt.schema_version == 2:
            return CohortBatchLaunchService._scheduled_outcomes_after_authorization(
                events,
                authorization_seq=authorization_seq,
                receipt=receipt,
            )
        for event in reversed(events):
            if event.get("event_type") != "cohort_batch_launch_outcomes_recorded":
                continue
            if event.get("cohort_id") != receipt.cohort_id:
                continue
            try:
                if int(event["seq"]) <= authorization_seq:
                    continue
                outcomes = CohortBatchLaunchOutcomesReceipt.model_validate(event)
                actual_members = {outcome.strategy_instance_id for outcome in outcomes.outcomes}
                if outcomes.account_id != receipt.account_id or actual_members != expected_members:
                    return None, "The persisted cohort outcomes do not match this authorization receipt."
                return outcomes, None
            except (KeyError, TypeError, ValueError, ValidationError):
                return None, "The persisted cohort outcomes are unreadable."
        return None, None

    @staticmethod
    def _scheduled_outcomes_after_authorization(
        events: list[dict],
        *,
        authorization_seq: int,
        receipt: CohortBatchLaunchReceipt,
    ) -> tuple[CohortBatchLaunchOutcomesReceipt | None, str | None]:
        """Project immutable per-slot V2 outcomes into the shared status shape."""

        expected_members = set(receipt.member_strategy_instance_ids)
        outcomes_by_member: dict[str, CohortBatchLaunchMemberOutcome] = {}
        recorded_at_ms = 0
        for event in events:
            if event.get("event_type") != "cohort_batch_launch_member_start_recorded":
                continue
            if event.get("cohort_id") != receipt.cohort_id:
                continue
            try:
                if int(event["seq"]) <= authorization_seq:
                    continue
                recorded_at = event["recorded_at_ms"]
                if not _is_int_not_bool(recorded_at):
                    return None, "A persisted staggered cohort outcome has no valid timestamp."
                outcome = CohortBatchLaunchMemberOutcome.model_validate(
                    {
                        "strategy_instance_id": event.get("strategy_instance_id"),
                        "state": event.get("state"),
                        "reason": event.get("reason"),
                        "next_safe_action": event.get("next_safe_action"),
                    }
                )
            except (KeyError, TypeError, ValueError, ValidationError):
                return None, "A persisted staggered cohort outcome is unreadable."
            member_id = outcome.strategy_instance_id
            if member_id not in expected_members or member_id in outcomes_by_member:
                return None, "The persisted staggered cohort outcomes do not match this authorization receipt."
            outcomes_by_member[member_id] = outcome
            recorded_at_ms = max(recorded_at_ms, recorded_at)
        if not outcomes_by_member:
            return None, None
        return (
            CohortBatchLaunchOutcomesReceipt(
                schema_version=2,
                account_id=receipt.account_id,
                cohort_id=receipt.cohort_id,
                outcomes=tuple(
                    outcomes_by_member[member_id]
                    for member_id in receipt.member_strategy_instance_ids
                    if member_id in outcomes_by_member
                ),
                recorded_at_ms=recorded_at_ms,
            ),
            None,
        )


def parse_cohort_evidence_sample(event: dict) -> CohortEvidenceSample | None:
    expected_at_ms = event.get("expected_at_ms")
    observed_at_ms = event.get("observed_at_ms")
    account_truth = event.get("account_truth")
    fleet = event.get("fleet")
    members = event.get("members")
    broker_net_positions = _position_map(event.get("broker_net_positions"))
    broker_residual = _position_map(event.get("broker_residual"))
    if (
        not _is_int_not_bool(expected_at_ms)
        or (observed_at_ms is not None and not _is_int_not_bool(observed_at_ms))
        or account_truth not in {"healthy", "failed", "unknown"}
        or fleet not in {"healthy", "failed", "unknown"}
        or not isinstance(members, list)
        or (event.get("broker_net_positions") is not None and broker_net_positions is None)
        or (event.get("broker_residual") is not None and broker_residual is None)
    ):
        return None
    parsed_members: list[CohortMemberSample] = []
    for member in members:
        if not isinstance(member, dict):
            return None
        strategy_instance_id = member.get("strategy_instance_id")
        run_id = member.get("run_id")
        state = member.get("state")
        reason = member.get("reason")
        orders_used = member.get("orders_used")
        orders_cap = member.get("orders_cap")
        if (
            not isinstance(strategy_instance_id, str)
            or (run_id is not None and not isinstance(run_id, str))
            or state not in {"healthy", "failed", "unknown"}
            or (reason is not None and not isinstance(reason, str))
            or (orders_used is not None and (not _is_int_not_bool(orders_used) or orders_used < 0))
            or (orders_cap is not None and (not _is_int_not_bool(orders_cap) or orders_cap <= 0))
        ):
            return None
        parsed_members.append(
            CohortMemberSample(
                strategy_instance_id,
                run_id,
                state,
                reason,
                orders_used,
                orders_cap,
            )
        )
    return CohortEvidenceSample(
        expected_at_ms,
        observed_at_ms,
        account_truth,
        fleet,
        tuple(parsed_members),
        broker_net_positions,
        broker_residual,
    )


def _position_map(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(symbol, str) or not _is_int_not_bool(quantity) for symbol, quantity in value.items()
    ):
        return None
    return {symbol: quantity for symbol, quantity in value.items()}


def _is_int_not_bool(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unreadable_evidence_summary() -> CohortEvidenceSummaryResponse:
    return CohortEvidenceSummaryResponse(
        sample_count=0,
        cadence_ms=_COHORT_EVIDENCE_CADENCE_MS,
        healthy_overlap_ms=0,
        verdict="failed",
        reason="COHORT_EVIDENCE_UNREADABLE",
        source="account_event.cohort_evidence_sample",
        members=[],
    )
