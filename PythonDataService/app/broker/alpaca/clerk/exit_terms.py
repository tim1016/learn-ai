"""Immutable execution terms, independent of the strategy's signal identity.

The registration's append-only seal is the authority after deployment. Profile
values are consulted only while deploying or backfilling an older registration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.schemas.exit_terms import ExitTerms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


def read_exit_terms(repo: ClerkSqliteRepository, strategy_instance_id: str) -> ExitTerms | None:
    row = repo.last_strategy_transition(
        strategy_instance_id=strategy_instance_id,
        transition_kind="EXIT_TERMS_SEALED",
    )
    if row is not None:
        return ExitTerms.model_validate_json(row["facts_json"])
    registration = repo.last_strategy_transition(
        strategy_instance_id=strategy_instance_id,
        transition_kind="STRATEGY_INSTANCE_REGISTERED",
    )
    payload = None if registration is None else json.loads(registration["facts_json"]).get("exit_terms")
    return None if payload is None else ExitTerms.model_validate(payload)


def seal_exit_terms(repo: ClerkSqliteRepository, strategy_instance_id: str, terms: ExitTerms) -> ExitTerms:
    """Create once under the Clerk's intake lock; never rewrite an existing seal."""
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    existing = read_exit_terms(repo, strategy_instance_id)
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
    if terms is None or terms.exit_allowance_bps is None:
        return replace(policy, allowances=None, allowance_refusal=None)
    entry_bps = policy.allowances.entry_bps if policy.allowances is not None else Decimal(0)
    return replace(
        policy,
        allowance_refusal=None,
        allowances=ExtendedHoursAllowances(
            entry_bps=entry_bps,
            exit_bps=Decimal(str(terms.exit_allowance_bps)),
            exit_band_multiple=Decimal(str(terms.band_multiple)),
            exit_spread_cap_bps=Decimal(str(terms.spread_cap_bps)),
        ),
    )


def backfilled_terms(policy: ProgramLegPolicy) -> ExitTerms:
    from app.broker.alpaca.clerk.program_leg import legacy_recovery_pricing

    values = policy.allowances
    legacy = legacy_recovery_pricing(values or ExtendedHoursAllowances.from_bps(entry_bps=0, exit_bps=0))
    return ExitTerms(
        exit_allowance_bps=None if values is None else float(values.exit_bps),
        band_multiple=float(legacy.exit_band_multiple),
        spread_cap_bps=float(legacy.exit_spread_cap_bps),
        provenance="backfilled",
    )
