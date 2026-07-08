"""Shared action capability evaluator (PRD #607 / Slice 1 / #608,
extended by PRD #616).

Single Python function that answers "is this action allowed right now?"
for the five canonical cockpit actions (ADR-0010): Resume, Pause, Stop,
Flatten-and-pause, Mark-poisoned.  Called from BOTH the operator-surface
status projection and every relevant mutation endpoint so that a stale
status snapshot cannot be exploited to drive a mutation past the same
eligibility rule.

PRD #616 reversed the prior "Resume/Pause are NEVER disabled by the
server" design.  Resume, Pause, and Stop now consume the shared
``ResumeGuardState`` resolver:

- ``resume`` is refused when the broker safety verdict is UNSAFE /
  UNKNOWN, when an uncertain intent is present / unknown, when the
  reconciliation receipt has failed / gone stale / is unreadable, OR
  when the current durable intent is already RUNNING (no-op,
  ``DESIRED_STATE_ALREADY_RUNNING``), when missing desired-state evidence
  defaults the effective intent to RUNNING
  (``DESIRED_STATE_DEFAULT_RUNNING``), or when the run is poisoned
  (``REDEPLOY_REQUIRED``). When durable intent is STOPPED, Resume is the
  explicit operator unlatch that writes RUNNING after the same artifact
  guards pass.
- ``pause`` is refused symmetrically: ``ALREADY_PAUSED`` when current
  intent is PAUSED, ``STOPPED_REQUIRES_REDEPLOY`` when STOPPED.  The
  broker-safety / reconciliation / uncertain-intent guards do NOT
  apply to Pause — pausing only stops new entries; it is always safe
  to make the bot stop trading.
- ``stop`` is permitted when the instance is not already STOPPED
  (``ALREADY_STOPPED``) and not poisoned (``REDEPLOY_REQUIRED`` —
  poisoned instances need a redeploy, not a stop on the already-dead
  binding); Stop does not consult the artifact guards because stop is
  the safe verb.

``effect`` flips between ``LIVE_ACTUATION`` (a daemon is bound and
running) and ``DURABLE_ONLY`` (no live binding — the write still
succeeds but does not actuate until the host runner starts).

``flatten_and_pause`` requires live actuation (FLATTEN_NOW cannot
execute without a bound runner).  Without a binding it is disabled
with ``NO_LIVE_BINDING``.

``mark_poisoned`` rejects on ``NO_LIVE_BINDING`` and (when the
poisoned sentinel is already set) on ``ALREADY_POISONED``.

The reason-code vocabulary is closed and documented; the Frontend
maintains a typed lookup mapping each code to operator-language copy.
"""

from __future__ import annotations

from typing import Literal

from app.schemas.live_runs import (
    ActionCapability,
    DesiredStateView,
    InstanceProcessView,
    LiveBinding,
    OperatorSurfaceActions,
)
from app.services.mutation_attempt import MutationAttempt
from app.services.resume_guard_state import (
    RESUME_REASON_CODES,
    ResumeGuardState,
    empty_guard_state,
    sort_reason_codes,
)
from app.services.runtime_freshness import RuntimeFreshness

ActionName = Literal["resume", "pause", "stop", "flatten_and_pause", "mark_poisoned"]


# PRD #619-D action-conflict matrix.  Reads as: "if the latest
# mutation attempt for this instance was an unresolved ``prior``, then
# evaluating any action in ``blocked_actions`` adds ``reason_code``
# to the disabled-reasons list."  Priors not listed below (Pause,
# Start, Mark-poisoned) never block any action that ``evaluate_action``
# governs — Pause is goal-idempotent, Start blocks other Start /
# Redeploy at the routing layer (619-D2 router-level enforcement is
# left to a follow-up), Mark-poisoned is incident recovery.
#
# An attempt is "resolved" iff its ``dispatch_state`` is
# ``EFFECT_CONFIRMED``.  Every other state — including the three
# non-confirmed terminals (EFFECT_NOT_OBSERVED, NOT_PROVABLE,
# EVIDENCE_CONFLICT) — means we cannot prove the prior mutation
# completed and the matrix stays engaged until Reconcile (619-D3)
# advances the attempt to EFFECT_CONFIRMED.
_MUTATION_CONFLICT_MATRIX: dict[str, tuple[frozenset[ActionName], str]] = {
    "stop": (frozenset({"resume", "stop"}), "MUTATION_UNRESOLVED_STOP"),
    "flatten": (frozenset({"flatten_and_pause"}), "MUTATION_UNRESOLVED_FLATTEN"),
    "resume": (frozenset({"resume"}), "MUTATION_UNRESOLVED_RESUME"),
}


# Closed reason-code vocabulary.  The ADR-0010 intent-state-pair
# codes and the shared ``ResumeGuardState`` codes (PRD #616) are
# layered above the legacy live-binding codes; the union is the
# entire vocabulary the Frontend reason-code lookup must cover.
REASON_CODES: frozenset[str] = (
    frozenset(
        {
            # Live-binding gate.
            "NO_LIVE_BINDING",
            # Flatten gate.
            "NO_OWNED_POSITIONS",
            # Mark-poisoned gate.
            "ALREADY_POISONED",
            # Stop gate.
            "ALREADY_STOPPED",
            # Child runtime evidence is stale / unavailable.
            "POSTURE_DEMOTED",
            # Account-wide freeze overlay.
            "ACCOUNT_FROZEN",
            # PRD #619-C5 — single-shot mutation transport returned an
            # ambiguous outcome (e.g. ReadTimeout after send): the
            # daemon may or may not have observed the request.  C5
            # surfaces this synchronously on the mutation response; the
            # durable disabled_reasons[] surfacing lands in 619-D once
            # the mutation_attempt record is persisted.
            "OUTCOME_UNKNOWN",
            # PRD #619-D action-conflict matrix.  Each code names the
            # *prior* unresolved mutation whose presence is blocking
            # this action; the cockpit operator-runbook entries (D5)
            # cover the per-code next-step copy.  Reconcile (D3) is
            # the resolution path.
            "MUTATION_UNRESOLVED_START",
            "MUTATION_UNRESOLVED_STOP",
            "MUTATION_UNRESOLVED_FLATTEN",
            "MUTATION_UNRESOLVED_RESUME",
        }
    )
    | RESUME_REASON_CODES
)


def _mutation_conflict_reason(
    action: ActionName, latest_mutation: MutationAttempt | None
) -> str | None:
    """Return the matrix reason code blocking ``action``, or ``None``.

    ``EFFECT_CONFIRMED`` is the only state that disengages the matrix;
    every other ``dispatch_state`` (including the three non-confirmed
    terminals) leaves the prior mutation unresolved for matrix purposes.
    """
    if latest_mutation is None or latest_mutation.dispatch_state == "EFFECT_CONFIRMED":
        return None
    cell = _MUTATION_CONFLICT_MATRIX.get(latest_mutation.action)
    if cell is None:
        return None
    blocked_actions, reason_code = cell
    if action not in blocked_actions:
        return None
    return reason_code


def _effect(
    *, process: InstanceProcessView, live_binding: LiveBinding | None
) -> Literal["LIVE_ACTUATION", "DURABLE_ONLY"]:
    # A durable write actuates live iff the host daemon is RUNNING for
    # this instance AND a live binding exists.  Anything else (idle,
    # stopping, exited, no binding) means the write is durable-only.
    if process.state == "running" and live_binding is not None:
        return "LIVE_ACTUATION"
    return "DURABLE_ONLY"


def _current_intent(desired_state: DesiredStateView | None) -> str | None:
    """Pure helper: extract the resolved intent string or ``None`` when
    the sidecar is missing / corrupt / never-deployed.  Absence is the
    effective-RUNNING default per ``DesiredStateView`` semantics.
    """
    if desired_state is None or desired_state.state is None:
        return None
    return desired_state.state.upper()


def _enabled(effect: Literal["LIVE_ACTUATION", "DURABLE_ONLY"]) -> ActionCapability:
    return ActionCapability(
        enabled=True,
        effect=effect,
        disabled_reason_code=None,
        disabled_reasons=[],
    )


def _disabled(
    effect: Literal["LIVE_ACTUATION", "DURABLE_ONLY"],
    reasons: list[str],
) -> ActionCapability:
    sorted_reasons = sort_reason_codes(reasons)
    return ActionCapability(
        enabled=False,
        effect=effect,
        disabled_reason_code=sorted_reasons[0] if sorted_reasons else None,
        disabled_reasons=sorted_reasons,
    )


def evaluate_action(
    action: ActionName,
    *,
    process: InstanceProcessView,
    live_binding: LiveBinding | None,
    poisoned: bool = False,
    owned_positions_empty: bool = False,
    desired_state: DesiredStateView | None = None,
    guard_state: ResumeGuardState | None = None,
    runtime_freshness: RuntimeFreshness | None = None,
    latest_mutation: MutationAttempt | None = None,
) -> ActionCapability:
    """Pure evaluator for a single action.

    The status projection calls this once per action; mutation endpoints
    call it again before executing so a stale snapshot cannot drive a
    write past the same gate.

    ``guard_state`` is the canonical ``ResumeGuardState`` resolved
    once-per-request by the caller (PRD #616).  When ``None`` the
    evaluator falls back to ``empty_guard_state()`` — the
    "nothing-ever-deployed" stance that permits Resume/Pause.

    ``latest_mutation`` is the most recent ``MutationAttempt`` for the
    instance (619-D1's repo).  When present and unresolved it engages
    the 619-D action-conflict matrix — the reason code is appended
    to the disabled-reasons list alongside any other blockers.
    """

    effect = _effect(process=process, live_binding=live_binding)
    guards = guard_state if guard_state is not None else empty_guard_state()
    explicit_intent = _current_intent(desired_state)
    intent = explicit_intent if explicit_intent is not None else "RUNNING"
    mutation_conflict = _mutation_conflict_reason(action, latest_mutation)

    def _finish(branch_effect: Literal["LIVE_ACTUATION", "DURABLE_ONLY"], reasons: list[str]) -> ActionCapability:
        """Close the evaluation by folding any matrix conflict in.

        ``mutation_conflict`` rides alongside whatever the branch
        accumulated — the cockpit sees both blockers (e.g.
        ``NO_LIVE_BINDING`` *and* ``MUTATION_UNRESOLVED_FLATTEN``)
        instead of one masking the other.
        """
        if mutation_conflict is not None:
            reasons.append(mutation_conflict)
        if reasons:
            return _disabled(branch_effect, reasons)
        return _enabled(branch_effect)

    if (
        runtime_freshness is not None
        and runtime_freshness.posture_demoted
        and action in {"resume", "flatten_and_pause"}
    ):
        return _finish(effect, ["POSTURE_DEMOTED"])

    if action == "resume":
        reasons: list[str] = []
        # Intent-state-pair rules — short-circuit above the artifact
        # guards because they describe the durable target, not a
        # broker / WAL condition.
        if intent == "RUNNING":
            reasons.append(
                "DESIRED_STATE_ALREADY_RUNNING"
                if explicit_intent == "RUNNING"
                else "DESIRED_STATE_DEFAULT_RUNNING"
            )
        if poisoned:
            reasons.append("REDEPLOY_REQUIRED")
        # Artifact guards (broker safety verdict, reconciliation,
        # uncertain-intent WAL) — full priority-ordered list.
        reasons.extend(guards.reason_codes)
        return _finish(effect, reasons)

    if action == "pause":
        # Pause does not consult artifact guards — pausing is always
        # safe (it only stops new entries).  Only the intent-state
        # rules apply.
        reasons = []
        if intent == "PAUSED":
            reasons.append("ALREADY_PAUSED")
        if intent == "STOPPED":
            reasons.append("STOPPED_REQUIRES_REDEPLOY")
        if poisoned:
            reasons.append("REDEPLOY_REQUIRED")
        return _finish(effect, reasons)

    if action == "stop":
        # Stop is the safe verb — no artifact guards apply.  Refuse
        # when already STOPPED (no-op) and when poisoned (the
        # already-poisoned run is dead; revival is Redeploy).
        reasons = []
        if intent == "STOPPED":
            reasons.append("ALREADY_STOPPED")
        if poisoned:
            reasons.append("REDEPLOY_REQUIRED")
        return _finish(effect, reasons)

    if action == "flatten_and_pause":
        reasons = []
        if live_binding is None:
            reasons.append("NO_LIVE_BINDING")
        elif owned_positions_empty:
            reasons.append("NO_OWNED_POSITIONS")
        return _finish("LIVE_ACTUATION", reasons)

    # mark_poisoned
    reasons = []
    if live_binding is None:
        reasons.append("NO_LIVE_BINDING")
    elif poisoned:
        reasons.append("ALREADY_POISONED")
    return _finish("LIVE_ACTUATION", reasons)


def evaluate_all_actions(
    *,
    process: InstanceProcessView,
    live_binding: LiveBinding | None,
    poisoned: bool = False,
    owned_positions_empty: bool = False,
    desired_state: DesiredStateView | None = None,
    guard_state: ResumeGuardState | None = None,
    runtime_freshness: RuntimeFreshness | None = None,
    latest_mutation: MutationAttempt | None = None,
) -> OperatorSurfaceActions:
    """Convenience wrapper used by the status projection."""
    common = {
        "process": process,
        "live_binding": live_binding,
        "poisoned": poisoned,
        "owned_positions_empty": owned_positions_empty,
        "desired_state": desired_state,
        "guard_state": guard_state,
        "runtime_freshness": runtime_freshness,
        "latest_mutation": latest_mutation,
    }
    return OperatorSurfaceActions(
        resume=evaluate_action("resume", **common),
        pause=evaluate_action("pause", **common),
        stop=evaluate_action("stop", **common),
        flatten_and_pause=evaluate_action("flatten_and_pause", **common),
        mark_poisoned=evaluate_action("mark_poisoned", **common),
    )
