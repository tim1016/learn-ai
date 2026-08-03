"""Pure account-scoped Clerk↔broker custody-diagnosis fold tests (task 1.1),
plus the clerk-method + endpoint seam tests (task 1.2)."""

from __future__ import annotations

from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.test_clerk_reconciliation import (
    _FIXED_MS,
    _clerk_root,  # noqa: F401 -- autouse fixture, imported for its side effect
    _FakeBroker,
    _fixed_clock,
    _position,
)


def _intent(order_ref: str) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.INTENT_RECORDED,
        account_id="A",
        operator="op",
        intent_id="i",
        order_ref=order_ref,
        client_order_id=order_ref,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        recorded_at_ms=_FIXED_MS,
    )


def test_attribution_mismatch_reports_per_symbol_delta() -> None:
    # Journal expects nothing (no baseline, no terminal orders); broker holds 1 SPY.
    entries: list[OrderJournalEntry] = []
    positions = [_position()]

    divergences = diagnosis.diagnose_custody(
        entries, orders=[], positions=positions, namespaces=frozenset()
    )

    assert len(divergences) == 1
    d = divergences[0]
    assert d.kind == "exposure_attribution_mismatch"
    assert d.state == "resolvable_now"
    assert d.resolution_step == "record_inventory_baseline"
    assert d.position_deltas == (
        diagnosis.CustodyPositionDelta(
            symbol="SPY", clerk_attributed_qty=0.0, broker_observed_qty=1.0
        ),
    )
    assert d.possible_causes  # backend-authored, non-empty
    assert d.explanation  # backend-authored prose


def test_flat_and_reconciled_account_has_no_divergence() -> None:
    assert diagnosis.diagnose_custody([], orders=[], positions=[], namespaces=frozenset()) == ()


def test_needs_review_reported_when_no_deltas_but_unresolved_intent() -> None:
    # A submitted intent that never reached submit_acked/submit_failed, and a
    # broker that is flat with no working orders (so there is no attribution
    # delta to fold into) — the false-all-clear gap (Task 3.3, Part A).
    entries = [_intent("manual/ops/v1:i1")]

    divergences = diagnosis.diagnose_custody(
        entries, orders=[], positions=[], namespaces=frozenset()
    )

    assert len(divergences) == 1
    d = divergences[0]
    assert d.kind == "needs_review"
    assert d.state == "needs_review"
    assert d.evidence_refs == ("manual/ops/v1:i1",)
    assert d.resolution_step is None
    assert d.possible_causes
    assert d.explanation
    # The account must NOT be reported in sync (clerk.py: in_sync = not divergences).
    assert bool(divergences) is True
    # resolution_plan must exclude a needs_review divergence -> resolvable=False
    # at the clerk-method level (CustodyDiagnosis.resolvable = bool(plan)).
    assert diagnosis.resolution_plan(divergences) == ()


# ── AlpacaClerk.custody_diagnosis() (Style A: direct clerk) ─────────────────


async def test_custody_diagnosis_reports_missing_intent_mismatch() -> None:
    broker = _FakeBroker(orders=[], positions=[_position(symbol="SPY", quantity=1, side="long")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is False
    assert result.resolvable is True
    assert [s.action_id for s in result.resolution_plan] == [
        "reconcile_now",
        "record_inventory_baseline",
    ]
    assert result.divergences[0].kind == "exposure_attribution_mismatch"
    assert result.divergences[0].position_deltas[0].broker_observed_qty == 1.0
    assert result.snapshot_version  # non-empty guard token


async def test_custody_diagnosis_needs_review_when_unresolved_intent_has_no_delta() -> None:
    # The false-all-clear gap (Task 3.3, Part A) exercised end-to-end through
    # custody_diagnosis(): a journaled intent that never reached
    # submit_acked/submit_failed, with a broker that is flat and has no
    # working orders (no attribution delta to fold into). `resolvable` and
    # `in_sync` must come back False/False off the actual CustodyDiagnosis —
    # not inferred from the intermediate resolution_plan() call.
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    account_id, journal = await clerk._ensure_journal()  # type: ignore[attr-defined]
    journal.append(
        OrderJournalEntry(
            kind=ClerkEntryKind.INTENT_RECORDED,
            account_id=account_id,
            operator="ops",
            intent_id="i1",
            order_ref="manual/ops/v1:i1",
            client_order_id="manual/ops/v1:i1",
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            recorded_at_ms=_FIXED_MS,
        )
    )

    result = await clerk.custody_diagnosis()

    assert result.in_sync is False
    assert result.resolvable is False
    assert len(result.divergences) == 1
    assert result.divergences[0].kind == "needs_review"
    assert result.divergences[0].evidence_refs == ("manual/ops/v1:i1",)
    assert result.resolution_plan == ()


async def test_custody_diagnosis_flat_account_is_in_sync() -> None:
    broker = _FakeBroker(orders=[], positions=[])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is True
    assert result.divergences == ()
    assert result.resolution_plan == ()


async def test_custody_diagnosis_stale_reconciliation_when_broker_unavailable() -> None:
    # The broker-unreachable gap (Task 3.3, Part B): a 503 from the broker read
    # must surface as a graceful stale_reconciliation diagnosis, not an
    # uncaught 503 out of the endpoint.
    broker = _FakeBroker(
        list_error=BrokerUnavailable("broker unavailable", broker="alpaca")
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    result = await clerk.custody_diagnosis()

    assert result.in_sync is False
    assert result.resolvable is True
    assert len(result.divergences) == 1
    d = result.divergences[0]
    assert d.kind == "stale_reconciliation"
    assert d.resolution_step == "reconcile_now"
    assert d.possible_causes
    assert d.explanation
    assert [s.action_id for s in result.resolution_plan] == ["reconcile_now"]
    assert result.resolution_plan[0].mutates is False

    # A genuine flat/in-sync diagnosis (empty orders, empty positions, same
    # empty journal) must produce a DIFFERENT snapshot_version — the stale
    # payload shape must never alias a real one (409 concurrency guard).
    flat_broker = _FakeBroker(orders=[], positions=[])
    flat_clerk = AlpacaClerk(read=flat_broker, trade=flat_broker, clock=_fixed_clock)
    flat_result = await flat_clerk.custody_diagnosis()

    assert result.snapshot_version != flat_result.snapshot_version
