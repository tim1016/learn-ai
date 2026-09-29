"""Replayable broker fee evidence and a synchronous custody-fenced projection.

The transition stream stores provider facts, not balances or allocated charges.
The one fee allocator derives money from effective corrected fill lineage on
read. Consumers use this function inside their existing SQLite transaction;
there is no network I/O, separate fee ledger or independent cash authority.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.money import MoneyInputError, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.custody_subjects import outside_order_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reads import external_orders as tracked_external_orders
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
    A read either starts at the newest activity (``page_token`` absent) or
    continues an unfinished walk from the provider cursor it names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at_ms: int = Field(ge=0)
    activities: list[BrokerActivity]
    # Oldest dated row anywhere in the read's newest-first window.
    oldest_occurred_at_ms: int | None
    history_complete: bool
    page_token: str | None
    next_page_token: str | None


@dataclass(frozen=True)
class _ProvenRun:
    """One contiguous stretch of newest-first history the recorded reads proved."""

    # Newest dated row retained from the run; ``None`` when it retained none.
    newest_ms: int | None
    # Oldest dated row the run read down to; ``None`` once it reached the
    # start of history.
    oldest_ms: int | None
    # Where the walk down from this run continues; ``None`` when it cannot.
    cursor: str | None

    def reaches(self, instant_ms: int) -> bool:
        return self.oldest_ms is None or self.oldest_ms < instant_ms

    def meets(self, lower: _ProvenRun) -> bool:
        """This run read down into ``lower``, so no unread row lies between them."""
        return self.oldest_ms is None or (lower.newest_ms is not None and self.oldest_ms <= lower.newest_ms)

    def joined(self, lower: _ProvenRun) -> _ProvenRun:
        """One run from this one and the ``lower`` run it meets; the deeper walk continues."""
        deeper = lower if lower.oldest_ms is None or (self.oldest_ms is not None and lower.oldest_ms <= self.oldest_ms) else self
        return _ProvenRun(_newest(self.newest_ms, lower.newest_ms), deeper.oldest_ms, deeper.cursor)

    def continued(self, read: FeeEvidenceFacts, newest_ms: int | None) -> _ProvenRun:
        """The run extended by a read that resumed exactly at its cursor."""
        newest = _newest(self.newest_ms, newest_ms)
        if read.history_complete or self.oldest_ms is None:
            return _ProvenRun(newest, None, None)
        reached = read.oldest_occurred_at_ms
        oldest = self.oldest_ms if reached is None else min(self.oldest_ms, reached)
        return _ProvenRun(newest, oldest, read.next_page_token)


def _newest(*instants: int | None) -> int | None:
    return max((ms for ms in instants if ms is not None), default=None)


@dataclass(frozen=True)
class _EvidenceWindow:
    """Which activity history the recorded reads proved, newest run first.

    Between two runs lies a gap: rows no read proved. It spans from the lower
    run's newest retained row to the upper run's oldest row, both included,
    since a boundary instant may hold rows neither read returned.
    """

    runs: tuple[_ProvenRun, ...]

    def reaches(self, day: date) -> bool:
        """The deepest run read past the start of ``day``."""
        return bool(self.runs) and self.runs[-1].reaches(et_midnight_ms(day))

    def covers(self, day: date) -> bool:
        return self.reaches(day) and not any(
            (first is None or first <= day) and day <= last for first, last in self._gaps()
        )

    def unproven_days(self, since: date) -> set[date]:
        """Every ET day from ``since`` on that a gap touches."""
        days: set[date] = set()
        for first, last in self._gaps():
            day = since if first is None else max(first, since)
            while day <= last:
                days.add(day)
                day += timedelta(days=1)
        return days

    @property
    def gap_cursor(self) -> str | None:
        """Where the walk that closes the newest walkable gap continues."""
        return next((run.cursor for run in self.runs[:-1] if run.cursor is not None), None)

    @property
    def history_cursor(self) -> str | None:
        """Where the walk below every proven run continues."""
        return self.runs[-1].cursor if self.runs else None

    def _gaps(self) -> Iterator[tuple[date | None, date]]:
        for upper, lower in pairwise(self.runs):
            # Only a run that reached the start of history has no oldest row,
            # and that run meets every run below it.
            if upper.oldest_ms is not None:
                first = None if lower.newest_ms is None else et_date_at_ms(lower.newest_ms)
                yield first, et_date_at_ms(upper.oldest_ms)


@dataclass(frozen=True)
class _HistoryFloor:
    """The oldest ET day of broker activity custody attributes.

    Custody began at the first transition of its hash chain; activity on an
    earlier day is already inside the cash it started from. The floor reaches
    further back only for fills custody recorded. ``day`` is ``None`` only
    before any history.

    An external order custody tracks may have executed before the floor (a
    GTC working across genesis). Those executions never move the floor: the
    walk continues until they explain the order's filled quantity, and they
    reach only the external cash claim, never a fee day.

    ``began_at_ms`` is the genesis instant itself. The floor is a day because
    fees post per session; which executions are inside the cash custody
    started from is decided by the instant, so an outside sale earlier on
    genesis day is as old as one the day before (H35).
    """

    day: date | None
    began_at_ms: int | None
    # Broker order ids of the external orders custody tracks.
    tracked: frozenset[str]
    # Every tracked external order's filled quantity is witnessed. Until then
    # its older executions may lie beyond the window, so the walk continues.
    explained: bool

    def admits(self, day: date) -> bool:
        return self.day is None or day >= self.day

    def reached_by(self, window: _EvidenceWindow) -> bool:
        return self.explained and (self.day is None or window.reaches(self.day))


def _history_floor(
    conn: sqlite3.Connection, *, fill_days: Iterable[date], activities: Iterable[BrokerActivity]
) -> _HistoryFloor:
    """Custody-owned bound for both the history walk and fee attribution.

    Replayed hash-chain facts only: ``control_meta.created_at_ms`` is
    re-stamped when the database is rebuilt from its mirror. No provider row
    moves the floor; a tracked order's executions only keep the walk going.
    """
    genesis = conn.execute("SELECT recorded_at_ms FROM custody_transitions ORDER BY sequence LIMIT 1").fetchone()
    days = {*fill_days, *([] if genesis is None else [et_date_at_ms(genesis[0])])}
    required = {order.broker_order_id: _normalized_or_none(order.filled_quantity) for order in tracked_external_orders(conn)}
    witnessed: dict[str, Decimal] = defaultdict(Decimal)
    for row in activities:
        if row.activity_type in {"FILL", "PARTIAL_FILL"} and row.native_order_id in required and row.occurred_at_ms is not None:
            witnessed[row.native_order_id] += _normalized_or_none(row.quantity) or 0
    explained = all(quantity is not None and witnessed[order] >= quantity for order, quantity in required.items())
    return _HistoryFloor(
        min(days, default=None), None if genesis is None else int(genesis[0]), frozenset(required), explained
    )


def _evidence_window(snapshots: Sequence[FeeEvidenceFacts]) -> _EvidenceWindow:
    """History proven by newest-first reads and the walks linked to them.

    A head read starts at the newest activity, so it opens a run above every
    other. It joins the run below only when its oldest row is no newer than
    that run's newest retained row; otherwise the rows between them are
    unread -- more arrived between two polls than one read returns -- and
    that gap stays unproven until a walk closes it (#2557). A continuation
    counts only when it resumed exactly at a run's retained cursor, so every
    walk stays contiguous with its run. Proven history never shrinks: runs
    only extend and join.
    """
    runs: list[_ProvenRun] = []
    for snapshot in snapshots:
        # Only rows no earlier read retained are recorded, so the newest of a
        # read's own rows is known exactly when it opens a run of its own.
        newest = _newest(*(row.occurred_at_ms for row in snapshot.activities))
        if snapshot.page_token is None:
            if snapshot.history_complete:
                runs.insert(0, _ProvenRun(newest, None, None))
            elif snapshot.oldest_occurred_at_ms is not None:
                runs.insert(0, _ProvenRun(newest, snapshot.oldest_occurred_at_ms, snapshot.next_page_token))
            else:
                continue
        else:
            index = next((index for index, run in enumerate(runs) if run.cursor == snapshot.page_token), None)
            if index is None:
                continue
            runs[index] = runs[index].continued(snapshot, newest)
        joined: list[_ProvenRun] = []
        for run in runs:
            if joined and joined[-1].meets(run):
                joined[-1] = joined[-1].joined(run)
            else:
                joined.append(run)
        runs = joined
    return _EvidenceWindow(tuple(runs))


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


@money_context()
def fee_evidence_cursor(repo: ClerkSqliteRepository) -> str | None:
    """The provider cursor the next walk read resumes from, or ``None`` when none is due.

    A gap between two newest-first reads is walked first, from the newer
    read's cursor, until it meets the history retained below it. Below every
    run, no read is due once the walk proved custody's history floor and
    explained every tracked external order. Cursors are kept, so a floor
    that later moves back, or a newly tracked order, resumes the same walk.
    """
    with repo._write_lock:
        snapshots = _recorded_evidence(repo._conn)
        window = _evidence_window(snapshots)
        if window.gap_cursor is not None:
            return window.gap_cursor
        if window.history_cursor is None:
            return None
        floor = _history_floor(
            repo._conn,
            fill_days=_effective_fills(repo._conn)[0],
            activities=collapse_activity_deliveries(
                activity for snapshot in snapshots for activity in snapshot.activities
            ).unique.values(),
        )
        return None if floor.reached_by(window) else window.history_cursor


def record_fee_evidence(
    repo: ClerkSqliteRepository,
    activities: Sequence[BrokerActivity],
    *,
    checked_at_ms: int,
    history_complete: bool = False,
    page_token: str | None = None,
    next_page_token: str | None = None,
) -> bool:
    """Record what one successful read adds; a head read stamps freshness.

    Only rows no earlier record retained are appended, with the window facts
    coverage needs. A read that adds neither rows nor reach appends nothing:
    like the account observation's cash freshness, liveness is this process's
    latest head-read time, not a custody fact. Returns whether custody grew.
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
            page_token=page_token,
            next_page_token=next_page_token,
        )
        grows = not recorded or bool(new_rows) or _evidence_window([*recorded, facts]) != _evidence_window(recorded)
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
        if page_token is None:
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
    window = _evidence_window(snapshots)
    by_date: dict[date, dict[str, BrokerActivity]] = defaultdict(dict)
    # A tracked external order's executions dated before the floor: inside
    # the cash custody started from, so never priced and never a fee day, but
    # its external cash claim needs the order's whole filled quantity.
    before_floor: dict[str, BrokerActivity] = {}
    undated: dict[str, BrokerActivity] = {}
    # Recorded order keeps each activity's first observation, so repeated
    # polling never turns a recognized fee back into an unrecognized claim.
    collapsed = collapse_activity_deliveries(
        activity for snapshot in snapshots for activity in snapshot.activities
    )
    floor = _history_floor(conn, fill_days=list(grouped), activities=collapsed.unique.values())
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
        day = et_date_at_ms(activity.occurred_at_ms)
        if floor.admits(day):
            by_date[day][activity.activity_id] = activity
        elif activity.activity_type in {"FILL", "PARTIAL_FILL"} and activity.native_order_id in floor.tracked:
            before_floor[activity.activity_id] = activity
    external_orders = {
        row[0]
        for row in conn.execute(
            "SELECT broker_order_id FROM external_orders WHERE filled_avg_price IS NOT NULL AND ABS(qty) >= 1e-9"
        )
    }
    witnessed_external: set[str] = set()
    external_fills: list[FeeFill] = []
    pre_custody_quantities: dict[str, Decimal] = defaultdict(Decimal)
    execution_ids = {row[0] for row in conn.execute("SELECT execution_id FROM fills WHERE execution_id IS NOT NULL")}
    from app.broker.alpaca.clerk.sqlite.historical_execution_recovery import _execution_id_from_activity_id

    # ``None`` marks the pre-floor executions: they open no fee day.
    for day, rows in [*by_date.items(), (None, before_floor)]:
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
                subject_id=outside_order_subject_id(row.native_order_id),
                side=OrderSide(row.side),
                quantity=normalize_money(row.quantity),
                price=normalize_money(row.price),
                native_order_id=row.native_order_id,
                observed_at_ms=row.observed_at_ms,
                occurred_at_ms=row.occurred_at_ms,
            )
            if day is not None:
                grouped[day].append(external)
            # Before custody began, the execution is inside the cash custody
            # started from (H35): it proves its order's filled quantity and
            # nothing else -- never a lot, a price, or a sale still settling.
            if floor.began_at_ms is not None and row.occurred_at_ms < floor.began_at_ms:
                pre_custody_quantities[row.native_order_id] += external.quantity
            else:
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
        unresolved.append("Account fee evidence is missing or stale.")
    # A day inside a gap between reads refuses even with no row custody
    # knows of: the unread rows may hold any fee or outside fill (#2557).
    unproven = set() if floor.day is None else window.unproven_days(floor.day)
    for day in sorted(
        set(grouped)
        | {day for day, rows in by_date.items() if any(row.activity_type == "FEE" for row in rows.values())}
        | unproven
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
            activities_complete=simulated or window.covers(day),
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
        pre_custody_quantities=dict(pre_custody_quantities),
        unattributed_charges=tuple(unattributed_charges),
    )
