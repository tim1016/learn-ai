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

    quantities: dict[tuple[str, str, str], float] = defaultdict(float)
    seen_execution_effects: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.entry_kind == "operator_adjustment" and entry.operator_adjustment is not None:
            adjustment = entry.operator_adjustment
            if group_by != "namespace" or (
                account_id is not None and adjustment.account_id != account_id
            ):
                continue
            quantities[
                (adjustment.account_id, adjustment.bot_order_namespace, adjustment.symbol)
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
        quantity = float(event.fill_quantity)
        if not math.isfinite(quantity):
            continue
        execution_key = (entry.intent.account_id, event.exec_id)
        if execution_key in seen_execution_effects:
            continue
        seen_execution_effects.add(execution_key)
        group_id = (
            entry.intent.bot_order_namespace
            if group_by == "namespace"
            else entry.intent.strategy_instance_id
        )
        signed_quantity = quantity if event.side == "BUY" else -quantity
        quantities[(entry.intent.account_id, group_id, event.symbol.upper())] += signed_quantity

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
    seen_execution_effects: set[tuple[str, str]] = set()
    for entry in entries:
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
        if not math.isfinite(quantity):
            continue
        execution_key = (event_account_id, event.exec_id)
        if execution_key in seen_execution_effects:
            continue
        seen_execution_effects.add(execution_key)
        signed_quantity = quantity if event.side == "BUY" else -quantity
        quantities[(event_account_id, event.symbol.upper())] += signed_quantity

    return tuple(
        AccountJournalExposure(account_id=projected_account_id, symbol=symbol, quantity=quantity)
        for (projected_account_id, symbol), quantity in sorted(quantities.items())
        if quantity != 0.0
    )


__all__ = [
    "AccountJournalExposure",
    "JournalExposure",
    "JournalExposureGroup",
    "normalize_broker_event",
    "normalize_journal_broker_event",
    "project_journal_account_exposure",
    "project_journal_exposure",
]
