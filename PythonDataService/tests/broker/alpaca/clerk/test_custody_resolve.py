"""Clerk ``resolve_custody()`` orchestration tests (task 2.1, Style A: direct clerk).

Composes the already-landed read-only ``custody_diagnosis()`` (task 1.1/1.2)
with the existing recovery verbs (``reconcile_once``,
``record_inventory_baseline``, ``clear_hold``) behind a snapshot guard. These
tests assert the operator's reason reaches the journal and that a stale
snapshot token is rejected before any mutation.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind
from tests.broker.alpaca.clerk.test_clerk_reconciliation import (
    _clerk_root,  # noqa: F401 -- autouse fixture, imported for its side effect
    _FakeBroker,
    _fixed_clock,
    _position,
)


async def test_resolve_adopts_baseline_and_journals_operator_reason() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops",
        reason="07-31 run was killed mid-fill; adopting broker truth.",
        snapshot_version=diag.snapshot_version,
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    # The operator comment is journaled on the baseline row.
    baseline = [
        e for e in clerk._journal.read_entries() if e.kind == ClerkEntryKind.BROKER_EVIDENCE_BASELINE  # type: ignore[union-attr]
    ]
    assert baseline[-1].operator == "ops"
    assert "adopting broker truth" in baseline[-1].reason


async def test_resolve_rejects_stale_snapshot() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    with pytest.raises(diagnosis.CustodySnapshotChangedError):
        await clerk.resolve_custody(operator="ops", reason="x", snapshot_version="stale-token")


async def test_resolve_already_in_sync_is_idempotent_noop() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    diag = await clerk.custody_diagnosis()

    receipt = await clerk.resolve_custody(
        operator="ops", reason="no-op check", snapshot_version=diag.snapshot_version
    )

    assert receipt.resolved is True
    assert receipt.in_sync is True
    assert receipt.steps_executed == ()
