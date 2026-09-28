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
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PARAMS,
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PLACEHOLDERS,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.services.alpaca_fee_attribution import FeeAttribution, FeeCharge, FeeFill, attribute_session_fees
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

FEE_EVIDENCE_KIND = "FEE_EVIDENCE_OBSERVED"
FEE_EVIDENCE_MAX_AGE_MS = 90_000


class FeeEvidenceFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at_ms: int = Field(ge=0)
    activities: list[BrokerActivity]
    history_complete: bool = False


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
    """Append changed evidence or a bounded freshness renewal under custody lock."""
    if repo.account_id.startswith(("sim:", "shadow:")):
        raise ValueError("simulated custody cannot record real broker fee activities")
    facts = FeeEvidenceFacts(
        checked_at_ms=checked_at_ms, activities=list(activities), history_complete=history_complete
    )
    with repo._write_lock:
        row = repo._conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE transition_kind = ? ORDER BY sequence DESC LIMIT 1",
            (FEE_EVIDENCE_KIND,),
        ).fetchone()
        if row is not None:
            prior = FeeEvidenceFacts.model_validate_json(row["facts_json"])
            # observed_at_ms changes on every HTTP delivery. Compare economics,
            # retaining periodic liveness evidence rather than identical ticks.
            current_rows = [activity.model_dump(exclude={"observed_at_ms"}) for activity in facts.activities]
            prior_rows = [activity.model_dump(exclude={"observed_at_ms"}) for activity in prior.activities]
            if (
                current_rows == prior_rows
                and prior.history_complete == history_complete
                and checked_at_ms - prior.checked_at_ms < 30_000
            ):
                return False
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
    return True


def _effective_fills(conn: sqlite3.Connection) -> tuple[dict[date, list[FeeFill]], set[str], bool]:
    account = conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0]
    records = effective_fill_records(conn, account_id=account)
    grouped: dict[date, list[FeeFill]] = defaultdict(list)
    complete = (
        conn.execute(
            "SELECT 1 FROM fills WHERE evidence_source = 'cumulative_recovery' AND NOT EXISTS (SELECT 1 FROM fills successor WHERE successor.superseded_execution_ref = fills.execution_id) LIMIT 1"
        ).fetchone()
        is None
    )
    orders: set[str] = set()
    for record in records:
        grouped[et_date_at_ms(record.filled_at_ms)].append(
            FeeFill(
                fill_id=record.event_key,
                subject_id=record.sid,
                side=record.side,
                quantity=Decimal(str(record.quantity)),
                price=Decimal(str(record.fill_price)),
                native_order_id=record.native_order_id,
                reported_fee=None if record.fee is None else Decimal(str(record.fee)),
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
    conn: sqlite3.Connection, *, now_ms: int, from_ms: int | None = None, to_ms: int | None = None
) -> FeeAttribution:
    """Lifetime fee projection in the caller's custody snapshot (no network).

    ``known`` gates new spending. Per-subject totals contain reported fill fees
    once; ``unobserved_cash_claim`` excludes those fees because the existing
    unseen-fill cash claim owns them. The observation cutoff is a consumer
    argument to that method and MUST come from the canonical cash observation.
    """
    account = conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0]
    simulated = account.startswith(("sim:", "shadow:"))
    grouped, owned_orders, population_complete = _effective_fills(conn)
    snapshots = (
        []
        if simulated
        else [
            FeeEvidenceFacts.model_validate_json(row[0])
            for row in conn.execute(
                "SELECT facts_json FROM custody_transitions WHERE transition_kind = ? ORDER BY sequence",
                (FEE_EVIDENCE_KIND,),
            )
        ]
    )
    by_date: dict[date, dict[str, BrokerActivity]] = defaultdict(dict)
    covered_days: set[date] = set()
    conflicting: set[date] = set()
    # Preserve oldest observation of each activity so repeated polling never
    # turns an already recognized fee back into an unrecognized cash claim.
    for snapshot in snapshots:
        for activity in snapshot.activities:
            if activity.occurred_at_ms is None:
                continue
            day = et_date_at_ms(activity.occurred_at_ms)
            prior = by_date[day].get(activity.activity_id)
            if prior is None:
                by_date[day][activity.activity_id] = activity
            elif prior.model_dump(exclude={"observed_at_ms"}) != activity.model_dump(exclude={"observed_at_ms"}):
                conflicting.add(day)
        oldest = min(
            (row.occurred_at_ms for row in snapshot.activities if row.occurred_at_ms is not None), default=None
        )
        if snapshot.history_complete:
            covered_days.update(set(grouped) | set(by_date))
        if oldest is not None:
            for day in set(grouped) | set(by_date):
                day_start = et_midnight_ms(day)
                if snapshot.history_complete or oldest < day_start:
                    covered_days.add(day)
    external_orders = {
        row[0]
        for row in conn.execute(
            "SELECT broker_order_id FROM external_orders WHERE filled_avg_price IS NOT NULL AND ABS(qty) >= 1e-9"
        )
    }
    witnessed_external: set[str] = set()
    for day, rows in by_date.items():
        for row in rows.values():
            if row.activity_type != "FILL" or row.native_order_id in owned_orders:
                continue
            if (
                row.native_order_id is None
                or row.quantity is None
                or row.price is None
                or row.side not in {"buy", "sell"}
            ):
                population_complete = False
                continue
            witnessed_external.add(row.native_order_id)
            grouped[day].append(
                FeeFill(
                    row.activity_id,
                    f"external:{row.native_order_id}",
                    OrderSide(row.side),
                    Decimal(str(row.quantity)),
                    Decimal(str(row.price)),
                    row.native_order_id,
                    observed_at_ms=row.observed_at_ms,
                )
            )
    population_complete = population_complete and external_orders <= witnessed_external
    shares = []
    unresolved = []
    unattributed = Decimal(0)
    observed = Decimal(0)
    predicted = Decimal(0)
    predicted_known = observed_known = True
    if not simulated and (not snapshots or now_ms - snapshots[-1].checked_at_ms > FEE_EVIDENCE_MAX_AGE_MS):
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
                None if row.net_amount is None else -Decimal(str(row.net_amount)),
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
            activities_complete=simulated or day in covered_days,
            simulated=simulated,
            session_ended=day < et_date_at_ms(now_ms),
            settlement_at_ms=et_midnight_ms(day + timedelta(days=1)),
        )
        shares.extend(result.shares)
        unresolved.extend(result.unresolved)
        unattributed += result.unattributed
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
    )
