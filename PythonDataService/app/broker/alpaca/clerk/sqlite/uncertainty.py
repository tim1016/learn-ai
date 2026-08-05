"""Typed uncertainty envelope and admission (#1380, Part A).

Two primitives, both pure/deterministic given the repository's current
state:

- :func:`raise_uncertainty` — the one gate for durably recording a typed R5
  uncertainty (severity, the two independent admission axes
  ``blocks_new_exposure``/``allows_reduction``, and the four backend-
  authored operator-facing strings) against the schema's already-scaffolded
  ``uncertainties`` table. Idempotent — a caller re-raising the identical
  ``(strategy_instance_id, reason_code)`` pair while one is still active is
  a no-op, the same bounded-growth discipline
  :func:`~.reconcile._raise_account_hold` established for holds. Scope is
  never a caller-supplied parameter: passing a ``strategy_instance_id``
  means ``BOT``, passing ``None`` means ``ACCOUNT_CLERK`` — the same
  truthful scope/identity coupling the schema already enforces for holds.
  A caller that does not (yet) know how to classify some new situation gets
  the fail-closed default for free just by using this function's own
  defaults (``blocks_new_exposure=True``, no ``strategy_instance_id``
  override unless the caller explicitly narrows it) — satisfying "an
  unrecognized reason defaults to ``ACCOUNT_CLERK`` and blocks new
  exposure" as a consequence of the API's shape, not a separate registry
  of "known" reason codes to validate against.
- :func:`admit_new_exposure` — the one admission function, reused verbatim
  by both a future preview surface and real enforcement (#1380 acceptance:
  "the same policy authors capability and effect"). Folds both the rich
  uncertainty envelope and the simpler #1378 hold mechanism behind one
  surface, since a caller submitting a new decision must be blocked by
  either. An ``ACCOUNT_CLERK``-scoped block applies to every bot; a
  ``BOT``-scoped block applies only to the matching bot — unrelated bots
  keep trading (#1380 acceptance).

:func:`require_admission` is the ``enter.py``-facing enforcement wrapper,
following the exact existing pattern of
:func:`~.idempotency.require_strategy_instance`/
:func:`~.idempotency.require_active_run`: called from inside a
``build_transition`` closure, under the write lock, so a concurrently-raised
uncertainty can never race past the check.

Deliberately deferred (see the module docstring precedent in ``enter.py``/
``exit.py``): the 6 named, backend-authored recovery actions (Reconcile now,
Cancel verified working orders, Prepare safe flatten, Stop bot decisions,
Open custody timeline, Rebuild from mirror / Reset authority) with their
own availability/reason/scope/freshness/next-step metadata are #1380's
remaining scope, not built here — this slice ships the envelope and the
admission policy those actions will eventually consult, not the action
catalog itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.facts import UncertaintyRaisedFacts, UncertaintyResolvedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

__all__ = [
    "AdmissionBlockedError",
    "AdmissionDecision",
    "admit_new_exposure",
    "raise_uncertainty",
    "require_admission",
    "resolve_uncertainty",
]


def raise_uncertainty(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str | None,
    reason_code: str,
    headline: str,
    explanation: str,
    operator_impact: str,
    next_step: str,
    evidence_refs: tuple[str, ...] = (),
    severity: str = "warning",
    blocks_new_exposure: bool = True,
    allows_reduction: bool = True,
) -> bool:
    """Raise a typed uncertainty if none is already ``ACTIVE`` for this
    ``(scope, reason_code, strategy_instance_id)``. Returns ``True`` if a
    new one was appended, ``False`` if one was already active (no-op)."""
    scope = "BOT" if strategy_instance_id is not None else "ACCOUNT_CLERK"

    def build_transition() -> TransitionInput:
        facts = UncertaintyRaisedFacts(
            severity=severity,
            blocks_new_exposure=blocks_new_exposure,
            allows_reduction=allows_reduction,
            reason_code=reason_code,
            headline=headline,
            explanation=explanation,
            operator_impact=operator_impact,
            next_step=next_step,
            evidence_refs=list(evidence_refs),
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="UNCERTAINTY_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="UNCERTAINTY_RAISED",
            facts_json=facts.to_facts_json(),
        )

    return repo.raise_uncertainty_if_none_active(
        scope=scope,
        reason_code=reason_code,
        strategy_instance_id=strategy_instance_id,
        build_transition=build_transition,
    )


def resolve_uncertainty(
    repo: ClerkSqliteRepository, *, uncertainty_id: str, resolution_note: str
) -> None:
    """Close an ``ACTIVE`` uncertainty. Idempotent on the fold's own
    ``resolved_at_ms IS NULL`` guard — resolving an already-resolved
    uncertainty a second time appends an audit transition but changes
    nothing."""
    facts = UncertaintyResolvedFacts(uncertainty_id=uncertainty_id, resolution_note=resolution_note)
    repo.append_transition(
        TransitionInput(
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="UNCERTAINTY_RESOLVED",
            facts_json=facts.to_facts_json(),
        )
    )


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason_code: str | None = None
    why: str | None = None


def admit_new_exposure(repo: ClerkSqliteRepository, *, strategy_instance_id: str) -> AdmissionDecision:
    """The one admission function (#1380): reused verbatim by both a future
    preview surface and real enforcement. Holds are checked first (they
    always block, by their existing #1378 design — there is no
    ``allows_reduction``-style exception for a hold); an uncertainty only
    blocks if its own ``blocks_new_exposure`` flag says so, since some
    uncertainties (proven cancellation, reconciliation, and risk reduction
    are meant to stay available per the issue's own framing) are
    deliberately allowed to coexist with continued new exposure.
    """
    for hold in repo.active_holds_for_admission(strategy_instance_id=strategy_instance_id):
        return AdmissionDecision(
            allowed=False,
            reason_code=hold["reason_code"],
            why=f"An active {hold['scope'].lower()}-scoped hold ({hold['reason_code']}) blocks new exposure.",
        )
    for uncertainty in repo.active_uncertainties_for_admission(strategy_instance_id=strategy_instance_id):
        if uncertainty["blocks_new_exposure"]:
            return AdmissionDecision(
                allowed=False,
                reason_code=uncertainty["reason_code"],
                why=uncertainty["explanation"],
            )
    return AdmissionDecision(allowed=True)


class AdmissionBlockedError(Exception):
    """A backend-authored uncertainty or hold currently blocks new exposure
    for this bot or account (#1380, R6) — never raised for a decision that
    only reduces/closes exposure (EXIT is untouched by this check)."""

    def __init__(self, decision: AdmissionDecision) -> None:
        self.decision = decision
        super().__init__(f"new exposure blocked: {decision.reason_code} — {decision.why}")


def require_admission(repo: ClerkSqliteRepository, *, strategy_instance_id: str) -> None:
    """Called from inside a ``build_transition`` closure, same rule as
    :func:`~.idempotency.require_strategy_instance`/
    :func:`~.idempotency.require_active_run` — must run under the write
    lock so a concurrently-raised uncertainty can never race past this
    check between an outside-the-lock preview and the actual accept."""
    decision = admit_new_exposure(repo, strategy_instance_id=strategy_instance_id)
    if not decision.allowed:
        raise AdmissionBlockedError(decision)
