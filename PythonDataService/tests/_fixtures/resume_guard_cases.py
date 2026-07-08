"""Shared parameterized table of Resume / Pause / Stop guard cases
(PRD #616).

Consumed by:

- ``tests/services/test_resume_guards.py`` — composed-resolver layer.
- ``tests/services/test_operator_surface.py`` — capability projection.
- ``tests/integration/test_resume_guard_entrypoints.py`` — entry-point
  consistency assertion (capability projection, mutation endpoint, CLI
  ``cmd_resume``).

The table is the single source of truth for *which artifact-state
combinations produce which decision + reason codes*; each consumer
exercises its surface against every row so a future refactor cannot
drift behaviour between entry points.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.resume_guard_state import (
    BrokerSafetyArtifact,
    ReconciliationArtifact,
    SubmissionCapabilityArtifact,
    UncertainIntentArtifact,
)

# PRD #619-A — default capability fixture: ``live_paper`` declared and
# the child constructed with ``readonly=False`` (the common-case
# paper-execution path). New cases that exercise the capability gate
# use the explicit BLOCKED / UNKNOWN fixtures below.
_CAPABILITY_SATISFIED = SubmissionCapabilityArtifact(
    state="SATISFIED",
    declared_submit_mode="live_paper",
    readonly_at_start=False,
)
_CAPABILITY_BLOCKED = SubmissionCapabilityArtifact(
    state="BLOCKED",
    declared_submit_mode="live_paper",
    readonly_at_start=True,
    detail="declared live_paper but child readonly=True",
)
_CAPABILITY_UNKNOWN = SubmissionCapabilityArtifact(
    state="UNKNOWN",
    detail="run_status.json absent",
)


@dataclass(frozen=True)
class GuardCase:
    """One artifact-combination → expected ``allow_resume`` + ``reason_codes``.

    ``current_intent`` is the durable desired state at the time of the
    operator click; ``poisoned`` is the run's poisoned-sentinel state.
    Both layer above the artifact guards in the capability evaluator.
    """

    name: str
    broker_safety: BrokerSafetyArtifact
    reconciliation: ReconciliationArtifact
    uncertain_intent: UncertainIntentArtifact
    # PRD #619-A — new fourth gate. Defaults to SATISFIED so existing
    # rows continue to express "capability not blocking"; rows that
    # specifically exercise the capability gate pass an explicit
    # BLOCKED or UNKNOWN.
    submission_capability: SubmissionCapabilityArtifact = field(
        default=_CAPABILITY_SATISFIED
    )
    current_intent: str | None = None
    poisoned: bool = False
    # Expected output for the *artifact-only* layer (the
    # ``ResumeGuardState`` resolver) — does NOT include intent-state
    # or poisoned codes (those are layered by the capability
    # evaluator).
    expected_allow_resume: bool = True
    expected_reason_codes: tuple[str, ...] = ()
    # Expected output for the *capability projection* — includes the
    # intent-state-pair and poisoned overlays.
    expected_resume_enabled: bool = True
    expected_resume_codes: tuple[str, ...] = ()
    expected_pause_enabled: bool = True
    expected_pause_codes: tuple[str, ...] = ()
    expected_stop_enabled: bool = True
    expected_stop_codes: tuple[str, ...] = ()
    # Mutation endpoint decision for `resume` (HTTP 200 vs 409) keyed
    # off ``expected_resume_enabled`` — explicit for clarity.
    expected_mutation_status: int = field(init=False, default=200)

    def __post_init__(self) -> None:  # pragma: no cover - dataclass derived
        object.__setattr__(
            self,
            "expected_mutation_status",
            200 if self.expected_resume_enabled else 409,
        )


_SAFE = BrokerSafetyArtifact(state="SAFE", verdict="paper-only")
_UNSAFE = BrokerSafetyArtifact(state="UNSAFE", verdict="unsafe")
_BROKER_UNKNOWN = BrokerSafetyArtifact(state="UNKNOWN", verdict="unknown")
_RECON_OK = ReconciliationArtifact(state="NOT_AVAILABLE")  # writer not wired yet
_RECON_FAILED = ReconciliationArtifact(state="FAILED", detail="net != explained")
_RECON_STALE = ReconciliationArtifact(state="STALE", detail="receipt < runtime")
_RECON_UNKNOWN = ReconciliationArtifact(state="UNKNOWN", detail="corrupt")
_INTENT_CLEAR = UncertainIntentArtifact(state="CLEAR")
_INTENT_PRESENT = UncertainIntentArtifact(state="PRESENT", unresolved_intent_ids=("intent-uncertain-a",))
_INTENT_UNKNOWN = UncertainIntentArtifact(state="UNKNOWN")


GUARD_CASES: list[GuardCase] = [
    GuardCase(
        name="all_clean_paused_resume_allowed",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=True,
        expected_resume_enabled=True,
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="broker_unsafe_blocks_resume",
        broker_safety=_UNSAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("BROKER_SAFETY_UNSAFE",),
        expected_resume_enabled=False,
        expected_resume_codes=("BROKER_SAFETY_UNSAFE",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="broker_unknown_blocks_resume",
        broker_safety=_BROKER_UNKNOWN,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("BROKER_SAFETY_UNKNOWN",),
        expected_resume_enabled=False,
        expected_resume_codes=("BROKER_SAFETY_UNKNOWN",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="uncertain_intent_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_PRESENT,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("UNRESOLVED_UNCERTAIN_INTENT",),
        expected_resume_enabled=False,
        expected_resume_codes=("UNRESOLVED_UNCERTAIN_INTENT",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="uncertain_intent_unknown_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_UNKNOWN,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("UNCERTAIN_INTENT_STATE_UNKNOWN",),
        expected_resume_enabled=False,
        expected_resume_codes=("UNCERTAIN_INTENT_STATE_UNKNOWN",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="reconciliation_failed_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_FAILED,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("RECONCILIATION_FAILED",),
        expected_resume_enabled=False,
        expected_resume_codes=("RECONCILIATION_FAILED",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="reconciliation_stale_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_STALE,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("RECONCILIATION_STALE",),
        expected_resume_enabled=False,
        expected_resume_codes=("RECONCILIATION_STALE",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="reconciliation_unknown_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_UNKNOWN,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("RECONCILIATION_UNKNOWN",),
        expected_resume_enabled=False,
        expected_resume_codes=("RECONCILIATION_UNKNOWN",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="multiple_guards_priority_order",
        broker_safety=_UNSAFE,
        reconciliation=_RECON_FAILED,
        uncertain_intent=_INTENT_PRESENT,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=(
            "BROKER_SAFETY_UNSAFE",
            "UNRESOLVED_UNCERTAIN_INTENT",
            "RECONCILIATION_FAILED",
        ),
        expected_resume_enabled=False,
        expected_resume_codes=(
            "BROKER_SAFETY_UNSAFE",
            "UNRESOLVED_UNCERTAIN_INTENT",
            "RECONCILIATION_FAILED",
        ),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="intent_running_resume_desired_state_already_running",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="RUNNING",
        expected_allow_resume=True,
        expected_reason_codes=(),
        expected_resume_enabled=False,
        expected_resume_codes=("DESIRED_STATE_ALREADY_RUNNING",),
        expected_pause_enabled=True,
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="intent_absent_resume_default_running",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent=None,
        expected_allow_resume=True,
        expected_reason_codes=(),
        expected_resume_enabled=False,
        expected_resume_codes=("DESIRED_STATE_DEFAULT_RUNNING",),
        expected_pause_enabled=True,
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="intent_stopped_resume_allowed_to_clear_latch",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="STOPPED",
        expected_allow_resume=True,
        expected_reason_codes=(),
        expected_resume_enabled=True,
        expected_resume_codes=(),
        expected_pause_enabled=False,
        expected_pause_codes=("STOPPED_REQUIRES_REDEPLOY",),
        expected_stop_enabled=False,
        expected_stop_codes=("ALREADY_STOPPED",),
    ),
    GuardCase(
        name="poisoned_resume_requires_redeploy",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        current_intent="PAUSED",
        poisoned=True,
        expected_allow_resume=True,
        expected_reason_codes=(),
        expected_resume_enabled=False,
        expected_resume_codes=("REDEPLOY_REQUIRED",),
        expected_pause_enabled=False,
        expected_pause_codes=("REDEPLOY_REQUIRED", "ALREADY_PAUSED"),
        expected_stop_enabled=False,
        expected_stop_codes=("REDEPLOY_REQUIRED",),
    ),
    # ── PRD #619-A — submission-capability gate ───────────────────────
    GuardCase(
        name="capability_blocked_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        submission_capability=_CAPABILITY_BLOCKED,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("SUBMISSION_CAPABILITY_BLOCKED",),
        expected_resume_enabled=False,
        expected_resume_codes=("SUBMISSION_CAPABILITY_BLOCKED",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        name="capability_unknown_blocks_resume",
        broker_safety=_SAFE,
        reconciliation=_RECON_OK,
        uncertain_intent=_INTENT_CLEAR,
        submission_capability=_CAPABILITY_UNKNOWN,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=("SUBMISSION_CAPABILITY_UNKNOWN",),
        expected_resume_enabled=False,
        expected_resume_codes=("SUBMISSION_CAPABILITY_UNKNOWN",),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
    GuardCase(
        # Capability priority is below identity but above uncertain
        # intent and reconciliation — assert the sort order.
        name="identity_unsafe_outranks_capability_blocked",
        broker_safety=_UNSAFE,
        reconciliation=_RECON_FAILED,
        uncertain_intent=_INTENT_PRESENT,
        submission_capability=_CAPABILITY_BLOCKED,
        current_intent="PAUSED",
        expected_allow_resume=False,
        expected_reason_codes=(
            "BROKER_SAFETY_UNSAFE",
            "SUBMISSION_CAPABILITY_BLOCKED",
            "UNRESOLVED_UNCERTAIN_INTENT",
            "RECONCILIATION_FAILED",
        ),
        expected_resume_enabled=False,
        expected_resume_codes=(
            "BROKER_SAFETY_UNSAFE",
            "SUBMISSION_CAPABILITY_BLOCKED",
            "UNRESOLVED_UNCERTAIN_INTENT",
            "RECONCILIATION_FAILED",
        ),
        expected_pause_enabled=False,
        expected_pause_codes=("ALREADY_PAUSED",),
        expected_stop_enabled=True,
    ),
]
