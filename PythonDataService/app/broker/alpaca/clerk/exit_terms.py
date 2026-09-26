"""Immutable execution terms, independent of the strategy's signal identity.

The registration's append-only seal is the authority after deployment. Profile
values are consulted only while deploying or backfilling an older registration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.schemas.exit_terms import ExitTerms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


def read_exit_terms(conn: sqlite3.Connection, strategy_instance_id: str) -> ExitTerms | None:
    """The one reader of the custody-owned, journal-rebuildable terms seal."""
    row = conn.execute("SELECT terms_json FROM strategy_exit_terms WHERE strategy_instance_id = ?", (strategy_instance_id,)).fetchone()
    return None if row is None else ExitTerms.model_validate_json(row[0])


def registered_exit_terms_at(db_path: Path, strategy_instance_id: str) -> ExitTerms | None:
    """Read the same seal without acquiring the live writer's lease."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return read_exit_terms(conn, strategy_instance_id)
    except sqlite3.DatabaseError as exc:
        raise ValueError("Custody exit terms are unavailable; complete the Clerk upgrade before arming.") from exc
    finally:
        conn.close()


def seal_exit_terms(repo: ClerkSqliteRepository, strategy_instance_id: str, terms: ExitTerms) -> ExitTerms:
    """Create once under the Clerk's intake lock; never rewrite an existing seal."""
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    existing = repo.exit_terms(strategy_instance_id)
    if existing is not None:
        if existing != terms:
            raise ValueError(f"Exit terms are immutable for {strategy_instance_id}")
        return existing
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="EXIT_TERMS_SEALED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXIT_TERMS_SEALED",
            facts_json=terms.model_dump_json(),
        )
    )
    return terms


def policy_with_exit_terms(policy: ProgramLegPolicy, terms: ExitTerms | None) -> ProgramLegPolicy:
    """One adapter for decision, send, recovery, flatten and simulated exit pricing."""
    if terms is None:
        raise ValueError("This bot has no custody-sealed exit terms.")
    entry_bps = policy.allowances.entry_bps if policy.allowances is not None else None
    return replace(
        policy,
        allowance_refusal=None,
        allowances=ExtendedHoursAllowances(
            entry_bps=entry_bps,
            exit_bps=None if terms.exit_allowance_bps is None else Decimal(str(terms.exit_allowance_bps)),
            exit_band_multiple=Decimal(str(terms.band_multiple)),
            exit_spread_cap_bps=Decimal(str(terms.spread_cap_bps)),
        ),
    )


def backfilled_terms(policy: ProgramLegPolicy) -> ExitTerms:
    from app.broker.alpaca.clerk.program_leg import legacy_recovery_pricing

    values = policy.allowances
    legacy = legacy_recovery_pricing(values or ExtendedHoursAllowances.from_bps(entry_bps=0, exit_bps=0))
    return ExitTerms(
        exit_allowance_bps=None if values is None or values.exit_bps is None else float(values.exit_bps),
        band_multiple=float(legacy.exit_band_multiple),
        spread_cap_bps=float(legacy.exit_spread_cap_bps),
        provenance="backfilled",
    )


def fold_exit_terms(conn: sqlite3.Connection, sid: str, terms: ExitTerms) -> None:
    """A registration/upgrade may seal once; replay never changes an existing value."""
    existing = read_exit_terms(conn, sid)
    if existing is not None:
        if existing != terms:
            raise ValueError(f"Exit terms are immutable for {sid}")
        return
    conn.execute("INSERT INTO strategy_exit_terms VALUES (?, ?)", (sid, terms.model_dump_json()))


def upgrade_exit_terms(repo: ClerkSqliteRepository, policy_for: Callable[[str], ProgramLegPolicy]) -> None:
    """One explicit legacy upgrade, after the account's arming seal was refreshed."""
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    if repo.exit_terms_upgrade_completed():
        return
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        if repo.exit_terms(sid) is None:
            seal_exit_terms(repo, sid, backfilled_terms(policy_for(sid)))
    repo.append_transition(TransitionInput(
        transition_kind="EXIT_TERMS_UPGRADE_COMPLETED", custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
        clerk_observed_at_ms=repo.clock(), summary_code="EXIT_TERMS_UPGRADE_COMPLETED", facts_json="{}",
    ))
