"""Backend-authored trader copy for lifecycle chart actions.

Slice 3 of the per-bot lifecycle workbench moves action reason prose off
Angular. The lifecycle chart still carries raw codes as receipts, but the
primary operator copy comes from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from app.schemas.live_runs import HostProcessStartDisabledReasonCode
from app.services.operator_capability import REASON_CODES

REDEPLOY_PROOF_MISSING = "REDEPLOY_PROOF_MISSING"


@dataclass(frozen=True)
class LifecycleActionReason:
    """Trader-facing reason payload for one lifecycle chart action."""

    code: str | None
    headline: str
    detail: str


_ACTION_REASON_COPY: dict[str, tuple[str, str]] = {
    "MUTATION_UNRESOLVED_START": (
        "Prior Start outcome unproven",
        "A prior Start attempt has not been proven complete. Reconcile the mutation outcome before retrying.",
    ),
    "MUTATION_UNRESOLVED_STOP": (
        "Prior Stop outcome unproven",
        "A prior Stop attempt has not been proven complete. Reconcile the mutation outcome before retrying.",
    ),
    "MUTATION_UNRESOLVED_FLATTEN": (
        "Prior flatten outcome unproven",
        "A prior Flatten-and-pause attempt has not been proven complete. Reconcile the mutation outcome before retrying.",
    ),
    "MUTATION_UNRESOLVED_RESUME": (
        "Prior Resume outcome unproven",
        "A prior Resume attempt has not been proven complete. Reconcile the mutation outcome before retrying.",
    ),
    "OUTCOME_UNKNOWN": (
        "Mutation outcome unknown",
        "The mutation transport returned an ambiguous outcome. Reconcile the prior attempt before sending another command.",
    ),
    "NO_LIVE_BINDING": (
        "No live binding",
        "The host runner is not bound to this instance. Start a runner before sending a live command.",
    ),
    "NO_OWNED_POSITIONS": (
        "Nothing to flatten",
        "The broker reports no owned positions for this bot, so Flatten and pause has no live position to close.",
    ),
    "ALREADY_POISONED": (
        "Already poisoned",
        "This run is already marked POISONED. Recovery requires a fresh deployment.",
    ),
    "ALREADY_STOPPED": (
        "Already stopped",
        "STOPPED is a terminal state for this run. Revival requires Redeploy.",
    ),
    "POSTURE_DEMOTED": (
        "Runtime evidence is stale",
        "Control-plane or runtime freshness is stale, so live actions are held until fresh evidence returns.",
    ),
    "BROKER_SAFETY_UNSAFE": (
        "Broker safety is unsafe",
        "The backend detected non-paper or otherwise unsafe broker evidence. Resume is held until paper-only safety is proven.",
    ),
    "BROKER_SAFETY_UNKNOWN": (
        "Broker safety is unknown",
        "The backend cannot prove the broker is paper-only. Resume is held until broker safety is proven.",
    ),
    "SUBMISSION_CAPABILITY_BLOCKED": (
        "Submit capability blocked",
        "The declared submit mode and the run's readonly setting do not satisfy the run contract.",
    ),
    "SUBMISSION_CAPABILITY_UNKNOWN": (
        "Submit capability unknown",
        "Durable child or run evidence cannot prove whether this run may submit orders.",
    ),
    "RECONCILIATION_FAILED": (
        "Reconciliation failed",
        "The latest reconciliation receipt reports a divergence. Reconcile before resuming.",
    ),
    "RECONCILIATION_STALE": (
        "Reconciliation is stale",
        "The latest reconciliation receipt predates current run or broker evidence. Reconcile again before resuming.",
    ),
    "RECONCILIATION_NOT_AVAILABLE": (
        "Reconciliation unavailable",
        "No reconciliation receipt is available yet. Treat the missing receipt as a block before resuming.",
    ),
    "RECONCILIATION_UNKNOWN": (
        "Reconciliation unreadable",
        "The reconciliation receipt is unreadable or malformed. Resume is held until a clean receipt is available.",
    ),
    "UNRESOLVED_UNCERTAIN_INTENT": (
        "Submit outcome uncertain",
        "An uncertain submit intent is unresolved in the WAL. Reconcile before resuming or retrying.",
    ),
    "UNCERTAIN_INTENT_STATE_UNKNOWN": (
        "Uncertain-intent state unknown",
        "The durable uncertain-intent state cannot be read. Resume is held until the WAL is observable.",
    ),
    "ALREADY_RUNNING": (
        "Already running",
        "The host process is already running, so Start bot process would be a no-op.",
    ),
    "DESIRED_STATE_ALREADY_RUNNING": (
        "Desired state already running",
        "Resume only changes durable desired state, and desired_state is already RUNNING. If no runtime is bound, use Start bot process.",
    ),
    "DESIRED_STATE_DEFAULT_RUNNING": (
        "Desired state defaults to running",
        "No durable desired_state is recorded, so Resume would leave the effective default RUNNING. If no runtime is bound, use Start bot process.",
    ),
    "ALREADY_PAUSED": (
        "Already paused",
        "The effective desired state is already PAUSED, so Pause would be a no-op.",
    ),
    "STOPPED_REQUIRES_REDEPLOY": (
        "Redeploy required",
        "This run is dead or retired. Recovery requires a fresh redeploy.",
    ),
    "STOPPED_REQUIRES_RESUME": (
        "Resume required",
        "This bot is stopped. Use Resume to clear the durable stop latch before starting.",
    ),
    "REDEPLOY_REQUIRED": (
        "Redeploy required",
        "This run is dead or poisoned. Recovery requires a fresh deployment.",
    ),
    "ACCOUNT_FROZEN": (
        "Account frozen",
        "An account-wide freeze is active. Resolve the freeze before starting or resuming this bot.",
    ),
    "ACCOUNT_EVIDENCE_STALE": (
        "Account verification overdue",
        "The connected account is missing fresh verification. Reconcile the account before starting this bot.",
    ),
    "CRASH_RECOVERY_REQUIRED": (
        "Recovery proof required",
        "The previous host runner crashed. Record audited recovery evidence before restarting this bot.",
    ),
    "STOPPING": (
        "Stopping",
        "The host runner is stopping. Wait for it to finish before starting again.",
    ),
    "HOST_SERVICE_OFFLINE": (
        "Host service offline",
        "The host service is offline. Start or reconnect the host runner first.",
    ),
    "START_SETTINGS_INCOMPLETE": (
        "Start settings incomplete",
        "Start settings are incomplete or invalid. Redeploy with the missing settings.",
    ),
    REDEPLOY_PROOF_MISSING: (
        "Redeploy proof unavailable",
        "Redeploy requires stored deployment proof for this bot before the workbench can create a fresh run.",
    ),
}

LIFECYCLE_ACTION_REASON_CODES: frozenset[str] = frozenset(_ACTION_REASON_COPY)


def lifecycle_action_reason_for_code(
    code: str | None,
    *,
    enabled: bool = False,
    enabled_detail: str = "Backend gates currently allow this action.",
    disabled_fallback_detail: str = "Backend gates currently block this action.",
) -> LifecycleActionReason:
    """Return backend-authored trader copy for a lifecycle action."""

    if enabled:
        return LifecycleActionReason(
            code=None,
            headline="Available",
            detail=enabled_detail,
        )
    if code is None:
        return LifecycleActionReason(
            code=None,
            headline="Action unavailable",
            detail=disabled_fallback_detail,
        )
    copy = _ACTION_REASON_COPY.get(code)
    if copy is None:
        return LifecycleActionReason(
            code=code,
            headline="Action unavailable",
            detail=(
                "The backend emitted an unrecognized action reason code. "
                "Use the receipt code and runbook to diagnose it."
            ),
        )
    headline, detail = copy
    return LifecycleActionReason(code=code, headline=headline, detail=detail)


def expected_lifecycle_action_reason_codes() -> frozenset[str]:
    """Return every code Slice 3 expects backend action prose to cover."""

    return frozenset(
        set(REASON_CODES)
        | set(get_args(HostProcessStartDisabledReasonCode))
        | {REDEPLOY_PROOF_MISSING}
    )
