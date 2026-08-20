"""Canonical account-level operator posture for Alpaca (issue #1664).

One evidence cut decides one dominant ``OperatorCondition`` (or none, when
healthy) and projects it onto the ``account_desk`` and ``fleet_roster``
hosts per ADR 0027. The evidence is exactly the facts that already author
``ClerkProjectionResponse.guidance`` / ``recovery_actions``
(``authority_health``, uncertainty count, the recovery catalog) plus the
account-eligibility facts that already gate paper-deploy readiness
(``app/services/broker_v2_panel/paper_deploy_service.py``): paper mode,
active account status, Alpaca trading/account block flags, unresolved
Clerk intents, and Clerk-channel health.

Frontends render only their own host projection (``account_desk`` for the
Alpaca desk operator card, ``fleet_roster`` for the bot-roster Account
Strip) and must never re-derive a verdict from raw evidence — see
``docs/audits/non-numeric-operator-verdict-census-2026-08-18.md`` and
``docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md``
Decision 12.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.projection_models import (
    AuthorityHealth,
    ProjectionGuidance,
    RecoveryCapability,
)
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    AccountOperatorPosture,
    ConfirmInFormAction,
    Disposition,
    NavigateAction,
    OpenRunbookAction,
    OperatorBlocker,
    OperatorBlockerAnchor,
    OperatorCondition,
    OperatorMove,
)

# The account_desk anchor is a stable string the Alpaca operator lens
# recognizes to open its in-place Clerk recovery panel (see
# alpaca-operator-lens.component.ts `onBlockerMoveRequested`).
ACCOUNT_DESK_RECOVERY_ANCHOR = "account-desk-clerk-recovery"

_ACCOUNT_DESK_CLERK_ANCHOR = OperatorBlockerAnchor(kind="clerk", subject_key=None)
_OPEN_RECOVERY_MOVE = OperatorMove(
    label="Open Clerk recovery",
    action=ConfirmInFormAction(kind="confirm_in_form", anchor=ACCOUNT_DESK_RECOVERY_ANCHOR),
)
_OPEN_OPERATOR_DESK_MOVE = OperatorMove(
    label="Open Account Operator desk",
    action=NavigateAction(kind="navigate", route="/brokers/alpaca"),
)
_RUNBOOK_MOVE = OperatorMove(
    label="Open Clerk recovery runbook",
    action=OpenRunbookAction(kind="open_runbook", slug="alpaca-account-clerk-authority-recovery"),
)
# For account-eligibility conditions with no self-healing path (wrong
# execution mode, identity mismatch): distinct from _RUNBOOK_MOVE, which is
# specifically about custody/authority recovery, not account configuration.
_ACCOUNT_CONFIGURATION_RUNBOOK_MOVE = OperatorMove(
    label="Open account configuration runbook",
    action=OpenRunbookAction(kind="open_runbook", slug="alpaca-account-configuration"),
)

# One (disposition, move) pair per host, keyed by the custody-side
# disposition. `fix_here` on account_desk (it owns the in-place recovery
# panel) always projects as `fix_elsewhere` on fleet_roster (it can only
# point the operator at the desk) — the same condition, different honest
# cures, per ADR 0027. `wait`/`terminal` are host-symmetric.
_HostProjection = tuple[Disposition, OperatorMove | None]
_CUSTODY_HOST_PROJECTIONS: dict[Disposition, tuple[_HostProjection, _HostProjection]] = {
    "fix_here": (("fix_here", _OPEN_RECOVERY_MOVE), ("fix_elsewhere", _OPEN_OPERATOR_DESK_MOVE)),
    "wait": (("wait", None), ("wait", None)),
    "terminal": (("terminal", _RUNBOOK_MOVE), ("terminal", _RUNBOOK_MOVE)),
}


@dataclass(frozen=True)
class AccountOperatorPostureContext:
    """One evidence cut for the account-level operator decision (#1664).

    ``guidance``/``recovery_actions``/``authority_health``/
    ``uncertainty_count`` are the same custody-side facts that already
    author ``ClerkProjectionResponse``. The remaining fields are the
    account-eligibility facts paper-deploy readiness already requires.

    ``account_mode``/``account_status``/``trading_blocked``/
    ``account_blocked`` are ``None`` together exactly when the Alpaca
    account snapshot could not be read this cycle — there is no separate
    "evidence unavailable" flag to fall out of sync with them. Absence is
    unrepresentable any other way: ``__post_init__`` rejects a context
    where only some of the four are ``None`` (2026-08-19 review: a prior
    revision defaulted a missing snapshot to paper/ACTIVE/unblocked, which
    was safe only because ``_eligibility_condition`` happened to check the
    now-removed flag first).
    """

    authority_health: AuthorityHealth
    uncertainty_count: int
    guidance: ProjectionGuidance
    recovery_actions: tuple[RecoveryCapability, ...]
    account_mode: str | None
    account_status: str | None
    trading_blocked: bool | None
    account_blocked: bool | None
    outstanding_intents: int
    channels_ready: bool
    channels_detail: str | None
    # True when a same-request account read succeeded but named a different
    # account than the projection this posture is being authored for (e.g.
    # Alpaca credentials point at account B while the active SQLite Clerk
    # authority is bound to account A). The caller must also leave the four
    # account_* fields above None in this case — a mismatched account's
    # facts must never be attributed to this projection's account.
    account_identity_mismatch: bool = False

    def __post_init__(self) -> None:
        account_fields = (
            self.account_mode,
            self.account_status,
            self.trading_blocked,
            self.account_blocked,
        )
        some_present = any(field is not None for field in account_fields)
        all_present = all(field is not None for field in account_fields)
        if some_present and not all_present:
            raise ValueError(
                "account_mode/account_status/trading_blocked/account_blocked must be "
                "all present or all None — a missing account snapshot leaves all four "
                "unset, never a partial mix"
            )


def build_account_operator_posture(ctx: AccountOperatorPostureContext) -> AccountOperatorPosture:
    """Author the one dominant account condition, or a healthy posture."""
    custody = _custody_condition(ctx)
    if custody is not None:
        return custody
    eligibility = _eligibility_condition(ctx)
    if eligibility is not None:
        return eligibility
    return AccountOperatorPosture(
        condition=None,
        account_desk=None,
        fleet_roster=None,
        status_headline=ctx.guidance.headline,
        status_detail=ctx.guidance.explanation,
    )


def _posture(
    *,
    condition_id: str,
    severity: Literal["blocking", "warning"],
    headline: str,
    detail: str,
    account_desk: _HostProjection,
    fleet_roster: _HostProjection,
    evidence: dict[str, str | int | float | bool | None],
) -> AccountOperatorPosture:
    """Build both host projections of one condition. The sole constructor
    of `OperatorBlocker` in this module — every branch below only decides
    *which* (disposition, move) pair each host gets."""
    condition = OperatorCondition(id=condition_id, severity=severity, scope="account", evidence=evidence)
    desk_disposition, desk_move = account_desk
    roster_disposition, roster_move = fleet_roster
    return AccountOperatorPosture(
        condition=condition,
        account_desk=OperatorBlocker(
            condition=condition,
            host="account_desk",
            anchor=_ACCOUNT_DESK_CLERK_ANCHOR,
            audience="operator",
            disposition=desk_disposition,
            headline=headline,
            detail=detail,
            primary_move=desk_move,
            applies_to="both",
        ),
        fleet_roster=OperatorBlocker(
            condition=condition,
            host="fleet_roster",
            anchor=SURFACE_ANCHOR,
            audience="operator",
            disposition=roster_disposition,
            headline=headline,
            detail=detail,
            primary_move=roster_move,
            applies_to="both",
        ),
        status_headline=headline,
        status_detail=detail,
    )


def _custody_condition(ctx: AccountOperatorPostureContext) -> AccountOperatorPosture | None:
    """Port the prior client-side ``projectionPosture`` branching server-side.

    Uses the same evidence (``authority_health``, uncertainty count, the
    recovery catalog) that already authors ``guidance``/``recovery_actions``,
    per the issue's "same recovery-policy evidence cut" requirement.
    """
    healthy_custody = (
        ctx.authority_health == "healthy"
        and ctx.uncertainty_count == 0
        and not ctx.guidance.action_required
    )
    if healthy_custody:
        return None

    authority_unhealthy = ctx.authority_health != "healthy"
    primary = next((action for action in ctx.recovery_actions if action.primary), None)
    if primary is None:
        primary = next((action for action in ctx.recovery_actions if not action.available), None)

    if primary is not None and primary.available:
        disposition: Disposition = "fix_here"
        condition_id = f"alpaca_clerk_recovery:{primary.action_id}"
        severity: Literal["blocking", "warning"] = "blocking"
    elif primary is not None:
        disposition = "wait"
        condition_id = f"alpaca_clerk_recovery_pending:{primary.action_id}"
        severity = "blocking" if authority_unhealthy else "warning"
    elif authority_unhealthy:
        disposition = "terminal"
        condition_id = f"alpaca_clerk_authority_failure:{ctx.authority_health}"
        severity = "blocking"
    else:
        disposition = "wait"
        condition_id = "alpaca_clerk_evidence_review"
        severity = "warning"

    account_desk, fleet_roster = _CUSTODY_HOST_PROJECTIONS[disposition]
    return _posture(
        condition_id=condition_id,
        severity=severity,
        headline=ctx.guidance.headline,
        detail=ctx.guidance.explanation,
        account_desk=account_desk,
        fleet_roster=fleet_roster,
        evidence={
            "authority_health": ctx.authority_health,
            "uncertainty_count": ctx.uncertainty_count,
            "action_required": ctx.guidance.action_required,
        },
    )


# One evidence check per row. Most of these have no in-app cure and cannot
# self-heal by waiting either, so they use `wait`/no move by default — the
# honest cure lives at Alpaca or in a future retry, not behind a button.
# Two rows (identity mismatch, wrong execution mode) are static
# configuration problems fresh evidence can never resolve: they override the
# default with `terminal` + an honest runbook move instead of leaving the UI
# saying "waiting" forever (2026-08-20 review).
def _eligibility_condition(ctx: AccountOperatorPostureContext) -> AccountOperatorPosture | None:
    if ctx.account_identity_mismatch:
        return _eligibility_posture(
            condition_id="alpaca_account_identity_mismatch",
            severity="blocking",
            headline="Account identity does not match this Clerk authority",
            detail=(
                "The Alpaca account observed by this read does not match the "
                "account this Clerk authority is bound to. Paper-mode and "
                "active-status checks cannot trust a different account's facts."
            ),
            evidence={"account_identity_mismatch": True},
            account_desk=("terminal", _ACCOUNT_CONFIGURATION_RUNBOOK_MOVE),
            fleet_roster=("terminal", _ACCOUNT_CONFIGURATION_RUNBOOK_MOVE),
        )
    if ctx.account_mode is None:
        return _eligibility_posture(
            condition_id="alpaca_account_evidence_unavailable",
            severity="warning",
            headline="Account confirmation is temporarily unavailable",
            detail=(
                "The latest Alpaca account read failed. Paper-mode and "
                "active-status checks will resume once a fresh account "
                "observation succeeds."
            ),
            evidence={"account_evidence_unavailable": True},
        )
    if ctx.account_mode != "paper":
        return _eligibility_posture(
            condition_id="alpaca_account_wrong_execution_mode",
            severity="blocking",
            headline="This account is not in paper mode",
            detail=(
                "Paper deployment and manual orders require a paper-mode "
                f"account; Alpaca reports {ctx.account_mode!r} mode for this account. "
                "This will not resolve by waiting — the account mode must be changed."
            ),
            evidence={"account_mode": ctx.account_mode},
            account_desk=("terminal", _ACCOUNT_CONFIGURATION_RUNBOOK_MOVE),
            fleet_roster=("terminal", _ACCOUNT_CONFIGURATION_RUNBOOK_MOVE),
        )
    if ctx.account_status.upper() != "ACTIVE":
        return _eligibility_posture(
            condition_id="alpaca_account_inactive",
            severity="blocking",
            headline="This account is not active",
            detail=(
                f"Alpaca reports account status {ctx.account_status!r}; paper "
                "deployment and manual orders require an ACTIVE account."
            ),
            evidence={"account_status": ctx.account_status},
        )
    if ctx.trading_blocked or ctx.account_blocked:
        return _eligibility_posture(
            condition_id="alpaca_account_trading_blocked",
            severity="blocking",
            headline="Alpaca has blocked trading on this account",
            detail=(
                f"Alpaca reports trading_blocked={ctx.trading_blocked}, "
                f"account_blocked={ctx.account_blocked} for this account."
            ),
            evidence={
                "trading_blocked": ctx.trading_blocked,
                "account_blocked": ctx.account_blocked,
            },
        )
    if not ctx.channels_ready:
        return _eligibility_posture(
            condition_id="alpaca_account_channels_not_ready",
            severity="blocking",
            headline="Clerk submission channels are not ready",
            detail=(
                ctx.channels_detail
                or "One or more Clerk submission-gate channels is missing, stale, or unhealthy."
            ),
            evidence={"channels_ready": False},
        )
    if ctx.outstanding_intents > 0:
        return _eligibility_posture(
            condition_id="alpaca_account_unresolved_intents",
            severity="warning",
            headline="An account command is still resolving",
            detail=(
                f"{ctx.outstanding_intents} account command(s) are still "
                "awaiting a final broker outcome."
            ),
            evidence={"outstanding_intents": ctx.outstanding_intents},
        )
    return None


def _eligibility_posture(
    *,
    condition_id: str,
    severity: Literal["blocking", "warning"],
    headline: str,
    detail: str,
    evidence: dict[str, str | int | float | bool | None],
    account_desk: _HostProjection = ("wait", None),
    fleet_roster: _HostProjection = ("wait", None),
) -> AccountOperatorPosture:
    return _posture(
        condition_id=condition_id,
        severity=severity,
        headline=headline,
        detail=detail,
        account_desk=account_desk,
        fleet_roster=fleet_roster,
        evidence=evidence,
    )
