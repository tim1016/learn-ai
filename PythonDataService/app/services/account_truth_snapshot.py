"""In-memory Account Truth snapshot cache for read-side readiness.

The cache is deliberately non-canonical: Account Truth itself is still composed
by the broker endpoint from broker sweeps plus account registry evidence. Bot
status/readiness may consume only the latest cached projection so status reads
do not trigger IBKR I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Literal

from app.broker.ibkr.account_truth_freshness import critical_source_freshness_blocks
from app.schemas.account_truth import AccountTruthResponse
from app.schemas.live_runs import GateResult
from app.utils.timestamps import now_ms_utc

DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS = 60_000
BROKER_TRUTH_SUBMIT_GRACE_MS = 120_000
ACCOUNT_TRUTH_GATE_ID = "account.account_truth"
ACCOUNT_TRUTH_GATE_SOURCE = "account_truth_snapshot"


@dataclass(frozen=True)
class AccountTruthSnapshot:
    """Cached Account Truth projection plus readiness freshness policy."""

    truth: AccountTruthResponse
    cached_at_ms: int
    hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS

    @property
    def account_id(self) -> str | None:
        return self.truth.account_id

    def age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self.cached_at_ms)

    def is_stale(self, now_ms: int) -> bool:
        return self.age_ms(now_ms) > self.hard_ttl_ms

@dataclass(frozen=True)
class AccountTruthUnavailable:
    """Latest Account Truth refresh attempt failed or produced no projection."""

    account_id: str
    attempted_at_ms: int
    detail: str
    hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS

    def age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self.attempted_at_ms)


AccountTruthReadinessEvidence = AccountTruthSnapshot | AccountTruthUnavailable


@dataclass(frozen=True)
class AccountTruthAssessment:
    """Single readiness/gate projection over cached Account Truth evidence."""

    status: Literal["pass", "block"]
    reason_codes: tuple[str, ...]
    primary_reason_code: str | None
    headline: str
    explanation: str
    operator_next_step: str | None
    evidence_at_ms: int
    age_ms: int
    hard_ttl_ms: int

    @property
    def can_submit(self) -> bool:
        return self.status == "pass"

    def to_gate_result(self) -> GateResult:
        return GateResult(
            gate_id=ACCOUNT_TRUTH_GATE_ID,
            status=self.status,
            source=ACCOUNT_TRUTH_GATE_SOURCE,
            operator_reason=self.primary_reason_code or "ACCOUNT_TRUTH_CLEAN",
            operator_next_step=self.operator_next_step,
            evidence_at_ms=self.evidence_at_ms,
        )


class AccountTruthSnapshotProvider:
    """Process-local cache of the latest Account Truth projection by account."""

    def __init__(self, *, hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS) -> None:
        self._hard_ttl_ms = hard_ttl_ms
        self._entries: dict[str, AccountTruthReadinessEvidence] = {}
        self._lock = Lock()

    @property
    def hard_ttl_ms(self) -> int:
        return self._hard_ttl_ms

    def remember(self, truth: AccountTruthResponse, *, cached_at_ms: int | None = None) -> AccountTruthSnapshot | None:
        if truth.account_id is None:
            return None
        snapshot = AccountTruthSnapshot(
            truth=truth,
            cached_at_ms=cached_at_ms if cached_at_ms is not None else now_ms_utc(),
            hard_ttl_ms=self._hard_ttl_ms,
        )
        with self._lock:
            self._entries[truth.account_id.upper()] = snapshot
        return snapshot

    def mark_refresh_failed(
        self,
        account_id: str | None,
        *,
        detail: str,
        attempted_at_ms: int | None = None,
    ) -> AccountTruthUnavailable | None:
        if account_id is None:
            return None
        unavailable = AccountTruthUnavailable(
            account_id=account_id,
            attempted_at_ms=attempted_at_ms if attempted_at_ms is not None else now_ms_utc(),
            detail=detail,
            hard_ttl_ms=self._hard_ttl_ms,
        )
        with self._lock:
            self._entries[account_id.upper()] = unavailable
        return unavailable

    def get(self, account_id: str | None) -> AccountTruthReadinessEvidence | None:
        if account_id is None:
            return None
        with self._lock:
            return self._entries.get(account_id.upper())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class AccountTruthSubmitGrace:
    """Allow a running strategy a bounded continuous broker-truth outage."""

    def __init__(self, *, grace_ms: int = BROKER_TRUTH_SUBMIT_GRACE_MS) -> None:
        self._grace_ms = grace_ms
        self._outage_started_at_ms: int | None = None

    def gate(
        self,
        evidence: AccountTruthReadinessEvidence | None,
        *,
        now_ms: int,
    ) -> GateResult:
        assessment = assess_account_truth(evidence, now_ms=now_ms)
        if assessment.status == "pass":
            self._outage_started_at_ms = None
            return assessment.to_gate_result()
        if not _is_broker_truth_outage(assessment):
            return assessment.to_gate_result()
        if self._outage_started_at_ms is None:
            self._outage_started_at_ms = _broker_truth_outage_started_at_ms(
                evidence,
                assessment=assessment,
                detected_at_ms=now_ms,
            )
        if now_ms < self._outage_started_at_ms:
            return _broker_truth_stale_gate(assessment)
        if now_ms - self._outage_started_at_ms < self._grace_ms:
            return GateResult(
                gate_id=ACCOUNT_TRUTH_GATE_ID,
                status="pass",
                source=ACCOUNT_TRUTH_GATE_SOURCE,
                operator_reason="BROKER_TRUTH_GRACE",
                operator_next_step="WAIT_FOR_BROKER_TRUTH",
                evidence_at_ms=assessment.evidence_at_ms,
            )
        return _broker_truth_stale_gate(assessment)


_PROVIDER = AccountTruthSnapshotProvider()


def get_account_truth_snapshot_provider() -> AccountTruthSnapshotProvider:
    return _PROVIDER


def account_truth_gate_result(
    evidence: AccountTruthReadinessEvidence | None,
    *,
    now_ms: int | None = None,
) -> GateResult:
    """Project the cached Account Truth snapshot into the submit-gate contract."""

    return assess_account_truth(evidence, now_ms=now_ms).to_gate_result()


def assess_account_truth(
    evidence: AccountTruthReadinessEvidence | None,
    *,
    now_ms: int | None = None,
) -> AccountTruthAssessment:
    """Evaluate observation proof once for readiness and submit gates.

    A pass means broker evidence is fresh, attributable, and clean. It permits
    non-zero positions when the Account Truth projection attributes that
    exposure to a known active owner; flatness belongs exclusively to recovery
    proof in :class:`AccountReconciliationReceipt`.
    """

    decided_at_ms = now_ms_utc() if now_ms is None else now_ms
    if evidence is None:
        return AccountTruthAssessment(
            status="block",
            reason_codes=("ACCOUNT_TRUTH_NOT_AVAILABLE",),
            primary_reason_code="ACCOUNT_TRUTH_NOT_AVAILABLE",
            headline="Account Truth snapshot is unavailable",
            explanation="No cached Account Truth projection is available for this account.",
            operator_next_step="Refresh Account Truth from broker evidence before treating submit readiness as safe.",
            evidence_at_ms=decided_at_ms,
            age_ms=0,
            hard_ttl_ms=DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
        )

    if isinstance(evidence, AccountTruthUnavailable):
        return AccountTruthAssessment(
            status="block",
            reason_codes=("ACCOUNT_TRUTH_REFRESH_FAILED",),
            primary_reason_code="ACCOUNT_TRUTH_REFRESH_FAILED",
            headline="Account Truth refresh failed",
            explanation=evidence.detail,
            operator_next_step="Refresh Account Truth from broker evidence before treating submit readiness as safe.",
            evidence_at_ms=evidence.attempted_at_ms,
            age_ms=evidence.age_ms(decided_at_ms),
            hard_ttl_ms=evidence.hard_ttl_ms,
        )

    age_ms = evidence.age_ms(decided_at_ms)
    if evidence.is_stale(decided_at_ms):
        return AccountTruthAssessment(
            status="block",
            reason_codes=("ACCOUNT_TRUTH_STALE",),
            primary_reason_code="ACCOUNT_TRUTH_STALE",
            headline="Account Truth snapshot is stale",
            explanation=(
                f"Account Truth snapshot age is {age_ms} ms; "
                f"hard freshness threshold is {evidence.hard_ttl_ms} ms."
            ),
            operator_next_step="Refresh Account Truth from broker evidence before treating submit readiness as safe.",
            evidence_at_ms=evidence.cached_at_ms,
            age_ms=age_ms,
            hard_ttl_ms=evidence.hard_ttl_ms,
        )
    critical_source_blocks = critical_source_freshness_blocks(
        evidence.truth.source_freshness,
        checked_at_ms=decided_at_ms,
    )
    if critical_source_blocks:
        reason_codes = tuple(
            row.reason_code or f"ACCOUNT_TRUTH_SOURCE_{row.status.upper()}_{row.source.upper()}"
            for row in critical_source_blocks
        )
        first_block = critical_source_blocks[0]
        return AccountTruthAssessment(
            status="block",
            reason_codes=reason_codes,
            primary_reason_code=reason_codes[0],
            headline="Account Truth source evidence is not fresh",
            explanation=first_block.message,
            operator_next_step="Refresh Account Truth from broker evidence before treating submit readiness as safe.",
            evidence_at_ms=first_block.fetched_at_ms or evidence.truth.generated_at_ms,
            age_ms=first_block.age_ms or age_ms,
            hard_ttl_ms=first_block.hard_ttl_ms,
        )
    if evidence.truth.final_verdict != "clean":
        blocker_codes = tuple(
            f"ACCOUNT_TRUTH_{message.code.upper()}"
            for message in evidence.truth.blockers
        )
        reason_codes = ("ACCOUNT_TRUTH_NOT_PROVEN", *blocker_codes)
        first_blocker = evidence.truth.blockers[0].message if evidence.truth.blockers else evidence.truth.status_detail
        return AccountTruthAssessment(
            status="block",
            reason_codes=reason_codes,
            primary_reason_code=blocker_codes[0] if blocker_codes else "ACCOUNT_TRUTH_NOT_PROVEN",
            headline="Account Truth is not clean",
            explanation=first_blocker,
            operator_next_step="Resolve the Account Truth blockers before treating submit readiness as safe.",
            evidence_at_ms=evidence.truth.generated_at_ms,
            age_ms=age_ms,
            hard_ttl_ms=evidence.hard_ttl_ms,
        )
    return AccountTruthAssessment(
        status="pass",
        reason_codes=(),
        primary_reason_code=None,
        headline="Account Truth is clean",
        explanation=evidence.truth.status_detail,
        operator_next_step=None,
        evidence_at_ms=evidence.truth.generated_at_ms,
        age_ms=age_ms,
        hard_ttl_ms=evidence.hard_ttl_ms,
    )


def _is_broker_truth_outage(assessment: AccountTruthAssessment) -> bool:
    return assessment.primary_reason_code in {
        "ACCOUNT_TRUTH_NOT_AVAILABLE",
        "ACCOUNT_TRUTH_REFRESH_FAILED",
        "ACCOUNT_TRUTH_STALE",
    } or any(code.startswith("ACCOUNT_TRUTH_SOURCE_") for code in assessment.reason_codes)


def _broker_truth_outage_started_at_ms(
    evidence: AccountTruthReadinessEvidence | None,
    *,
    assessment: AccountTruthAssessment,
    detected_at_ms: int,
) -> int:
    """Return the earliest durable time at which this outage was known."""

    if isinstance(evidence, AccountTruthUnavailable):
        return evidence.attempted_at_ms
    if isinstance(evidence, AccountTruthSnapshot):
        if assessment.primary_reason_code == "ACCOUNT_TRUTH_STALE":
            return evidence.cached_at_ms + evidence.hard_ttl_ms
        if any(code.startswith("ACCOUNT_TRUTH_SOURCE_") for code in assessment.reason_codes):
            if any("_STALE_" in code for code in assessment.reason_codes):
                return assessment.evidence_at_ms + assessment.hard_ttl_ms
            return assessment.evidence_at_ms
    return detected_at_ms


def _broker_truth_stale_gate(assessment: AccountTruthAssessment) -> GateResult:
    return GateResult(
        gate_id=ACCOUNT_TRUTH_GATE_ID,
        status="block",
        source=ACCOUNT_TRUTH_GATE_SOURCE,
        operator_reason="BROKER_TRUTH_STALE",
        operator_next_step="WAIT_FOR_BROKER_TRUTH",
        evidence_at_ms=assessment.evidence_at_ms,
    )
