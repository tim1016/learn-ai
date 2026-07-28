"""Content-addressed eight-bot custody qualification report assembly.

The direct production-seam exercises live in
``account_custody_qualification_drills`` and the paper-only Clerk adapters in
``account_custody_qualification_fixtures``.  Keeping report assembly separate
makes it impossible for a certificate to hide synthetic drill outcomes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from app.engine.live.account_custody_topology import (
    SUPPORTED_ACCOUNT_CUSTODY_ENTRY_CAPACITY,
    SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE,
    SUPPORTED_ACCOUNT_CUSTODY_RISK_REDUCING_CAPACITY,
)
from app.schemas.account_custody_qualification import (
    INT64_MAX,
    AccountCustodyQualificationCertificate,
    AccountCustodyQualificationDrill,
    AccountCustodyQualificationMetric,
    AccountCustodyQualificationMetrics,
    AccountCustodyQualificationReport,
    account_custody_qualification_payload_sha256,
)
from app.services.account_custody_qualification_drills import (
    CustodyQualificationCampaign,
    run_deterministic_custody_campaign,
)
from app.services.account_custody_qualification_fixtures import DeterministicFaultControls


def nist_r6_percentile(values: Sequence[int], percentile: int) -> float:
    """Return the NIST R6 latency percentile.

    Formula: r = p(n + 1) = k + d; Q_p = x_[k] + d(x_[k+1] - x_[k]).
    Reference: NIST/SEMATECH e-Handbook, §7.2.6.2, Percentiles,
      https://itl.nist.gov/div898/handbook/prc/section2/prc262.htm.
    Canonical implementation: this file.
    Validated against: tests/services/test_account_custody_qualification.py::test_nist_r6_percentiles_match_reference_examples.
    """

    if not values:
        raise ValueError("a percentile requires at least one latency sample")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be within 1..100")
    if any(value < 0 for value in values):
        raise ValueError("latency samples must be nonnegative")
    ordered = sorted(values)
    rank = percentile * (len(ordered) + 1) / 100
    if rank <= 1:
        return float(ordered[0])
    if rank >= len(ordered):
        return float(ordered[-1])
    lower_rank = math.floor(rank)
    fractional = rank - lower_rank
    lower = ordered[lower_rank - 1]
    upper = ordered[lower_rank]
    return lower + fractional * (upper - lower)


def summarize_latency_samples(
    samples: Mapping[str, Sequence[int]],
    *,
    source_receipt_refs: Mapping[str, Sequence[str]] | None = None,
) -> tuple[AccountCustodyQualificationMetric, ...]:
    """Build stable receipt-interval metrics from exact observed timestamps."""

    receipt_refs = source_receipt_refs or {}
    return tuple(
        AccountCustodyQualificationMetric(
            name=name,
            sample_count=len(values),
            p50_ms=nist_r6_percentile(values, 50) if values else None,
            p95_ms=nist_r6_percentile(values, 95) if values else None,
            p99_ms=nist_r6_percentile(values, 99) if values else None,
            source_receipt_refs=tuple(receipt_refs.get(name, (f"deterministic-custody-metric:{name}",))),
            sample_trace_sha256=_sample_trace_sha256(values),
        )
        for name, values in sorted(samples.items())
    )


def run_deterministic_account_custody_qualification(
    *,
    account_id: str,
    generated_at_ms: int,
    controls: DeterministicFaultControls = DeterministicFaultControls(),
) -> AccountCustodyQualificationReport:
    """Run every deterministic drill without opening an IBKR connection."""

    if not 0 <= generated_at_ms <= INT64_MAX:
        raise ValueError("generated_at_ms must be an int64 UTC epoch millisecond")
    if not account_id:
        raise ValueError("account_id is required")
    campaign = run_deterministic_custody_campaign(controls)
    metrics = _metrics(campaign)
    certificate = _certificate(campaign.drills)
    base_payload = {
        "schema_version": 1,
        "generated_at_ms": generated_at_ms,
        "account_id": account_id,
        "execution_mode": "DETERMINISTIC",
        "broker_dependency": "NONE",
        "fleet_size": SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE,
        "entry_capacity": SUPPORTED_ACCOUNT_CUSTODY_ENTRY_CAPACITY,
        "risk_reducing_capacity": SUPPORTED_ACCOUNT_CUSTODY_RISK_REDUCING_CAPACITY,
        "drills": [drill.model_dump(mode="json") for drill in campaign.drills],
        "metrics": metrics.model_dump(mode="json"),
        "certificate": certificate.model_dump(mode="json"),
    }
    return AccountCustodyQualificationReport(
        **base_payload,
        report_sha256=account_custody_qualification_payload_sha256(base_payload),
    )


def verify_account_custody_qualification_report(report: AccountCustodyQualificationReport) -> bool:
    """Verify the report SHA-256 against its complete semantic payload."""

    payload = report.model_dump(mode="json", exclude={"report_sha256"})
    return report.report_sha256 == account_custody_qualification_payload_sha256(payload)


def _metrics(campaign: CustodyQualificationCampaign) -> AccountCustodyQualificationMetrics:
    """Report only non-overlapping intervals read from campaign receipts."""

    phase_samples = {
        "receipt_request_to_intake_admission": campaign.queue_wait_samples_ms,
        "receipt_intake_admission_to_inbox_fsync": campaign.fsync_samples_ms,
        "receipt_a0_recorded_to_a1_submitting": campaign.a0_to_a1_dispatch_samples_ms,
        "receipt_a1_submitting_to_callback": campaign.a1_to_callback_samples_ms,
        "receipt_callback_to_ack": campaign.callback_to_ack_samples_ms,
        "receipt_epoch_notice_to_recovery": campaign.epoch_recovery_samples_ms,
    }
    return AccountCustodyQualificationMetrics(
        phase_latencies=summarize_latency_samples(
            phase_samples,
            source_receipt_refs={
                "receipt_request_to_intake_admission": ("deterministic-custody-drill:1",),
                "receipt_intake_admission_to_inbox_fsync": ("deterministic-custody-drill:2",),
                "receipt_a0_recorded_to_a1_submitting": ("deterministic-custody-drill:3",),
                "receipt_a1_submitting_to_callback": ("deterministic-custody-drill:6",),
                "receipt_callback_to_ack": ("deterministic-custody-drill:6",),
                "receipt_epoch_notice_to_recovery": ("deterministic-custody-drill:11",),
            },
        ),
        queue_high_water=campaign.queue_high_water,
        queue_refusal_count=campaign.queue_refusal_count,
        epoch_recovery_ms=max(campaign.epoch_recovery_samples_ms) if campaign.epoch_recovery_samples_ms else None,
        max_uncertain_intent_age_ms=campaign.max_uncertain_intent_age_ms,
        projection_gap_count=campaign.projection_gap_count,
    )


def _certificate(
    drills: Sequence[AccountCustodyQualificationDrill],
) -> AccountCustodyQualificationCertificate:
    failed_ids = tuple(drill.drill_id for drill in drills if not drill.passed)
    if failed_ids:
        status = "FAILED"
    else:
        status = "DETERMINISTIC_PASSED_AWAITING_PAPER"
    return AccountCustodyQualificationCertificate(
        status=status,
        deterministic_drill_count=len(drills),
        passed_drill_count=len(drills) - len(failed_ids),
        failed_drill_ids=failed_ids,
        paper_environment_status="NOT_RUN",
    )


def _sample_trace_sha256(values: Sequence[int]) -> str:
    """Hash the exact timing sample sequence behind one reported percentile."""

    return account_custody_qualification_payload_sha256({"samples_ms": list(values)})
