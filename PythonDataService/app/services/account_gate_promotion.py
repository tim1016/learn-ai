"""Evidence-gated promotion from Account Truth to Clerk-keyed proof.

``IbkrSettings.account_gate_authority`` is a requested deployment authority,
not a permission to bypass the promotion evidence.  The resolver is used at
every enforcement boundary, so a prematurely changed environment variable
keeps the proven Account Truth path active and reports exactly what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.engine.live.account_artifacts import (
    AccountClerkLeaseUnavailableError,
    append_account_event,
    read_account_events,
    read_active_accepting_account_clerk_generation,
)
from app.engine.live.account_identity import normalize_account_id
from app.schemas.live_runs import GateResult
from app.services.observation_lease_parity import (
    AccountObservationLeaseParityReport,
    assess_observation_lease_shadow_parity_from_artifacts,
)

AccountGateAuthority = Literal["account_truth", "observation_lease"]
AccountGatePromotionState = Literal[
    "SAFE_DEFAULT",
    "WAITING_FOR_SHADOW_PARITY",
    "WAITING_FOR_CLERK_RESTART_SMOKE",
    "CLERK_PROOF_ACTIVE",
]

ACCOUNT_CLERK_RESTART_SMOKE_EVENT = "account_clerk_restart_smoke_confirmed"
ACCOUNT_CLERK_RESTART_SMOKE_SCHEMA_VERSION = 1
CLERK_RESTART_SMOKE_CONFIRMATION = "CLERK_RESTART_SMOKE"


@dataclass(frozen=True)
class ClerkRestartSmokeEvidence:
    """One operator-confirmed post-restart Clerk smoke for a generation."""

    recorded_at_ms: int
    clerk_generation: int


@dataclass(frozen=True)
class AccountGatePromotionResolution:
    """Requested and effective gate authority plus durable promotion evidence."""

    requested_authority: AccountGateAuthority
    effective_authority: AccountGateAuthority
    state: AccountGatePromotionState
    reason_code: str
    disposition: str | None
    parity: AccountObservationLeaseParityReport | None
    restart_smoke: ClerkRestartSmokeEvidence | None


@dataclass(frozen=True)
class AccountActionGateResolution:
    """The single current-proof decision for an account action boundary.

    Promotion establishes which proof is eligible in general. This result
    applies the non-negotiable same-action no-weaker rule and selects the
    actual gate the caller must enforce.
    """

    authority: AccountGateAuthority
    gate: GateResult | None
    current_clerk_proof_is_weaker: bool


class AccountGatePromotionError(ValueError):
    """The requested evidence cannot safely record a Clerk promotion fact."""


def resolve_account_gate_authority(
    artifacts_root: Path,
    *,
    account_id: str,
    requested_authority: AccountGateAuthority,
    now_ms: int,
) -> AccountGatePromotionResolution:
    """Return the only authority an account action may enforce.

    Setting the global requested authority to ``observation_lease`` cannot
    activate the new path until the three-session no-weaker proof and a smoke
    for the *current* accepting Clerk generation both exist.  A later Clerk
    restart invalidates that smoke automatically.
    """

    canonical_account_id = normalize_account_id(account_id)
    if requested_authority == "account_truth":
        return _resolution(
            requested_authority=requested_authority,
            effective_authority="account_truth",
            state="SAFE_DEFAULT",
            reason_code="ACCOUNT_GATE_SAFE_DEFAULT",
            disposition=None,
            parity=None,
            restart_smoke=None,
        )

    parity = assess_observation_lease_shadow_parity_from_artifacts(
        artifacts_root,
        canonical_account_id,
    )
    active_generation = _active_generation(artifacts_root, canonical_account_id, now_ms=now_ms)
    restart_smoke = _current_restart_smoke(
        artifacts_root,
        canonical_account_id,
        active_generation=active_generation,
    )
    if not parity.cutover_ready:
        return _resolution(
            requested_authority=requested_authority,
            effective_authority="account_truth",
            state="WAITING_FOR_SHADOW_PARITY",
            reason_code="ACCOUNT_GATE_PROMOTION_EVIDENCE_INCOMPLETE",
            disposition="COMPLETE_THREE_SESSION_SHADOW_PARITY",
            parity=parity,
            restart_smoke=restart_smoke,
        )
    if restart_smoke is None:
        return _resolution(
            requested_authority=requested_authority,
            effective_authority="account_truth",
            state="WAITING_FOR_CLERK_RESTART_SMOKE",
            reason_code="ACCOUNT_GATE_CLERK_RESTART_SMOKE_REQUIRED",
            disposition="RESTART_CLERK_AND_RECORD_SMOKE",
            parity=parity,
            restart_smoke=None,
        )
    return _resolution(
        requested_authority=requested_authority,
        effective_authority="observation_lease",
        state="CLERK_PROOF_ACTIVE",
        reason_code="ACCOUNT_GATE_CLERK_PROOF_ACTIVE",
        disposition=None,
        parity=parity,
        restart_smoke=restart_smoke,
    )


def clerk_proof_is_active(
    artifacts_root: Path,
    account_id: str,
    requested_authority: AccountGateAuthority,
    now_ms: int,
) -> bool:
    """Return whether a boundary may enforce Clerk-keyed account proof."""

    return (
        resolve_account_gate_authority(
            artifacts_root,
            account_id=account_id,
            requested_authority=requested_authority,
            now_ms=now_ms,
        ).effective_authority
        == "observation_lease"
    )


def effective_account_gate_authority_for_current_evidence(
    promoted_authority: AccountGateAuthority,
    *,
    account_truth_gate_status: str | None,
    observation_lease_gate_status: str | None,
) -> AccountGateAuthority:
    """Fail closed when a current Clerk proof is weaker than Account Truth.

    Historical shadow parity qualifies promotion, but a divergence observed at
    this very action boundary must not receive a one-submit grace period.  The
    proven Account Truth path owns that action and the durable comparison then
    invalidates future promotion until the corpus is requalified.
    """

    if (
        promoted_authority == "observation_lease"
        and account_truth_gate_status == "block"
        and observation_lease_gate_status == "pass"
    ):
        return "account_truth"
    return promoted_authority


def resolve_action_gate(
    promoted_authority: AccountGateAuthority,
    *,
    account_truth_gate: GateResult | None,
    observation_lease_gate: GateResult | None,
) -> AccountActionGateResolution:
    """Resolve the one proof an account action must enforce right now.

    Every action boundary must make this selection identically: a current
    Clerk lease cannot pass an action that current Account Truth blocks, even
    after durable parity has promoted the lease path.
    """

    truth_status = None if account_truth_gate is None else account_truth_gate.status
    lease_status = None if observation_lease_gate is None else observation_lease_gate.status
    current_clerk_proof_is_weaker = (
        promoted_authority == "observation_lease"
        and truth_status == "block"
        and lease_status == "pass"
    )
    authority = effective_account_gate_authority_for_current_evidence(
        promoted_authority,
        account_truth_gate_status=truth_status,
        observation_lease_gate_status=lease_status,
    )
    return AccountActionGateResolution(
        authority=authority,
        gate=observation_lease_gate if authority == "observation_lease" else account_truth_gate,
        current_clerk_proof_is_weaker=current_clerk_proof_is_weaker,
    )


def record_clerk_restart_smoke(
    artifacts_root: Path,
    *,
    account_id: str,
    confirmation: str,
    recorded_at_ms: int,
) -> ClerkRestartSmokeEvidence:
    """Durably record the typed smoke for the current accepting Clerk only."""

    if confirmation != CLERK_RESTART_SMOKE_CONFIRMATION:
        raise AccountGatePromotionError("CLERK_RESTART_SMOKE_CONFIRMATION_REQUIRED")
    canonical_account_id = normalize_account_id(account_id)
    generation = _active_generation(artifacts_root, canonical_account_id, now_ms=recorded_at_ms)
    if generation is None:
        raise AccountGatePromotionError("ACCOUNT_CLERK_RESTART_SMOKE_REQUIRES_ACCEPTING_CLERK")
    evidence = ClerkRestartSmokeEvidence(
        recorded_at_ms=recorded_at_ms,
        clerk_generation=generation,
    )
    append_account_event(
        artifacts_root,
        canonical_account_id,
        {
            "event_type": ACCOUNT_CLERK_RESTART_SMOKE_EVENT,
            "schema_version": ACCOUNT_CLERK_RESTART_SMOKE_SCHEMA_VERSION,
            "recorded_at_ms": evidence.recorded_at_ms,
            "clerk_generation": evidence.clerk_generation,
        },
    )
    return evidence


def _active_generation(artifacts_root: Path, account_id: str, *, now_ms: int) -> int | None:
    try:
        active = read_active_accepting_account_clerk_generation(
            artifacts_root,
            account_id,
            now_ms=now_ms,
        )
    except (AccountClerkLeaseUnavailableError, OSError, ValueError):
        return None
    return None if active is None else active.generation


def _current_restart_smoke(
    artifacts_root: Path,
    account_id: str,
    *,
    active_generation: int | None,
) -> ClerkRestartSmokeEvidence | None:
    """Return the most recent typed smoke only if it names today's Clerk."""

    if active_generation is None:
        return None
    latest: ClerkRestartSmokeEvidence | None = None
    for event in read_account_events(artifacts_root, account_id):
        if event.get("event_type") != ACCOUNT_CLERK_RESTART_SMOKE_EVENT:
            continue
        if event.get("schema_version") != ACCOUNT_CLERK_RESTART_SMOKE_SCHEMA_VERSION:
            continue
        recorded_at_ms = event.get("recorded_at_ms")
        clerk_generation = event.get("clerk_generation")
        if (
            not isinstance(recorded_at_ms, int)
            or isinstance(recorded_at_ms, bool)
            or recorded_at_ms < 0
            or not isinstance(clerk_generation, int)
            or isinstance(clerk_generation, bool)
            or clerk_generation != active_generation
        ):
            continue
        candidate = ClerkRestartSmokeEvidence(
            recorded_at_ms=recorded_at_ms,
            clerk_generation=clerk_generation,
        )
        if latest is None or candidate.recorded_at_ms > latest.recorded_at_ms:
            latest = candidate
    return latest


def _resolution(
    *,
    requested_authority: AccountGateAuthority,
    effective_authority: AccountGateAuthority,
    state: AccountGatePromotionState,
    reason_code: str,
    disposition: str | None,
    parity: AccountObservationLeaseParityReport | None,
    restart_smoke: ClerkRestartSmokeEvidence | None,
) -> AccountGatePromotionResolution:
    return AccountGatePromotionResolution(
        requested_authority=requested_authority,
        effective_authority=effective_authority,
        state=state,
        reason_code=reason_code,
        disposition=disposition,
        parity=parity,
        restart_smoke=restart_smoke,
    )


__all__ = [
    "ACCOUNT_CLERK_RESTART_SMOKE_EVENT",
    "CLERK_RESTART_SMOKE_CONFIRMATION",
    "AccountActionGateResolution",
    "AccountGateAuthority",
    "AccountGatePromotionError",
    "AccountGatePromotionResolution",
    "ClerkRestartSmokeEvidence",
    "clerk_proof_is_active",
    "effective_account_gate_authority_for_current_evidence",
    "record_clerk_restart_smoke",
    "resolve_account_gate_authority",
    "resolve_action_gate",
]
