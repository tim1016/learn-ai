"""Pure account-scoped Clerk↔broker custody-diagnosis fold tests (task 1.1),
plus the clerk-method + endpoint seam tests (task 1.2)."""

from __future__ import annotations

from app.broker.alpaca.clerk import diagnosis
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkPositionEvidence,
)
from tests.broker.alpaca.clerk.test_clerk_reconciliation import (
    _FIXED_MS,
    _clerk_root,  # noqa: F401 -- autouse fixture, imported for its side effect
    _FakeBroker,
    _fixed_clock,
    _order,
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


def test_exposure_delta_golden_cases_pin_aggregation_inflight_and_tolerance() -> None:
    """Pin the account exposure contract at strict-float ``atol=1e-9, rtol=0``.

    Reference and rationale: ``docs/references/clerk-custody-exposure-deltas.md``.
    """

    baseline = OrderJournalEntry(
        kind=ClerkEntryKind.BROKER_EVIDENCE_BASELINE,
        account_id="A",
        operator="ops",
        reason="fixture",
        recorded_at_ms=_FIXED_MS,
        broker_evidence_baseline=AccountClerkBrokerEvidenceBaseline(
            account_id="A",
            observed_at_ms=_FIXED_MS,
            positions=(
                AccountClerkPositionEvidence(
                    symbol="SPY",
                    signed_quantity=0.4,
                    evidence_observed_at_ms=_FIXED_MS,
                ),
                AccountClerkPositionEvidence(
                    symbol="SPY",
                    signed_quantity=0.6,
                    evidence_observed_at_ms=_FIXED_MS,
                ),
            ),
        ),
    )
    below = diagnosis.diagnose_custody(
        [baseline],
        orders=[],
        positions=[_position(quantity=1.0 + 0.5e-9)],
        namespaces=frozenset(),
    )
    above = diagnosis.diagnose_custody(
        [baseline],
        orders=[],
        positions=[_position(quantity=1.0 + 2e-9)],
        namespaces=frozenset(),
    )

    assert below == ()
    assert above[0].position_deltas[0].clerk_attributed_qty == 1.0
    assert above[0].position_deltas[0].broker_observed_qty == 1.0 + 2e-9

    order_ref = "manual/ops/v1:i1"
    inflight_entries = [
        _intent(order_ref),
        OrderJournalEntry(
            kind=ClerkEntryKind.SUBMIT_ACKED,
            account_id="A",
            operator="ops",
            intent_id="i",
            order_ref=order_ref,
            client_order_id=order_ref,
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            order=_order(client_order_id=order_ref),
            recorded_at_ms=_FIXED_MS,
        ),
    ]
    assert diagnosis.diagnose_custody(
        inflight_entries,
        orders=[_order(client_order_id=order_ref)],
        positions=[_position(quantity=1.0)],
        namespaces=frozenset({"manual/ops/v1"}),
    ) == ()


def test_foreign_working_order_prevents_false_in_sync() -> None:
    divergences = diagnosis.diagnose_custody(
        [],
        orders=[_order(client_order_id="foreign-client")],
        positions=[],
        namespaces=frozenset(),
    )

    assert [item.kind for item in divergences] == ["foreign_working_order"]
    assert divergences[0].state == "blocked_on_prerequisite"
    assert divergences[0].evidence_refs == ("broker-order-1",)


def test_snapshot_version_changes_when_order_ownership_namespace_changes() -> None:
    order = _order(client_order_id="manual/ops/v1:i1")

    foreign = diagnosis.custody_snapshot_version(
        [], [order], [], namespaces=frozenset()
    )
    owned = diagnosis.custody_snapshot_version(
        [], [order], [], namespaces=frozenset({"manual/ops/v1"})
    )

    assert owned != foreign


def test_running_bot_blocks_account_baseline_plan() -> None:
    divergences = diagnosis.diagnose_custody(
        [],
        orders=[],
        positions=[_position()],
        namespaces=frozenset(),
        bot_running=True,
    )

    assert divergences[0].state == "blocked_on_prerequisite"
    assert "running bot" in (divergences[0].prerequisite_detail or "")
    assert diagnosis.resolution_plan(divergences) == ()


def test_unresolved_delta_exposes_reconcile_prerequisite_step() -> None:
    divergences = diagnosis.diagnose_custody(
        [_intent("manual/ops/v1:i1")],
        orders=[],
        positions=[_position()],
        namespaces=frozenset({"manual/ops/v1"}),
    )

    assert divergences[0].state == "blocked_on_prerequisite"
    assert divergences[0].prerequisite_step == "reconcile_now"
    assert [step.action_id for step in diagnosis.resolution_plan(divergences)] == [
        "reconcile_now"
    ]


def test_durable_reconciliation_freeze_prevents_false_in_sync() -> None:
    entries = [
        OrderJournalEntry(
            kind=ClerkEntryKind.RECONCILIATION,
            account_id="A",
            verdict="stale",
            recorded_at_ms=_FIXED_MS,
        )
    ]

    divergences = diagnosis.diagnose_custody(
        entries, orders=[], positions=[], namespaces=frozenset()
    )

    assert [item.kind for item in divergences] == ["stale_reconciliation"]
    assert [step.action_id for step in diagnosis.resolution_plan(divergences)] == [
        "reconcile_now"
    ]


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
