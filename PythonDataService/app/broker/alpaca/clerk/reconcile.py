"""Pure reconciliation policy for the Alpaca Clerk (phase 2, S6).

Decides the sweep verdict and the exact journal entries a pass should append,
given a pre-read ledger and the broker's live orders/positions. Pure — no I/O,
no lock, no Clerk state — so the policy (verdict priority, unexplained-order
dedup, verdict-on-change) is testable in isolation and lives in one place. The
Clerk keeps only the single-writer concerns: the intake lock, the broker reads,
and the journal append (see ``clerk.reconcile_once``).

Growth is bounded on purpose: an ``UNEXPLAINED_ORDER`` line is emitted only for a
foreign order not already recorded, and a ``RECONCILIATION`` line only when the
verdict *changes* from the last recorded one. A persistent foreign order (or a
run of clean passes) therefore appends nothing after the first — the ledger
records transitions, not sweeps, which matters because every hold-check, submit,
and startup replay scans the whole ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderJournalEntry,
    ReconciliationVerdict,
)
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.engine.live.order_identity import order_ref_namespace_matches


@dataclass
class ReconcilePlan:
    """What one reconciliation pass decided, for the Clerk to apply under lock."""

    verdict: ReconciliationVerdict
    entries_to_append: list[OrderJournalEntry] = field(default_factory=list)
    set_hold: bool = False
    new_unexplained_count: int = 0


def _verdict_line(
    account_id: str,
    verdict: ReconciliationVerdict,
    now_ms: int,
    entries: list[OrderJournalEntry],
) -> list[OrderJournalEntry]:
    """A RECONCILIATION line only when the verdict changed from the last one."""
    latest = derive.latest_reconciliation(entries)
    if latest is not None and latest.verdict == verdict:
        return []
    return [
        OrderJournalEntry(
            kind=ClerkEntryKind.RECONCILIATION,
            account_id=account_id,
            verdict=verdict,
            recorded_at_ms=now_ms,
        )
    ]


def plan_stale(
    entries: list[OrderJournalEntry], *, account_id: str, now_ms: int
) -> ReconcilePlan:
    """A broker read failed — record ``stale`` (on change) and touch nothing else."""
    return ReconcilePlan(
        verdict="stale",
        entries_to_append=_verdict_line(account_id, "stale", now_ms, entries),
    )


def plan(
    entries: list[OrderJournalEntry],
    orders: list[BrokerOrder],
    positions: list[BrokerPosition],
    namespaces: frozenset[str],
    *,
    account_id: str,
    now_ms: int,
) -> ReconcilePlan:
    """Decide the verdict and the entries to append for one reconciliation pass.

    Priority (highest first): ``unexplained_order`` (a foreign order — journal the
    new ones + raise the hold) → ``missing_intent`` (owned drift; observational) →
    ``clean``. ``stale`` is decided by the caller when a broker read fails.
    """
    foreign = [
        order
        for order in orders
        if not order_ref_namespace_matches(order.client_order_id, namespaces)
    ]
    if foreign:
        already = derive.unexplained_order_ids(entries)
        new_foreign = [order for order in foreign if order.order_id not in already]
        appended = [
            OrderJournalEntry(
                kind=ClerkEntryKind.UNEXPLAINED_ORDER,
                account_id=account_id,
                client_order_id=order.client_order_id or "",
                broker_order_id=order.order_id,
                owned=False,
                order=order,
                recorded_at_ms=now_ms,
            )
            for order in new_foreign
        ]
        appended += _verdict_line(account_id, "unexplained_order", now_ms, entries)
        # Hold on ANY foreign order present (not only newly-seen ones): if an
        # operator cleared the hold while the order persisted, the next pass must
        # re-raise it. ``_set_hold`` is idempotent, so this is a no-op while held.
        return ReconcilePlan(
            verdict="unexplained_order",
            entries_to_append=appended,
            set_hold=True,
            new_unexplained_count=len(new_foreign),
        )

    if derive.has_missing_intent(entries, orders, positions):
        return ReconcilePlan(
            verdict="missing_intent",
            entries_to_append=_verdict_line(
                account_id, "missing_intent", now_ms, entries
            ),
        )

    return ReconcilePlan(
        verdict="clean",
        entries_to_append=_verdict_line(account_id, "clean", now_ms, entries),
    )
