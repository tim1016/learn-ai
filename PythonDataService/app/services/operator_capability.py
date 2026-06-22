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
  when the current durable intent is already RUNNING (no-op) or
  STOPPED (revival requires Redeploy per ADR-0010 §4) or the run is
  poisoned (``REDEPLOY_REQUIRED``).
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
from app.services.resume_guard_state import (
    RESUME_REASON_CODES,
    ResumeGuardState,
    empty_guard_state,
    sort_reason_codes,
)
from app.services.runtime_freshness import RuntimeFreshness

ActionName = Literal["resume", "pause", "stop", "flatten_and_pause", "mark_poisoned"]


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
            # PRD #619-C5 — single-shot mutation transport returned an
            # ambiguous outcome (e.g. ReadTimeout after send): the
            # daemon may or may not have observed the request.  C5
            # surfaces this synchronously on the mutation response; the
            # durable disabled_reasons[] surfacing lands in 619-D once
            # the mutation_attempt record is persisted.
            "OUTCOME_UNKNOWN",
        }
    )
    | RESUME_REASON_CODES
)


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


def _effective_intent(desired_state: DesiredStateView | None) -> str:
    """Resolved intent for evaluation: when the sidecar is absent /
    unknown the system behaves as RUNNING (the cockpit's effective
    default for never-deployed instances)."""
    intent = _current_intent(desired_state)
    return intent if intent is not None else "RUNNING"


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
) -> ActionCapability:
    """Pure evaluator for a single action.

    The status projection calls this once per action; mutation endpoints
    call it again before executing so a stale snapshot cannot drive a
    write past the same gate.

    ``guard_state`` is the canonical ``ResumeGuardState`` resolved
    once-per-request by the caller (PRD #616).  When ``None`` the
    evaluator falls back to ``empty_guard_state()`` — the
    "nothing-ever-deployed" stance that permits Resume/Pause.
    """

    effect = _effect(process=process, live_binding=live_binding)
    guards = guard_state if guard_state is not None else empty_guard_state()
    intent = _effective_intent(desired_state)

    if (
        runtime_freshness is not None
        and runtime_freshness.posture_demoted
        and action in {"resume", "flatten_and_pause"}
    ):
        return _disabled(effect, ["POSTURE_DEMOTED"])

    if action == "resume":
        reasons: list[str] = []
        # Intent-state-pair rules — short-circuit above the artifact
        # guards because they describe the durable target, not a
        # broker / WAL condition.
        if intent == "RUNNING":
            reasons.append("ALREADY_RUNNING")
        if intent == "STOPPED":
            reasons.append("STOPPED_REQUIRES_REDEPLOY")
        if poisoned:
            reasons.append("REDEPLOY_REQUIRED")
        # Artifact guards (broker safety verdict, reconciliation,
        # uncertain-intent WAL) — full priority-ordered list.
        reasons.extend(guards.reason_codes)
        if reasons:
            return _disabled(effect, reasons)
        return _enabled(effect)

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
        if reasons:
            return _disabled(effect, reasons)
        return _enabled(effect)

    if action == "stop":
        # Stop is the safe verb — no artifact guards apply.  Refuse
        # when already STOPPED (no-op) and when poisoned (the
        # already-poisoned run is dead; revival is Redeploy).
        reasons = []
        if intent == "STOPPED":
            reasons.append("ALREADY_STOPPED")
        if poisoned:
            reasons.append("REDEPLOY_REQUIRED")
        if reasons:
            return _disabled(effect, reasons)
        return _enabled(effect)

    if action == "flatten_and_pause":
        if live_binding is None:
            return _disabled("LIVE_ACTUATION", ["NO_LIVE_BINDING"])
        if owned_positions_empty:
            return _disabled("LIVE_ACTUATION", ["NO_OWNED_POSITIONS"])
        return _enabled("LIVE_ACTUATION")

    # mark_poisoned
    if live_binding is None:
        return _disabled("LIVE_ACTUATION", ["NO_LIVE_BINDING"])
    if poisoned:
        return _disabled("LIVE_ACTUATION", ["ALREADY_POISONED"])
    return _enabled("LIVE_ACTUATION")


def evaluate_all_actions(
    *,
    process: InstanceProcessView,
    live_binding: LiveBinding | None,
    poisoned: bool = False,
    owned_positions_empty: bool = False,
    desired_state: DesiredStateView | None = None,
    guard_state: ResumeGuardState | None = None,
    runtime_freshness: RuntimeFreshness | None = None,
) -> OperatorSurfaceActions:
    """Convenience wrapper used by the status projection."""
    return OperatorSurfaceActions(
        resume=evaluate_action(
            "resume",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
        ),
        pause=evaluate_action(
            "pause",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
        ),
        stop=evaluate_action(
            "stop",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
        ),
        flatten_and_pause=evaluate_action(
            "flatten_and_pause",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
        ),
        mark_poisoned=evaluate_action(
            "mark_poisoned",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
        ),
    )
