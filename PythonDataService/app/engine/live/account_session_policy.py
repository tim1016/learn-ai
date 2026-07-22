"""Account-level live-session policy and durable live-feed evidence.

The exchange calendar owns *scheduled* session structure.  A recent live-feed
observation is a separate operational fact: a calendar cannot know about a
halt, a data-farm outage, or a lost subscription.  This module combines the
two only at the account-action boundary and deliberately defaults to refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import account_artifacts_root
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.live_state_sidecar import _file_lock
from app.lean_sidecar.trading_calendar import session_state_at_ms
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact
from app.schemas.live_runs import GateResult

ACCOUNT_SESSION_POLICY_FILENAME = "account_session_policy.json"
ACCOUNT_LIVE_FEED_EVIDENCE_FILENAME = "account_live_feed_evidence.json"
ACCOUNT_SESSION_POLICY_SCHEMA_VERSION = 1
ACCOUNT_LIVE_FEED_EVIDENCE_SCHEMA_VERSION = 1
ACCOUNT_LIVE_FEED_MAX_AGE_MS = 120_000
ACCOUNT_LIVE_SESSION_GATE_ID = "account.live_session"
ACCOUNT_LIVE_SESSION_GATE_SOURCE = "account_session_policy"


class AccountSessionPolicy(BaseModel):
    """The sole per-account exception to the normal live-session rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    allow_outside_live_session: bool = False
    updated_at_ms: int = Field(ge=0)


class AccountLiveFeedEvidence(BaseModel):
    """A live engine's recent observation that its market-data feed is alive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    observed_at_ms: int = Field(ge=0)
    source: Literal["live_engine"] = "live_engine"


class AccountLiveSessionAssessment(BaseModel):
    """One backend-authored tradability verdict for submit and flatten."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason_code: str
    disposition: str
    scheduled_session_state: Literal["RTH_OPEN", "CLOSED"]
    live_feed_observed_at_ms: int | None = Field(default=None, ge=0)
    evidence_at_ms: int = Field(ge=0)
    outside_live_session_override: bool

    def to_gate_result(self) -> GateResult:
        return GateResult(
            gate_id=ACCOUNT_LIVE_SESSION_GATE_ID,
            status="pass" if self.allowed else "block",
            source=ACCOUNT_LIVE_SESSION_GATE_SOURCE,
            operator_reason=self.reason_code,
            operator_next_step=None if self.allowed else self.disposition,
            evidence_at_ms=self.evidence_at_ms,
        )


def account_session_policy_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the durable policy path for one canonical account."""

    canonical_account_id = normalize_account_id(account_id)
    return account_artifacts_root(artifacts_root, canonical_account_id) / ACCOUNT_SESSION_POLICY_FILENAME


def account_live_feed_evidence_path(artifacts_root: Path, account_id: str) -> Path:
    """Return the durable live-feed evidence path for one canonical account."""

    canonical_account_id = normalize_account_id(account_id)
    return account_artifacts_root(artifacts_root, canonical_account_id) / ACCOUNT_LIVE_FEED_EVIDENCE_FILENAME


def read_account_session_policy(artifacts_root: Path, account_id: str) -> AccountSessionPolicy:
    """Read policy without making a missing file look like an unsafe override."""

    canonical_account_id = normalize_account_id(account_id)
    policy = read_pydantic_artifact(
        account_session_policy_path(artifacts_root, canonical_account_id),
        AccountSessionPolicy,
    )
    if policy is None:
        return AccountSessionPolicy(account_id=canonical_account_id, updated_at_ms=0)
    if policy.account_id != canonical_account_id:
        raise ValueError("account session policy belongs to a different account")
    return policy


def write_account_session_policy(
    artifacts_root: Path,
    *,
    account_id: str,
    allow_outside_live_session: bool,
    updated_at_ms: int,
) -> AccountSessionPolicy:
    """Persist the explicit account-level outside-session override."""

    canonical_account_id = normalize_account_id(account_id)
    policy = AccountSessionPolicy(
        account_id=canonical_account_id,
        allow_outside_live_session=allow_outside_live_session,
        updated_at_ms=updated_at_ms,
    )
    atomic_write_pydantic_artifact(account_session_policy_path(artifacts_root, canonical_account_id), policy)
    return policy


def write_account_live_feed_evidence(
    artifacts_root: Path,
    *,
    account_id: str,
    observed_at_ms: int,
) -> AccountLiveFeedEvidence:
    """Record monotonic live-feed evidence under one account-scoped writer lock.

    Several runners may observe the same account. Serializing the read/fold/
    write keeps the newest observation authoritative, while malformed existing
    evidence remains a visible durable-artifact fault instead of being hidden
    by a replacement write.
    """

    canonical_account_id = normalize_account_id(account_id)
    path = account_live_feed_evidence_path(artifacts_root, canonical_account_id)
    with _file_lock(path, trusted_root=account_artifacts_root(artifacts_root, canonical_account_id)):
        current = read_account_live_feed_evidence(artifacts_root, canonical_account_id)
        evidence = AccountLiveFeedEvidence(
            account_id=canonical_account_id,
            observed_at_ms=max(observed_at_ms, 0 if current is None else current.observed_at_ms),
        )
        atomic_write_pydantic_artifact(path, evidence)
        return evidence


def read_account_live_feed_evidence(
    artifacts_root: Path,
    account_id: str,
) -> AccountLiveFeedEvidence | None:
    """Read live-feed evidence strictly for diagnostic/account-desk projections.

    Missing evidence is an ordinary unproven-live-session state.  Present but
    malformed evidence is a durable-artifact fault, which callers that surface
    operator evidence must not silently collapse into "missing".
    """

    canonical_account_id = normalize_account_id(account_id)
    path = account_live_feed_evidence_path(artifacts_root, canonical_account_id)
    if not path.exists():
        return None
    evidence = AccountLiveFeedEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    if evidence.account_id != canonical_account_id:
        raise ValueError("account live-feed evidence belongs to a different account")
    return evidence


def assess_account_live_session(
    artifacts_root: Path,
    *,
    account_id: str,
    now_ms: int,
) -> AccountLiveSessionAssessment:
    """Require both canonical RTH structure and fresh live-feed evidence.

    The explicit account override is intentionally the only bypass.  It does
    not live in a bot configuration, so one bot can never silently select a
    weaker gate than its account peers.
    """

    canonical_account_id = normalize_account_id(account_id)
    policy = read_account_session_policy(artifacts_root, canonical_account_id)
    evidence = read_account_live_feed_evidence(artifacts_root, canonical_account_id)

    scheduled_state = session_state_at_ms(now_ms)
    scheduled_open = scheduled_state == "RTH_OPEN"
    observed_at_ms = evidence.observed_at_ms if evidence is not None else None
    live_feed_fresh = (
        observed_at_ms is not None
        and observed_at_ms <= now_ms
        and now_ms - observed_at_ms <= ACCOUNT_LIVE_FEED_MAX_AGE_MS
    )
    if policy.allow_outside_live_session:
        return AccountLiveSessionAssessment(
            allowed=True,
            reason_code="OUTSIDE_LIVE_SESSION_OVERRIDE_ENABLED",
            disposition="ACCOUNT_SESSION_OVERRIDE_ACTIVE",
            scheduled_session_state="RTH_OPEN" if scheduled_open else "CLOSED",
            live_feed_observed_at_ms=observed_at_ms,
            evidence_at_ms=observed_at_ms if observed_at_ms is not None else now_ms,
            outside_live_session_override=True,
        )
    if not scheduled_open:
        return AccountLiveSessionAssessment(
            allowed=False,
            reason_code="OUTSIDE_LIVE_TRADABLE_SESSION",
            disposition="WAIT_FOR_LIVE_TRADABLE_SESSION",
            scheduled_session_state="CLOSED",
            live_feed_observed_at_ms=observed_at_ms,
            evidence_at_ms=now_ms,
            outside_live_session_override=False,
        )
    if not live_feed_fresh:
        return AccountLiveSessionAssessment(
            allowed=False,
            reason_code="LIVE_SESSION_LIVENESS_UNPROVEN",
            disposition="RESTORE_LIVE_FEED_AND_WAIT_FOR_FRESH_EVIDENCE",
            scheduled_session_state="RTH_OPEN",
            live_feed_observed_at_ms=observed_at_ms,
            evidence_at_ms=observed_at_ms if observed_at_ms is not None else now_ms,
            outside_live_session_override=False,
        )
    return AccountLiveSessionAssessment(
        allowed=True,
        reason_code="LIVE_TRADABLE_SESSION_VERIFIED",
        disposition="GATE_PASSING",
        scheduled_session_state="RTH_OPEN",
        live_feed_observed_at_ms=observed_at_ms,
        evidence_at_ms=observed_at_ms,
        outside_live_session_override=False,
    )


__all__ = [
    "ACCOUNT_LIVE_FEED_EVIDENCE_FILENAME",
    "ACCOUNT_LIVE_FEED_MAX_AGE_MS",
    "ACCOUNT_LIVE_SESSION_GATE_ID",
    "ACCOUNT_LIVE_SESSION_GATE_SOURCE",
    "ACCOUNT_SESSION_POLICY_FILENAME",
    "AccountLiveFeedEvidence",
    "AccountLiveSessionAssessment",
    "AccountSessionPolicy",
    "account_live_feed_evidence_path",
    "account_session_policy_path",
    "assess_account_live_session",
    "read_account_live_feed_evidence",
    "read_account_session_policy",
    "write_account_live_feed_evidence",
    "write_account_session_policy",
]
