"""The v11-to-v12 carry-across of ``holds`` into ``uncertainties``.

ADR 0048 Decision 2 retires the ``holds`` table; ``schema.SCHEMA_MIGRATIONS``
drops it and republishes ``holds`` as a view. The rows have to move first,
and moving them is a domain question — which causes have a registered policy,
what R5 envelope describes each one, what a half-stated row means — not a DDL
one. It lives here so ``schema`` stays what its docstring says it is: the
pinned DDL and the version ladder.
"""

from __future__ import annotations

import json
import sqlite3

from app.broker.alpaca.clerk.sqlite.uncertainty_causes import normalized_hold_reason_code
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import insert_account_hold_episode


class HoldMigrationBlocked(ValueError):
    """A v11 ``holds`` row cannot be carried into ``uncertainties`` truthfully."""


def backfill_holds_into_uncertainties(conn: sqlite3.Connection) -> None:
    """Move every ``holds`` row into ``uncertainties`` (ADR 0048 Decision 2).

    ``uncertainty_id`` *is* the row's original ``hold_id``, so the id an
    operator saw before the upgrade is the id they see after it, and the two
    namespaces cannot collide — a hold fold minted ``hold:<sequence>``, an
    uncertainty fold ``uncertainty:<sequence>``. A collision would raise on
    the unique constraint rather than being absorbed, which is the outcome
    worth having: a v12 that silently dropped a blocking episode would remove
    an account-wide entry fence without a word.

    Resolved holds migrate too. They are the timeline evidence behind
    ``custody_transitions``; dropping them would silently rewrite history to
    say the account was never held.

    Raises :class:`HoldMigrationBlocked` rather than guessing whenever a row
    cannot be carried truthfully. A blocked migration rolls back whole (the
    caller runs it inside one transaction) and leaves a readable v11 file,
    which is a far better outcome than an authority whose entry fence was
    reconstructed from an assumption.
    """
    # Positional, not by name: ``migrate_schema`` runs on whatever connection
    # the caller opened, and only some of them set ``row_factory``.
    rows = conn.execute(
        "SELECT hold_id, scope, reason_code, state, opened_at_ms, resolved_at_ms, "
        "evidence_refs_json FROM holds ORDER BY hold_id"
    ).fetchall()
    for (
        hold_id,
        scope,
        stored_reason_code,
        state,
        opened_at_ms,
        resolved_at_ms,
        evidence_refs_json,
    ) in rows:
        insert_account_hold_episode(
            conn,
            uncertainty_id=hold_id,
            reason_code=_registered_reason_code(hold_id, stored_reason_code, scope),
            evidence_refs=_readable_evidence_refs(hold_id, evidence_refs_json),
            observed_at_ms=opened_at_ms,
            resolved_at_ms=_resolution_stamp(
                hold_id, state=state, opened_at_ms=opened_at_ms, resolved_at_ms=resolved_at_ms
            ),
        )


def _registered_reason_code(hold_id: str, stored_reason_code: str, scope: str) -> str:
    """The v12 spelling of a hold's cause, or a refusal to guess."""
    try:
        reason_code = normalized_hold_reason_code(stored_reason_code)
    except KeyError as exc:
        raise HoldMigrationBlocked(
            f"hold {hold_id!r} carries unregistered reason code "
            f"{stored_reason_code!r}; register its policy before upgrading to v12"
        ) from exc
    if scope != "ACCOUNT_CLERK":
        # Both registered causes are account-scoped and the pre-v12 table
        # CHECK already forbade a subject-scoped row for them. A row that
        # reaches here anyway would need a subject-scoped policy this
        # migration has not declared.
        raise HoldMigrationBlocked(
            f"hold {hold_id!r} is {scope}-scoped; only ACCOUNT_CLERK holds "
            "have a registered v12 policy"
        )
    return reason_code


def _resolution_stamp(
    hold_id: str, *, state: str, opened_at_ms: int, resolved_at_ms: int | None
) -> int | None:
    """One episode's resolution instant, from a table that allowed both to disagree."""
    if state == "RESOLVED" and resolved_at_ms is None:
        # Never observed, but the pre-v12 table did not constrain the pair.
        # Falling back to opened_at_ms keeps the episode resolved; treating
        # it as active would resurrect a closed account-wide entry fence.
        return opened_at_ms
    if state == "ACTIVE" and resolved_at_ms is not None:
        raise HoldMigrationBlocked(
            f"hold {hold_id!r} is ACTIVE with a resolution timestamp; "
            "its state cannot be expressed as one uncertainty episode"
        )
    return resolved_at_ms


def _readable_evidence_refs(hold_id: str, evidence_refs_json: str | None) -> list[str]:
    evidence_refs = json.loads(evidence_refs_json or "[]")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(ref, str) for ref in evidence_refs
    ):
        raise HoldMigrationBlocked(f"hold {hold_id!r} has an unreadable evidence_refs_json")
    return evidence_refs
