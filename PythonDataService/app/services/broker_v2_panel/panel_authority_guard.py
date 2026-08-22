"""Account-authority resolution and mixed-aggregate guard for panel evidence.

Issue #1729 AC #8: "``authority_kind`` survives storage, generated contracts,
and UI; mixed synthetic/real aggregate requests are rejected." A roster or
panel aggregate accepts exactly one Clerk account authority — the real-paper
account or one isolated ``sim:`` account — never a blend. This is the read
boundary ``panel_projection_service.build_panel`` resolves an evidence cut's
authority through, and refuses to serve an aggregate that would mix
authorities. It reuses the tested ``SingleAuthorityAggregate`` guard
(``app.schemas.account_authority``) rather than re-deriving the check.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.schemas.account_authority import (
    AuthorityKind,
    AuthorityScopedRow,
    SingleAuthorityAggregate,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import RecentDecisionView, RecentFillView


class MixedAuthorityAggregateError(RuntimeError):
    """A panel evidence aggregate would have combined more than one account authority.

    PRD Sec 15/FR-029: a roster or panel aggregate accepts exactly one Clerk
    account authority (the real-paper account or one isolated ``sim:``
    account) — never a blend of simulated and real-paper rows, and never rows
    stamped for two different accounts of the same kind (e.g. one bot's
    synthetic authority leaking into another bot's projection).
    """


def default_authority_account_id(status: BotStatusView, account_id: str) -> str:
    """The account authority implied by ``status.mode`` alone.

    Used only when a caller has no facade-derived override to pass
    ``build_panel`` (every direct test in ``test_panel_projection.py``, plus
    any future caller that hasn't selected a SQLite authority yet).
    """
    if status.mode == "dry_run":
        return synthetic_account_id_for_strategy(status.strategy_instance_id)
    return account_id


def reject_mixed_authority(
    *,
    authority_account_id: str,
    authority_kind: AuthorityKind,
    decision_views: list[RecentDecisionView],
    fill_views: list[RecentFillView],
) -> None:
    """Refuse a panel's decision/fill aggregate spanning more than one authority.

    Convention alone (each call happens to read one authority because of how
    the caller branched on ``mode``) is not a check; this makes a violation
    impossible to serve.

    Every row reaching this point must already carry its authority metadata
    (``panel_projection_service._recent_activity_views`` stamps it on every
    branch); a row that doesn't is itself a projection bug, so this fails
    closed rather than silently skipping it.
    """
    rows: list[AuthorityScopedRow] = []
    for view in (*decision_views, *fill_views):
        if view.authority_account_id is None or view.authority_kind is None:
            raise MixedAuthorityAggregateError(
                "panel evidence row is missing its account authority metadata"
            )
        rows.append(
            AuthorityScopedRow(
                account_id=view.authority_account_id,
                authority_kind=view.authority_kind,
            )
        )
    try:
        SingleAuthorityAggregate(
            account_id=authority_account_id,
            authority_kind=authority_kind,
            rows=tuple(rows),
        )
    except ValidationError as exc:
        raise MixedAuthorityAggregateError(str(exc)) from exc
