"""Canonical Account Clerk journal-to-exposure projection.

Formula: exposure[account, group, symbol] = Σ (+fill_quantity for BUY,
  -fill_quantity for SELL), once per (account, non-empty exec_id); zero
  balances are omitted.
Reference: learn-ai issue #1038, locked decision 30; issue #1039.
Canonical implementation: this file.
Validated against: tests/engine/live/test_journal_exposure.py::test_project_journal_exposure_matches_golden_fixture.

The journal ``seq`` is a delivery-replay identity. It must never be used to
deduplicate an execution effect: only the broker's ``exec_id`` owns that
responsibility. Callback idempotency is deliberately outside this projection.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_clerk_journal import AccountClerkJournalEntry, normalize_broker_event

JournalExposureGroup = Literal["namespace", "strategy_instance"]


@dataclass(frozen=True)
class ExecutionExposureEffect:
    """One normalized broker execution effect for the canonical fill fold."""

    account_id: str
    group_id: str
    symbol: str
    execution_id: str
    signed_quantity: float


def fold_execution_exposure(
    effects: Iterable[ExecutionExposureEffect],
) -> dict[tuple[str, str, str], float]:
    """Fold normalized execution effects into account/group/symbol quantities.

    Formula: exposure[account, group, symbol] =
      Σ signed_quantity once per (account, non-empty execution_id);
      exact zero balances are omitted.
    Reference: learn-ai issue #1038 locked decision 30 and issue #1261
      Alpaca Clerk ownership invariants.
    Canonical implementation: this file.
    Validated against:
      tests/engine/live/test_journal_exposure.py::
        test_fold_execution_exposure_normalizes_and_deduplicates;
      tests/broker/alpaca/clerk/test_instance_orders.py::
        test_alpaca_projection_uses_canonical_execution_fold.

    Vendor adapters own only normalization into this shape. Deduplication,
    finite-number validation, symbol normalization, signing, and zero pruning
    live here so IBKR and Alpaca cannot silently evolve different arithmetic.
    """
    quantities: dict[tuple[str, str, str], float] = defaultdict(float)
    seen_execution_effects: set[tuple[str, str]] = set()
    for effect in effects:
        if not effect.execution_id or not math.isfinite(effect.signed_quantity):
            continue
        execution_key = (effect.account_id, effect.execution_id)
        if execution_key in seen_execution_effects:
            continue
        seen_execution_effects.add(execution_key)
        quantities[
            (effect.account_id, effect.group_id, effect.symbol.upper())
        ] += effect.signed_quantity
    return {
        key: quantity
        for key, quantity in sorted(quantities.items())
        if quantity != 0.0
    }


@dataclass(frozen=True)
class JournalExposure:
    """One non-zero signed exposure bucket from Clerk journal fill effects."""

    account_id: str
    group_by: JournalExposureGroup
    group_id: str
    symbol: str
    quantity: float


@dataclass(frozen=True)
class AccountJournalExposure:
    """One non-zero account-level bucket, including unattributed callbacks."""

    account_id: str
    symbol: str
    quantity: float


def normalize_journal_broker_event(entry: AccountClerkJournalEntry) -> IbkrOrderEvent | None:
    """Return a trusted Clerk callback, or ``None`` when it cannot affect exposure.

    The Clerk writes ``IbkrOrderEvent.model_dump(mode="json")`` into the
    journal, while drain clients validate the same model at their wire
    boundary. Re-validating here keeps the exposure fold on exactly that
    normalized event shape and rejects an inconsistent account or order ref
    rather than allowing a malformed row to cross an account/intent boundary.
    """

    if entry.entry_kind != "broker_event" or entry.broker_event is None:
        return None
    event = normalize_broker_event(entry.broker_event)
    if event is None:
        return None
    expected_account_id = entry.event_account_id
    if expected_account_id is None and entry.intent is not None:
        # Pre-#1044 attributed rows did not carry explicit callback-account
        # metadata. Their durable intent remains the compatibility source.
        expected_account_id = entry.intent.account_id
    if event.account_id != expected_account_id:
        return None
    if entry.intent is not None and event.order_ref != entry.intent.order_ref:
        return None
    return event


def project_journal_exposure(
    entries: Iterable[AccountClerkJournalEntry],
    *,
    group_by: JournalExposureGroup = "namespace",
    account_id: str | None = None,
) -> tuple[JournalExposure, ...]:
    """Project journaled fill effects into non-zero account-scoped exposure.

    ``account_id`` narrows a multi-account input without changing the grouping
    key. Without it, account remains part of every output key, so rows from
    different account journals can never net against one another.
    """

    if group_by not in {"namespace", "strategy_instance"}:
        raise ValueError(f"unsupported journal exposure grouping: {group_by!r}")

    entries = tuple(entries)
    quantities: dict[tuple[str, str, str], float] = defaultdict(float)
    execution_effects: list[ExecutionExposureEffect] = []
    for entry in entries:
        if entry.entry_kind == "operator_adjustment" and entry.operator_adjustment is not None:
            adjustment = entry.operator_adjustment
            if account_id is not None and adjustment.account_id != account_id:
                continue
            group_id = (
                adjustment.bot_order_namespace
                if group_by == "namespace"
                else _strategy_instance_for_namespace(entries, adjustment.bot_order_namespace)
            )
            if group_id is None:
                # A cure is only accepted for a retired registry namespace;
                # no strategy projection is possible without its durable owner.
                continue
            quantities[
                (adjustment.account_id, group_id, adjustment.symbol)
            ] += adjustment.signed_quantity
            continue
        event = normalize_journal_broker_event(entry)
        if event is None or entry.intent is None:
            # Namespace and strategy projections intentionally never invent an
            # owner for unknown broker flow. Account truth is folded below.
            continue
        if account_id is not None and entry.intent.account_id != account_id:
            continue
        if event.event_type != "fill" or not event.exec_id:
            continue
        if event.symbol is None or event.side is None or event.fill_quantity is None:
            continue
        group_id = (
            entry.intent.bot_order_namespace
            if group_by == "namespace"
            else entry.intent.strategy_instance_id
        )
        quantity = float(event.fill_quantity)
        execution_effects.append(
            ExecutionExposureEffect(
                account_id=entry.intent.account_id,
                group_id=group_id,
                symbol=event.symbol,
                execution_id=event.exec_id,
                signed_quantity=quantity if event.side == "BUY" else -quantity,
            )
        )

    for key, quantity in fold_execution_exposure(execution_effects).items():
        quantities[key] += quantity

    return tuple(
        JournalExposure(
            account_id=projected_account_id,
            group_by=group_by,
            group_id=group_id,
            symbol=symbol,
            quantity=quantity,
        )
        for (projected_account_id, group_id, symbol), quantity in sorted(quantities.items())
        if quantity != 0.0
    )


def _strategy_instance_for_namespace(
    entries: Iterable[AccountClerkJournalEntry],
    namespace: str,
) -> str | None:
    """Return the sole journal-proven strategy owner for a cured namespace."""

    owners = {
        entry.intent.strategy_instance_id
        for entry in entries
        if entry.intent is not None and entry.intent.bot_order_namespace == namespace
    }
    return next(iter(owners)) if len(owners) == 1 else None


def project_journal_account_exposure(
    entries: Iterable[AccountClerkJournalEntry],
    *,
    account_id: str | None = None,
) -> tuple[AccountJournalExposure, ...]:
    """Project all journaled fill effects into account truth.

    Formula: exposure[account, symbol] = Σ (+fill_quantity for BUY,
      -fill_quantity for SELL), once per (account, non-empty exec_id).
    Reference: learn-ai issue #1038, locked decision 22 and 30; issue #1044.
    Canonical implementation: this file.
    Validated against: tests/engine/live/test_journal_exposure.py::test_account_projection_includes_unattributed_callbacks.

    Unlike ``project_journal_exposure``, this fold includes a callback with no
    Clerk intent. That makes manual/foreign account flow observable without
    assigning a fabricated namespace or strategy owner.
    """

    quantities: dict[tuple[str, str], float] = defaultdict(float)
    execution_effects: list[ExecutionExposureEffect] = []
    for entry in entries:
        baseline = entry.broker_evidence_baseline
        if entry.entry_kind == "broker_evidence_baseline" and baseline is not None:
            if account_id is None or baseline.account_id == account_id:
                for position in baseline.positions:
                    quantities[(baseline.account_id, position.symbol.upper())] += position.signed_quantity
            continue
        event = normalize_journal_broker_event(entry)
        if event is None or event.event_type != "fill" or not event.exec_id:
            continue
        if event.symbol is None or event.side is None or event.fill_quantity is None:
            continue
        event_account_id = entry.event_account_id
        if event_account_id is None and entry.intent is not None:
            event_account_id = entry.intent.account_id
        if event_account_id is None or (account_id is not None and event_account_id != account_id):
            continue
        quantity = float(event.fill_quantity)
        execution_effects.append(
            ExecutionExposureEffect(
                account_id=event_account_id,
                group_id="account",
                symbol=event.symbol,
                execution_id=event.exec_id,
                signed_quantity=quantity if event.side == "BUY" else -quantity,
            )
        )

    for (projected_account_id, _group_id, symbol), quantity in (
        fold_execution_exposure(execution_effects).items()
    ):
        quantities[(projected_account_id, symbol)] += quantity

    return tuple(
        AccountJournalExposure(account_id=projected_account_id, symbol=symbol, quantity=quantity)
        for (projected_account_id, symbol), quantity in sorted(quantities.items())
        if quantity != 0.0
    )


__all__ = [
    "AccountJournalExposure",
    "ExecutionExposureEffect",
    "JournalExposure",
    "JournalExposureGroup",
    "fold_execution_exposure",
    "normalize_broker_event",
    "normalize_journal_broker_event",
    "project_journal_account_exposure",
    "project_journal_exposure",
]
