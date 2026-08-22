"""Exact-pairing admission gate and Clerk-proved rollback boundary for the
guarded Alpaca Paper canary path.

Issue #1729 (PRD Slice 3 — ``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md``
Sec 23). Two responsibilities, both additive to machinery that already
exists — this module invents no new proof of its own:

* ``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`` gates every real Alpaca Paper
  (``mode="trade"``) admission of a Signal-Program-backed program to one
  exact ``(program_key, account_id)`` pairing. ``app.services.run_admission``
  already composes seal + build
  (``signal_program_admission.prove_running_program_build``, which also
  proves the sealed provider identity is present and unchanged), validation,
  replay/boot-recovery readiness, and Clerk custody for every Start/Resume —
  see ``evaluate_run_admission``'s existing ``PROGRAM_BUILD_UNPROVEN``,
  ``STRATEGY_VALIDATION_*``, runtime, and Clerk custody gates. This module
  adds only the missing exact-pairing check and is composed into that same
  gate sequence, following the established pattern rather than inventing a
  parallel one.

* ``evaluate_canary_rollback`` classifies whether an existing Stop custody
  outcome (``app.services.bot_carryover.prove_stop_outcome``, itself backed
  by the Clerk's ``prove_instance_custody``) is a *safe* boundary to roll a
  canary back at. Resuming after a rollback is not a separate mechanism: it
  re-enters ``BotResumeAdmission`` and is re-gated by the same allowlist,
  seal, build, validation, replay, and custody checks as any other Resume —
  there is no cached or partial admission to replay, and no process is
  hot-swapped (Resume only admits once the prior process is proven
  ``EXITED``; see ``evaluate_run_admission``'s ``RESUME_PROCESS_NOT_TERMINAL``
  gate).

SAFETY (issue #1729): ``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`` ships EMPTY.
Populating it is an explicit human decision made after reviewing the full
composed proof for one specific program bound to one specific account. Do
not add an entry, a default, a dev-convenience shortcut, or an env-var
fallback here as part of a code change — that would be an operational
activation, not a build.
"""

from __future__ import annotations

from app.schemas.canary_admission import CanaryRollbackDecision
from app.services.bot_carryover import StopCustodyOutcome

# SAFETY: exact by (program_key, account_id) — never a prefix, a program-only
# match, or an account-only match. Ships empty; see module docstring.
CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS: frozenset[tuple[str, str]] = frozenset()

# The only two Stop outcomes `bot_carryover.prove_stop_outcome` can produce
# that represent a Clerk-proved safe position: no exposure at all, or an
# exposure explicitly approved for carryover. The other two outcomes
# (`STOP_REQUIRES_FLATTEN`, `STOPPED_CUSTODY_UNPROVABLE`) are honest records
# of an unsafe or unprovable boundary — a rollback refuses at those rather
# than proceeding.
_SAFE_ROLLBACK_STOP_OUTCOMES: frozenset[StopCustodyOutcome] = frozenset(
    {"STOPPED_FLAT", "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE"}
)


def canary_gate_applies(*, mode: str, program_build_state: str) -> bool:
    """True only once a real Alpaca Paper trade-mode build is already proven.

    Dry Run uses its own isolated synthetic authority and is never subject to
    the canary allowlist (PRD Sec 15). A program with no registered Signal
    Program (``program_build_state == "NOT_APPLICABLE"``) has no seal to
    compose a canary proof from, so it is out of this gate's scope entirely —
    the legacy event-handler strategies keep working exactly as before.

    Deliberately ``== "PROVEN"`` rather than ``!= "NOT_APPLICABLE"``: an
    unproven build is already refused by the pre-existing
    ``PROGRAM_BUILD_UNPROVEN`` gate regardless of allowlist membership, and
    scoping to the proven case keeps this check from ever pre-empting that
    established gate's own reason code — the allowlist is the deciding
    factor only once every other proof would otherwise pass.
    """
    return mode == "trade" and program_build_state == "PROVEN"


def canary_pairing_admitted(*, program_key: str, account_id: str) -> bool:
    """Exact-by-(program, account) allowlist membership."""
    return (program_key, account_id) in CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS


def evaluate_canary_rollback(
    *,
    strategy_instance_id: str,
    stop_outcome: StopCustodyOutcome,
    evaluated_at_ms: int,
) -> CanaryRollbackDecision:
    """Refuse a canary rollback unless the Clerk proves a safe stop boundary.

    ``stop_outcome`` is the exact classification
    ``bot_carryover.prove_stop_outcome`` already derives from a fresh
    ``InstanceCustodyProof``: freeze inactive, reconciliation clean, and no
    working orders or unresolved intents remain, then either no exposure
    (``STOPPED_FLAT``) or an approved carried position
    (``STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE``). An ordinary Stop always
    records that outcome honestly, even when it is ``STOP_REQUIRES_FLATTEN``
    or ``STOPPED_CUSTODY_UNPROVABLE``. Rollback is stricter: it is refused
    outright at either of those two boundaries instead of proceeding on an
    unprovable or policy-violating position.
    """
    allowed = stop_outcome in _SAFE_ROLLBACK_STOP_OUTCOMES
    if allowed:
        reason_code = "CANARY_ROLLBACK_ADMITTED"
        explanation = "The Clerk proves a safe boundary to stop the canary at."
        next_step = None
    elif stop_outcome == "STOP_REQUIRES_FLATTEN":
        reason_code = "CANARY_ROLLBACK_REQUIRES_FLATTEN"
        explanation = (
            "The Clerk proves attributed exposure remains and this instance's "
            "carryover policy does not approve carrying it through rollback."
        )
        next_step = "Flatten the exact Clerk-attributed exposure before rollback."
    else:
        reason_code = "CANARY_ROLLBACK_BOUNDARY_UNPROVABLE"
        explanation = "The Clerk cannot currently prove a safe custody boundary to roll back at."
        next_step = "Reconcile the account through the Clerk, then retry rollback."
    return CanaryRollbackDecision(
        strategy_instance_id=strategy_instance_id,
        allowed=allowed,
        reason_code=reason_code,
        explanation=explanation,
        next_step=next_step,
        stop_outcome=stop_outcome,
        evaluated_at_ms=evaluated_at_ms,
    )


__all__ = [
    "CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
    "canary_gate_applies",
    "canary_pairing_admitted",
    "evaluate_canary_rollback",
]
