"""Replayable broker fee evidence and a synchronous custody-fenced projection.

The transition stream stores provider facts, not balances or allocated charges.
The one fee allocator derives money from effective corrected fill lineage on
read. Consumers use this function inside their existing SQLite transaction;
there is no network I/O, separate fee ledger or independent cash authority.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.money import MoneyInputError, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reads import governing_acknowledgement
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PARAMS,
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PLACEHOLDERS,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.services.alpaca_fee_attribution import (
    FeeAttribution,
    FeeCharge,
    FeeFill,
    UnattributedCharge,
    activity_economics,
    attribute_session_fees,
    collapse_activity_deliveries,
)
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

FEE_EVIDENCE_KIND = "FEE_EVIDENCE_OBSERVED"
FEE_EVIDENCE_MAX_AGE_MS = 90_000


class FeeEvidenceFacts(BaseModel):
    """What one broker activity read added to the recorded evidence.

    ``activities`` holds only rows no earlier record retained: a first
    delivery, or an economically different copy of a known identity (which
    the projection treats as a conflict). The window facts describe the whole
    read, so coverage never depends on re-recording rows already retained.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at_ms: int = Field(ge=0)
    activities: list[BrokerActivity]
    # Oldest dated row anywhere in the read's newest-first window.
    oldest_occurred_at_ms: int | None
    history_complete: bool


@dataclass(frozen=True)
class _EvidenceReach:
    """How far back the recorded reads proved the activity history."""

    oldest_ms: int | None
    complete: bool

    def covers(self, day: date) -> bool:
        return self.complete or (self.oldest_ms is not None and self.oldest_ms < et_midnight_ms(day))


def _evidence_reach(snapshots: Sequence[FeeEvidenceFacts]) -> _EvidenceReach:
    """A day once proven covered stays covered: reach only ever extends."""
    return _EvidenceReach(
        min((row.oldest_occurred_at_ms for row in snapshots if row.oldest_occurred_at_ms is not None), default=None),
        any(row.history_complete for row in snapshots),
    )


def _recorded_evidence(conn: sqlite3.Connection) -> list[FeeEvidenceFacts]:
    return [
        FeeEvidenceFacts.model_validate_json(row[0])
        for row in conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE transition_kind = ? ORDER BY sequence",
            (FEE_EVIDENCE_KIND,),
        )
    ]


def fold_fee_evidence(_conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Strictly validate historical evidence; money is never materialized here."""
    FeeEvidenceFacts.model_validate_json(payload["facts_json"])


def record_fee_evidence(
    repo: ClerkSqliteRepository,
    activities: Sequence[BrokerActivity],
    *,
    checked_at_ms: int,
    history_complete: bool = False,
) -> bool:
    """Record what one successful read adds, then stamp producer freshness.

    Only rows no earlier record retained are appended, with the window facts
    coverage needs. An unchanged read appends nothing: like the account
    observation's cash freshness, liveness is this process's latest read
    time, not a custody fact. Returns whether custody grew.
    """
    if repo.account_id.startswith(("sim:", "shadow:")):
        raise ValueError("simulated custody cannot record real broker fee activities")
    with repo._write_lock:
        recorded = _recorded_evidence(repo._conn)
        retained: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for snapshot in recorded:
            for row in snapshot.activities:
                retained[row.activity_id].append(activity_economics(row))
        new_rows: list[BrokerActivity] = []
        for row in activities:
            economics = activity_economics(row)
            if economics not in retained[row.activity_id]:
                retained[row.activity_id].append(economics)
                new_rows.append(row)
        facts = FeeEvidenceFacts(
            checked_at_ms=checked_at_ms,
            activities=new_rows,
            oldest_occurred_at_ms=min(
                (row.occurred_at_ms for row in activities if row.occurred_at_ms is not None), default=None
            ),
            history_complete=history_complete,
        )
        grows = not recorded or bool(new_rows) or _evidence_reach([*recorded, facts]) != _evidence_reach(recorded)
        if grows:
            repo.append_transition(
                TransitionInput(
                    transition_kind=FEE_EVIDENCE_KIND,
                    custody_owner="ACCOUNT_CLERK",
                    execution_authority="ACCOUNT_CLERK",
                    operation_state="succeeded",
                    clerk_observed_at_ms=checked_at_ms,
                    summary_code=FEE_EVIDENCE_KIND,
                    facts_json=canonicalize(facts.model_dump(mode="json")),
                )
            )
        repo._fee_evidence_checked_at_ms = checked_at_ms
    return grows


def _normalized_or_none(value: object) -> Decimal | None:
    """The canonical normalizer as a fail-closed population check.

    External activity rows with malformed money must mark the population
    incomplete, not abort the whole projection with an input error.
    """
    try:
        return normalize_money(value)
    except MoneyInputError:
        return None


def _effective_fills(
    conn: sqlite3.Connection, *, simulated_fill_cutoff_ms: int | None = None
) -> tuple[dict[date, list[FeeFill]], set[str], bool]:
    account = conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0]
    records = effective_fill_records(conn, account_id=account)
    grouped: dict[date, list[FeeFill]] = defaultdict(list)
    complete = (
        conn.execute(
            "SELECT 1 FROM fills WHERE evidence_source = 'cumulative_recovery' AND NOT EXISTS (SELECT 1 FROM fills successor WHERE successor.superseded_execution_ref = fills.execution_id) LIMIT 1"
        ).fetchone()
        is None
    )
    # A terminal acknowledgement may arrive before its exact execution. Its
    # missing population cannot become a zero-fee day just because no fill
    # row (or separately raised uncertainty) exists yet. Reuse the existing
    # governing acknowledgement, including partially canceled orders. Compare
    # normalized recorded facts exactly: an execution-coverage epsilon cannot
    # forgive an unpriced debit at the money-admission boundary.
    witnessed_refs = {record.order_ref for record in records}
    with money_context():
        quantities: dict[str, Decimal] = defaultdict(Decimal)
        for record in records:
            quantities[record.order_ref] += normalize_money(record.quantity)
        for order in conn.execute("SELECT order_ref, broker_state FROM orders"):
            acknowledgement = governing_acknowledgement(conn, order["order_ref"])
            reported = None if acknowledgement is None else acknowledgement[1]
            if (
                (order["broker_state"] or "").lower() == "filled"
                and order["order_ref"] not in witnessed_refs
            ) or (reported is not None and normalize_money(reported) > quantities[order["order_ref"]]):
                complete = False
    orders = {row[0] for row in conn.execute("SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
    for record in records:
        if record.quantity == 0 or (
            simulated_fill_cutoff_ms is not None and record.filled_at_ms > simulated_fill_cutoff_ms
        ):
            continue
        grouped[et_date_at_ms(record.filled_at_ms)].append(
            FeeFill(
                fill_id=record.event_key,
                subject_id=record.sid,
                side=record.side,
                quantity=normalize_money(record.quantity),
                price=normalize_money(record.fill_price),
                native_order_id=record.native_order_id,
                reported_fee=None if record.fee is None else normalize_money(record.fee),
                observed_at_ms=record.recorded_at_ms,
            )
        )
        if record.native_order_id:
            orders.add(record.native_order_id)
    conflicts = conn.execute(
        f"SELECT 1 FROM uncertainties WHERE reason_code IN ({EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PLACEHOLDERS}) AND resolved_at_ms IS NULL LIMIT 1",
        EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PARAMS,
    ).fetchone()
    return grouped, orders, complete and conflicts is None


@money_context()
def custody_fee_attribution(
    conn: sqlite3.Connection, *, now_ms: int, evidence_checked_at_ms: int | None = None,
    from_ms: int | None = None, to_ms: int | None = None, simulated_fill_cutoff_ms: int | None = None,
) -> FeeAttribution:
    """Lifetime fee projection in the caller's custody snapshot (no network).

    Real accounts are fresh only while the producer's latest successful read
    (``evidence_checked_at_ms``, process-local, see ``fee_attribution`` on the
    repository) is within ``FEE_EVIDENCE_MAX_AGE_MS`` of ``now_ms``; missing
    freshness refuses like missing evidence.

    ``known`` gates new spending. Per-subject totals contain reported fill fees
    once; ``unobserved_cash_claim`` excludes those fees because the existing
    unseen-fill cash claim owns them. The observation cutoff is a consumer
    argument to that method and MUST come from the canonical cash observation.

    Simulated historical equity may select effective fills through an inclusive
    economic-time cutoff before applying the existing fee model. Corrections
    retain their root execution time. This is not a broker activity window:
    real accounts must retain their trade-date settlement/coverage semantics.
    Coverage is still checked against all current execution evidence; selecting
    a historical population never grants permission to ignore missing facts.

    Formula: existing session fee model/apportionment over selected effective fills.
    Reference: docs/references/alpaca-fee-attribution.md; PRD #2540.
    Canonical implementation: app.services.alpaca_fee_attribution.attribute_session_fees.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_fee_evidence.py.
    """
    account = conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0]
    simulated = account.startswith(("sim:", "shadow:"))
    if simulated_fill_cutoff_ms is not None and not simulated:
        raise ValueError("an economic fill cutoff requires simulated custody")
    grouped, owned_orders, population_complete = _effective_fills(
        conn, simulated_fill_cutoff_ms=simulated_fill_cutoff_ms
    )
    snapshots = [] if simulated else _recorded_evidence(conn)
    reach = _evidence_reach(snapshots)
    by_date: dict[date, dict[str, BrokerActivity]] = defaultdict(dict)
    undated: dict[str, BrokerActivity] = {}
    # Recorded order keeps each activity's first observation, so repeated
    # polling never turns a recognized fee back into an unrecognized claim.
    collapsed = collapse_activity_deliveries(
        activity for snapshot in snapshots for activity in snapshot.activities
    )
    conflicting = {
        et_date_at_ms(copy.occurred_at_ms)
        for copies in collapsed.conflicts.values()
        for copy in copies
        if copy.occurred_at_ms is not None
    }
    for activity in collapsed.unique.values():
        if activity.occurred_at_ms is None:
            if activity.activity_type in {"FEE", "FILL", "PARTIAL_FILL"}:
                undated[activity.activity_id] = activity
            continue
        by_date[et_date_at_ms(activity.occurred_at_ms)][activity.activity_id] = activity
    external_orders = {
        row[0]
        for row in conn.execute(
            "SELECT broker_order_id FROM external_orders WHERE filled_avg_price IS NOT NULL AND ABS(qty) >= 1e-9"
        )
    }
    witnessed_external: set[str] = set()
    external_fills: list[FeeFill] = []
    execution_ids = {row[0] for row in conn.execute("SELECT execution_id FROM fills WHERE execution_id IS NOT NULL")}
    from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import _execution_id_from_activity_id

    for day, rows in by_date.items():
        for row in rows.values():
            if row.activity_type not in {"FILL", "PARTIAL_FILL"}:
                continue
            if row.native_order_id in owned_orders:
                if _execution_id_from_activity_id(row.activity_id) not in execution_ids:
                    population_complete = False
                continue
            if (
                row.native_order_id is None
                or row.quantity is None
                or row.price is None
                or row.side not in {"buy", "sell"}
                or _normalized_or_none(row.quantity) is None
                or _normalized_or_none(row.price) is None
                or row.quantity <= 0
                or row.price <= 0
            ):
                population_complete = False
                continue
            witnessed_external.add(row.native_order_id)
            external = FeeFill(
                fill_id=row.activity_id,
                subject_id=f"external:{row.native_order_id}",
                side=OrderSide(row.side),
                quantity=normalize_money(row.quantity),
                price=normalize_money(row.price),
                native_order_id=row.native_order_id,
                observed_at_ms=row.observed_at_ms,
            )
            grouped[day].append(external)
            external_fills.append(external)
    population_complete = population_complete and external_orders <= witnessed_external
    shares = []
    unresolved = []
    unattributed = Decimal(0)
    unattributed_charges: list[UnattributedCharge] = []
    if undated:
        unresolved.append("A broker fee or fill has no economic date. Reconcile account activity evidence.")
        for row in undated.values():
            if row.activity_type == "FEE" and row.net_amount is not None:
                amount = -normalize_money(row.net_amount)
                unattributed += amount
                unattributed_charges.append(UnattributedCharge(row.activity_id, amount, row.observed_at_ms))
    observed = Decimal(0)
    predicted = Decimal(0)
    predicted_known = observed_known = True
    if not simulated and (
        not snapshots
        or evidence_checked_at_ms is None
        or not 0 <= now_ms - evidence_checked_at_ms <= FEE_EVIDENCE_MAX_AGE_MS
    ):
        unresolved.append("Account fee evidence is missing or stale. Refresh account evidence before deploying.")
    for day in sorted(
        set(grouped)
        | {day for day, rows in by_date.items() if any(row.activity_type == "FEE" for row in rows.values())}
    ):
        day_start = et_midnight_ms(day)
        if (from_ms is not None and day_start < from_ms) or (to_ms is not None and day_start >= to_ms):
            continue
        fees = [
            FeeCharge(
                row.activity_id,
                None if row.net_amount is None else -normalize_money(row.net_amount),
                row.observed_at_ms,
                row.native_order_id,
            )
            for row in by_date[day].values()
            if row.activity_type == "FEE"
        ]
        result = attribute_session_fees(
            trade_date=day,
            fills=grouped[day],
            charges=fees,
            population_complete=population_complete and day not in conflicting,
            activities_complete=simulated or reach.covers(day),
            simulated=simulated,
            session_ended=day < et_date_at_ms(now_ms),
            settlement_at_ms=et_midnight_ms(day + timedelta(days=1)),
        )
        shares.extend(result.shares)
        unresolved.extend(result.unresolved)
        unattributed += result.unattributed
        unattributed_charges.extend(result.unattributed_charges)
        if result.observed_total is None:
            observed_known = False
        else:
            observed += result.observed_total
        if result.predicted_total is None:
            predicted_known = False
        else:
            predicted += result.predicted_total
    if not population_complete:
        unresolved.append("Account fill coverage is incomplete. Reconcile account executions before deploying.")
    return FeeAttribution(
        tuple(shares),
        unattributed,
        tuple(dict.fromkeys(unresolved)),
        observed if observed_known else None,
        predicted if predicted_known else None,
        external_fills=tuple(external_fills),
        unattributed_charges=tuple(unattributed_charges),
    )
