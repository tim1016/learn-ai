"""Build and store immutable validation certificates from durable evidence only."""

from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
from typing import TypedDict

from app.engine.live.account_artifacts import CohortBatchLaunchReceipt, account_artifacts_root, read_account_events
from app.engine.live.account_clerk_journal import read_account_clerk_journal
from app.engine.live.journal_exposure import normalize_journal_broker_event, project_journal_exposure
from app.engine.live.live_state_sidecar import _file_lock
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact
from app.schemas.cohort_batch_launch import CohortEvidenceMemberResponse
from app.schemas.cohort_validation_certificate import (
    CohortCertificateRoundTrip,
    CohortCertificateSample,
    CohortValidationCertificate,
)
from app.services.cohort_batch_launch import CohortBatchLaunchService, parse_cohort_evidence_sample


class CohortValidationCertificateService:
    """Certificate boundary: reads evidence; never polls or repairs live state."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    async def generate(self, *, account_id: str, cohort_id: str) -> CohortValidationCertificate:
        events = await asyncio.to_thread(read_account_events, self._artifacts_root, account_id)
        authorization = CohortBatchLaunchService.authorization_event(events, cohort_id)
        if authorization is None:
            raise LookupError(f"cohort receipt not found: {cohort_id}")
        authorization_seq, receipt = authorization
        samples = self._samples(events, authorization_seq, receipt)
        status = await CohortBatchLaunchService(artifacts_root=self._artifacts_root).get_status(
            account_id=account_id,
            cohort_id=cohort_id,
        )
        if status is None:
            raise RuntimeError("newly found cohort receipt could not be projected")
        journal = await asyncio.to_thread(read_account_clerk_journal, self._artifacts_root, account_id)
        namespaces = {pin.strategy_instance_id: pin for pin in receipt.member_pins}
        exposure = project_journal_exposure(journal, account_id=account_id, group_by="namespace")
        # Pins carry run identity, while the Clerk journal owns the namespace.
        member_namespaces = {
            entry.intent.strategy_instance_id: entry.intent.bot_order_namespace
            for entry in journal
            if entry.intent is not None and entry.intent.strategy_instance_id in namespaces
            and entry.intent.run_id == namespaces[entry.intent.strategy_instance_id].run_id
        }
        final_journal_exposure = {
            namespace: {row.symbol: row.quantity}
            for row in exposure
            if row.group_id in set(member_namespaces.values())
            for namespace in (row.group_id,)
        }
        round_trips = _round_trips(journal, set(member_namespaces.values()), final_journal_exposure)
        incidents = _incidents(events, authorization_seq)
        reasons = _certificate_reasons(
            evidence_verdict=status.evidence.verdict,
            evidence_reason=status.evidence.reason,
            samples=samples,
            round_trips=round_trips,
            expected_member_count=len(receipt.member_pins),
            observed_member_namespace_count=len(member_namespaces),
            exposure=final_journal_exposure,
            incidents=incidents,
        )
        latest = samples[-1] if samples else None
        verdict = "passed" if not reasons else "failed" if any(reason.startswith("FAILED_") for reason in reasons) else "incomplete"
        return CohortValidationCertificate(
            account_id=account_id,
            cohort_id=cohort_id,
            member_strategy_instance_ids=list(receipt.member_strategy_instance_ids),
            member_run_ids={pin.strategy_instance_id: pin.run_id for pin in receipt.member_pins},
            window_start_ms=receipt.window_start_ms,
            window_end_ms=receipt.window_end_ms,
            healthy_overlap_ms=status.evidence.healthy_overlap_ms,
            evidence_verdict=status.evidence.verdict,
            evidence_reason=status.evidence.reason,
            samples=samples,
            round_trips=round_trips,
            incidents=incidents,
            final_broker_net_positions=latest.broker_net_positions if latest is not None else None,
            final_broker_residual=latest.broker_residual if latest is not None else None,
            final_journal_exposure=final_journal_exposure,
            verdict=verdict,
            reasons=reasons,
        )

    def write_once(self, certificate: CohortValidationCertificate) -> Path:
        path = self.path_for(certificate.account_id, certificate.cohort_id)
        with _file_lock(path):
            if path.exists():
                raise FileExistsError(f"cohort validation certificate already exists: {certificate.cohort_id}")
            atomic_write_pydantic_artifact(path, certificate)
        return path

    def read(self, *, account_id: str, cohort_id: str) -> CohortValidationCertificate | None:
        return read_pydantic_artifact(self.path_for(account_id, cohort_id), CohortValidationCertificate)

    def path_for(self, account_id: str, cohort_id: str) -> Path:
        digest = hashlib.sha256(cohort_id.encode("utf-8")).hexdigest()
        return account_artifacts_root(self._artifacts_root, account_id) / "cohort_certificates" / f"{digest}.json"

    @staticmethod
    def _samples(
        events: list[dict],
        authorization_seq: int,
        receipt: CohortBatchLaunchReceipt,
    ) -> list[CohortCertificateSample]:
        samples: list[CohortCertificateSample] = []
        for event in events:
            if event.get("event_type") != "cohort_evidence_sample" or event.get("cohort_id") != receipt.cohort_id:
                continue
            if not isinstance(event.get("seq"), int) or event["seq"] <= authorization_seq:
                continue
            sample = parse_cohort_evidence_sample(event)
            if sample is None:
                continue
            samples.append(
                CohortCertificateSample(
                    expected_at_ms=sample.expected_at_ms,
                    observed_at_ms=sample.observed_at_ms,
                    account_truth=sample.account_truth,
                    fleet=sample.fleet,
                    members=[
                        CohortEvidenceMemberResponse(
                            strategy_instance_id=member.strategy_instance_id,
                            run_id=member.run_id,
                            verdict=member.state,
                            reason=member.reason,
                            orders_used=member.orders_used,
                            orders_cap=member.orders_cap,
                        )
                        for member in sample.members
                    ],
                    broker_net_positions=sample.broker_net_positions,
                    broker_residual=sample.broker_residual,
                )
            )
        return samples


class _RoundTripFold(TypedDict):
    refs: set[str]
    order_ids: set[int]
    perm_ids: set[int]
    exec_ids: set[str]
    exposure: float
    saw_nonzero: bool


def _round_trips(journal, namespaces: set[str], exposure: dict[str, dict[str, float]]) -> list[CohortCertificateRoundTrip]:
    rows: dict[str, _RoundTripFold] = {}
    for entry in journal:
        if entry.intent is None or entry.intent.bot_order_namespace not in namespaces:
            continue
        row = rows.setdefault(
            entry.intent.bot_order_namespace,
            {
                "refs": set(),
                "order_ids": set(),
                "perm_ids": set(),
                "exec_ids": set(),
                "exposure": 0.0,
                "saw_nonzero": False,
            },
        )
        row["refs"].add(entry.intent.order_ref)
        if entry.order_id is not None:
            row["order_ids"].add(entry.order_id)
        if entry.perm_id is not None:
            row["perm_ids"].add(entry.perm_id)
        if entry.exec_id:
            row["exec_ids"].add(entry.exec_id)
        broker_event = normalize_journal_broker_event(entry)
        if broker_event is not None:
            if broker_event.order_id is not None:
                row["order_ids"].add(broker_event.order_id)
            if broker_event.perm_id is not None:
                row["perm_ids"].add(broker_event.perm_id)
            if broker_event.exec_id:
                row["exec_ids"].add(broker_event.exec_id)
            if (
                broker_event.event_type == "fill"
                and broker_event.side in {"BUY", "SELL"}
                and broker_event.fill_quantity is not None
                and math.isfinite(float(broker_event.fill_quantity))
            ):
                current = row["exposure"]
                current += float(broker_event.fill_quantity) * (1 if broker_event.side == "BUY" else -1)
                row["exposure"] = current
                row["saw_nonzero"] = row["saw_nonzero"] or current != 0.0
    return [
        CohortCertificateRoundTrip(
            bot_order_namespace=namespace,
            order_refs=sorted(row["refs"]),
            order_ids=sorted(row["order_ids"]),
            perm_ids=sorted(row["perm_ids"]),
            exec_ids=sorted(row["exec_ids"]),
            saw_nonzero_exposure=row["saw_nonzero"],
            closed=row["saw_nonzero"] and row["exposure"] == 0.0 and namespace not in exposure,
        )
        for namespace, row in sorted(rows.items())
    ]


def _incidents(events: list[dict], authorization_seq: int) -> list[str]:
    return sorted({str(event["event_type"]) for event in events if isinstance(event.get("seq"), int) and event["seq"] > authorization_seq and event.get("event_type") in {"account_freeze_recorded", "account_clerk_event_stream_down"}})


def _certificate_reasons(
    *,
    evidence_verdict: str,
    evidence_reason: str | None,
    samples: list[CohortCertificateSample],
    round_trips: list[CohortCertificateRoundTrip],
    expected_member_count: int,
    observed_member_namespace_count: int,
    exposure: dict[str, dict[str, float]],
    incidents: list[str],
) -> list[str]:
    reasons: list[str] = []
    if not samples:
        reasons.append("INCOMPLETE_EVIDENCE_SAMPLES")
    if evidence_verdict == "failed":
        reasons.append(f"FAILED_EVIDENCE_{evidence_reason or evidence_verdict}")
    elif evidence_verdict == "unknown":
        reasons.append(f"INCOMPLETE_EVIDENCE_{evidence_reason or evidence_verdict}")
    if not round_trips:
        reasons.append("INCOMPLETE_ROUND_TRIP_IDENTITY")
    if observed_member_namespace_count != expected_member_count:
        reasons.append("INCOMPLETE_MEMBER_NAMESPACE_IDENTITY")
    if any(
        not row.order_refs
        or not row.order_ids
        or not row.perm_ids
        or not row.exec_ids
        or not row.saw_nonzero_exposure
        or not row.closed
        for row in round_trips
    ):
        reasons.append("INCOMPLETE_ROUND_TRIP_IDENTITY")
    if exposure:
        reasons.append("FAILED_NAMESPACE_EXPOSURE_NONZERO")
    latest = samples[-1] if samples else None
    if latest is None or latest.broker_net_positions is None or latest.broker_residual is None:
        reasons.append("INCOMPLETE_FINAL_BROKER_PROOF")
    elif latest.broker_residual:
        reasons.append("FAILED_BROKER_RESIDUAL_NONZERO")
    if incidents:
        reasons.append("FAILED_INCIDENT_RECORDED")
    return sorted(set(reasons))
