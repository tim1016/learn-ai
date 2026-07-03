"""In-memory Account Truth snapshot cache for read-side readiness.

The cache is deliberately non-canonical: Account Truth itself is still composed
by the broker endpoint from broker sweeps plus account registry evidence. Bot
status/readiness may consume only the latest cached projection so status reads
do not trigger IBKR I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from app.schemas.account_truth import AccountTruthResponse
from app.schemas.live_runs import GateResult
from app.utils.timestamps import now_ms_utc

DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS = 60_000
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
        return max(0, now_ms - self.truth.generated_at_ms)

    def is_stale(self, now_ms: int) -> bool:
        return self.age_ms(now_ms) > self.hard_ttl_ms

    def blocking_reason_codes(self, now_ms: int) -> tuple[str, ...]:
        if self.is_stale(now_ms):
            return ("ACCOUNT_TRUTH_STALE",)
        if self.truth.final_verdict != "clean":
            blocker_codes = tuple(
                f"ACCOUNT_TRUTH_{message.code.upper()}"
                for message in self.truth.blockers
            )
            return ("ACCOUNT_TRUTH_NOT_PROVEN", *blocker_codes)
        return ()

    def primary_blocking_reason_code(self, now_ms: int) -> str | None:
        if self.is_stale(now_ms):
            return "ACCOUNT_TRUTH_STALE"
        if self.truth.final_verdict != "clean" and self.truth.blockers:
            return f"ACCOUNT_TRUTH_{self.truth.blockers[0].code.upper()}"
        if self.truth.final_verdict != "clean":
            return "ACCOUNT_TRUTH_NOT_PROVEN"
        return None


class AccountTruthSnapshotProvider:
    """Process-local cache of the latest Account Truth projection by account."""

    def __init__(self, *, hard_ttl_ms: int = DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS) -> None:
        self._hard_ttl_ms = hard_ttl_ms
        self._snapshots: dict[str, AccountTruthSnapshot] = {}
        self._lock = Lock()

    def remember(self, truth: AccountTruthResponse, *, cached_at_ms: int | None = None) -> AccountTruthSnapshot | None:
        if truth.account_id is None:
            return None
        snapshot = AccountTruthSnapshot(
            truth=truth,
            cached_at_ms=cached_at_ms if cached_at_ms is not None else now_ms_utc(),
            hard_ttl_ms=self._hard_ttl_ms,
        )
        with self._lock:
            self._snapshots[truth.account_id.upper()] = snapshot
        return snapshot

    def get(self, account_id: str | None) -> AccountTruthSnapshot | None:
        if account_id is None:
            return None
        with self._lock:
            return self._snapshots.get(account_id.upper())

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()


_PROVIDER = AccountTruthSnapshotProvider()


def get_account_truth_snapshot_provider() -> AccountTruthSnapshotProvider:
    return _PROVIDER


def account_truth_gate_result(
    snapshot: AccountTruthSnapshot | None,
    *,
    now_ms: int | None = None,
) -> GateResult:
    """Project the cached Account Truth snapshot into the submit-gate contract."""

    decided_at_ms = now_ms_utc() if now_ms is None else now_ms
    if snapshot is None:
        return GateResult(
            gate_id=ACCOUNT_TRUTH_GATE_ID,
            status="block",
            source=ACCOUNT_TRUTH_GATE_SOURCE,
            operator_reason="ACCOUNT_TRUTH_NOT_AVAILABLE",
            operator_next_step="REFRESH_ACCOUNT_TRUTH",
            evidence_at_ms=decided_at_ms,
        )

    reason = snapshot.primary_blocking_reason_code(decided_at_ms)
    if reason is None:
        return GateResult(
            gate_id=ACCOUNT_TRUTH_GATE_ID,
            status="pass",
            source=ACCOUNT_TRUTH_GATE_SOURCE,
            operator_reason="ACCOUNT_TRUTH_CLEAN",
            operator_next_step=None,
            evidence_at_ms=snapshot.truth.generated_at_ms,
        )

    next_step = "REFRESH_ACCOUNT_TRUTH" if reason == "ACCOUNT_TRUTH_STALE" else "RESOLVE_ACCOUNT_TRUTH_BLOCKERS"
    return GateResult(
        gate_id=ACCOUNT_TRUTH_GATE_ID,
        status="block",
        source=ACCOUNT_TRUTH_GATE_SOURCE,
        operator_reason=reason,
        operator_next_step=next_step,
        evidence_at_ms=snapshot.truth.generated_at_ms,
    )
