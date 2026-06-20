"""Shared action capability evaluator (PRD #607 / Slice 1 / #608).

Single Python function that answers "is this action allowed right now?"
for the four cockpit actions: Resume, Pause, Flatten-and-pause,
Mark-poisoned.  Called from BOTH the operator-surface status projection
and every relevant mutation endpoint so that a stale status snapshot
cannot be exploited to drive a mutation past the same eligibility rule.

Per the #608 host-process authority section (ADR-0003 / ADR-0007):

- ``resume`` and ``pause`` are *durable-intent writes* and are NEVER
  disabled by the server: the durable write succeeds regardless of
  liveness and gates the next host start.  Their ``effect`` flips
  between ``LIVE_ACTUATION`` (a daemon is bound and running) and
  ``DURABLE_ONLY`` (no live binding — the write still succeeds but does
  not actuate until the host runner starts).
- ``flatten_and_pause`` requires live actuation (FLATTEN_NOW cannot
  execute without a bound runner).  Without a binding it is disabled
  with ``NO_LIVE_BINDING``.
- ``mark_poisoned`` rejects on ``NO_LIVE_BINDING`` and (when the
  poisoned sentinel is already set) on ``ALREADY_POISONED``.

The reason-code vocabulary is closed and documented; the Frontend
maintains a typed lookup mapping each code to operator-language copy.
"""

from __future__ import annotations

from typing import Literal

from app.schemas.live_runs import (
    ActionCapability,
    InstanceProcessView,
    LiveBinding,
    OperatorSurfaceActions,
)

ActionName = Literal["resume", "pause", "flatten_and_pause", "mark_poisoned"]


# Closed reason-code vocabulary.  ``BUSY_VERB_IN_FLIGHT``,
# ``ALREADY_RUNNING``, and ``NOT_RUNNING`` are deliberately absent (#608
# § "Reason-code vocabulary for actions.* (revised)"); the first lives
# only in Angular and the latter two described eligibility for intent
# transitions that durable writes simply do not have.
REASON_CODES: frozenset[str] = frozenset(
    {
        "NO_LIVE_BINDING",
        "SAFETY_BLOCK_HALT",
        "RECONCILE_NOT_WIRED",
        "NO_OWNED_POSITIONS",
        "ALREADY_POISONED",
    }
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


def evaluate_action(
    action: ActionName,
    *,
    process: InstanceProcessView,
    live_binding: LiveBinding | None,
    poisoned: bool = False,
    owned_positions_empty: bool = False,
) -> ActionCapability:
    """Pure evaluator for a single action.

    The status projection calls this once per action; mutation endpoints
    call it again before executing so a stale snapshot cannot drive a
    write past the same gate.
    """

    effect = _effect(process=process, live_binding=live_binding)

    if action in ("resume", "pause"):
        # Durable-intent writes always succeed: they gate the next host
        # start and are absorbed as a no-op when already matching.
        return ActionCapability(enabled=True, effect=effect, disabled_reason_code=None)

    if action == "flatten_and_pause":
        if live_binding is None:
            return ActionCapability(
                enabled=False,
                effect="LIVE_ACTUATION",
                disabled_reason_code="NO_LIVE_BINDING",
            )
        if owned_positions_empty:
            return ActionCapability(
                enabled=False,
                effect="LIVE_ACTUATION",
                disabled_reason_code="NO_OWNED_POSITIONS",
            )
        return ActionCapability(enabled=True, effect="LIVE_ACTUATION", disabled_reason_code=None)

    # mark_poisoned
    if live_binding is None:
        return ActionCapability(
            enabled=False,
            effect="LIVE_ACTUATION",
            disabled_reason_code="NO_LIVE_BINDING",
        )
    if poisoned:
        return ActionCapability(
            enabled=False,
            effect="LIVE_ACTUATION",
            disabled_reason_code="ALREADY_POISONED",
        )
    return ActionCapability(enabled=True, effect="LIVE_ACTUATION", disabled_reason_code=None)


def evaluate_all_actions(
    *,
    process: InstanceProcessView,
    live_binding: LiveBinding | None,
    poisoned: bool = False,
    owned_positions_empty: bool = False,
) -> OperatorSurfaceActions:
    """Convenience wrapper used by the status projection."""
    return OperatorSurfaceActions(
        resume=evaluate_action(
            "resume",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
        ),
        pause=evaluate_action(
            "pause",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
        ),
        flatten_and_pause=evaluate_action(
            "flatten_and_pause",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
        ),
        mark_poisoned=evaluate_action(
            "mark_poisoned",
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
        ),
    )
