"""Replayable broker fee evidence and a synchronous custody-fenced projection.

The transition stream stores provider facts, not balances or allocated charges.
The one fee allocator derives money from effective corrected fill lineage on
read. Consumers use this function inside their existing SQLite transaction;
there is no network I/O, separate fee ledger or independent cash authority.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.adapter import execution_id_from_activity_id
from app.broker.alpaca.clerk.money import MoneyInputError, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.custody_subjects import outside_order_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reads import external_orders as tracked_external_orders
from app.broker.alpaca.clerk.sqlite.reads import filled_outside_order_ids, governing_acknowledgement
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PARAMS,
    EXECUTION_COVERAGE_INCOMPLETE_REASON_CODE_SQL_PLACEHOLDERS,
)
from app.broker.contract.models import BrokerActivity, OrderSide
from app.services.alpaca_fee_attribution import (
    CollapsedDeliveries,
    FeeAttribution,
    FeeCharge,
    FeeFill,
    UnattributedCharge,
    activity_economics,
    attribute_session_fees,
    collapse_activity_deliveries,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms, et_midnight_ms

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

    checked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    activities: list[BrokerActivity]
    # Oldest dated row anywhere in the read's newest-first window. Coverage
    # never reads it as reach: a date-only row's midnight stamp may lie long
    # before where the row posted (#2557).
    oldest_occurred_at_ms: int | None
    history_complete: bool
    page_token: str | None
    next_page_token: str | None
    # Oldest trade-timestamped row anywhere in the read (#2557). Omitted when
    # absent, so earlier records keep their exact bytes; a record without it
    # falls back to its own trade rows, which can only place it later.
    oldest_trade_at_ms: int | None = None


def _trade_instants(rows: Iterable[BrokerActivity]) -> list[int]:
    """The instants that place rows in the provider's newest-first order.

    Only a trade timestamp does. A date-only row is stamped at ET midnight of
    its date, and it may post long after that date (day D's FEE posts after D
    ends), so its stamp cannot prove where a read stood in the order.
    """
    return [row.occurred_at_ms for row in rows if row.category == "trade_activity" and row.occurred_at_ms is not None]


def _earliest(*instants: int | None) -> int | None:
    return min((ms for ms in instants if ms is not None), default=None)


def _reach(read: FeeEvidenceFacts, trades: Sequence[int]) -> float:
    """How far down one read proved it reached, as a trade instant.

    ``-inf`` once it read to the start of history; ``+inf`` when it returned
    no trade row, since a date-only row proves nothing about reach. A record
    without its oldest trade row (every one written before #2557) falls back
    to its own new trade rows, which can only place it higher.
    """
    if read.history_complete:
        return -math.inf
    oldest = _earliest(read.oldest_trade_at_ms, *trades)
    return math.inf if oldest is None else oldest


@dataclass(frozen=True)
class _ProvenRun:
    """One contiguous stretch of newest-first history the recorded reads proved.

    A run is one head read and the walk reads chained to it by cursor. Only
    trade instants place rows in the provider's order (``_trade_instants``),
    so the stretch runs from ``top`` down to ``bottom``, both trade instants.
    """

    # The newest trade row retained when the head read started at the top of
    # the order, or read by its walk since. Every such row existed then, so
    # the read started at or above it. ``-inf`` before any trade row.
    top: float
    # The oldest trade row any of its reads returned: ``-inf`` once one read
    # to the start of history, ``+inf`` while none returned a trade row.
    bottom: float
    # Where the walk down from this run continues; ``None`` when it cannot.
    cursor: str | None

    def reaches(self, instant_ms: int) -> bool:
        return self.bottom < instant_ms

    def joined(self, other: _ProvenRun) -> _ProvenRun:
        """One run from this one and an ``other`` it overlaps.

        The deeper walk continues. When it cannot, the shallower cursor
        re-reads the overlap and then extends below it.
        """
        deeper, shallower = sorted((self, other), key=lambda run: run.bottom)
        cursor = None if deeper.bottom == -math.inf else deeper.cursor or shallower.cursor
        return _ProvenRun(max(self.top, other.top), deeper.bottom, cursor)

    def continued(self, read: FeeEvidenceFacts, trades: Sequence[int]) -> _ProvenRun:
        """The run extended by a walk read that resumed exactly at its cursor."""
        cursor = None if read.history_complete else read.next_page_token
        return _ProvenRun(max([self.top, *trades]), min(self.bottom, _reach(read, trades)), cursor)


def _day(instant: float, otherwise: date) -> date:
    """The ET day of a trade instant; ``otherwise`` for an open end."""
    return otherwise if math.isinf(instant) else et_date_at_ms(int(instant))


def _days(first: date, last: date) -> set[date]:
    """Every day from ``first`` through ``last``; none when ``last`` is earlier."""
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


@dataclass(frozen=True)
class _EvidenceWindow:
    """Which activity history the recorded reads proved, newest run first.

    Between two runs lies a gap: rows no read proved. The newest run's head
    read started at the top of the order, so nothing above it is a gap.
    """

    runs: tuple[_ProvenRun, ...]

    def reaches(self, day: date) -> bool:
        """The deepest run read past the start of ``day``."""
        return bool(self.runs) and self.runs[-1].reaches(et_midnight_ms(day))

    def unproven_days(self, since: date, through: date) -> set[date]:
        """The ET days from ``since`` through ``through`` whose rows no read proved.

        Those are the days the deepest run has not read past, and every day a
        gap touches, from the lower run's top to the upper run's bottom, both
        included, since a boundary instant may hold rows neither read returned.
        """
        if not self.runs:
            return _days(since, through)
        deepest = self.runs[-1].bottom
        spans = [] if deepest == -math.inf else [(since, _day(deepest, through))]
        spans.extend((_day(lower.top, since), _day(upper.bottom, through)) for upper, lower in pairwise(self.runs))
        return set().union(*(_days(max(first, since), min(last, through)) for first, last in spans))

    @property
    def gap_cursor(self) -> str | None:
        """Where the walk that closes the newest walkable gap continues."""
        return next((run.cursor for run in self.runs[:-1] if run.cursor is not None), None)

    @property
    def history_cursor(self) -> str | None:
        """Where the walk below every proven run continues."""
        return self.runs[-1].cursor if self.runs else None


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

    A head read starts at the top of the order, so it opens a run above every
    other; a head read that added no row re-read only retained history and
    extends the newest run instead. A walk read counts only when it resumed
    exactly at a run's retained cursor, so every walk stays contiguous with
    its run. Runs whose stretches overlap join (#2557). Tops never rise down
    the list -- a head read's top is the newest trade row retained by then,
    and a walk reads only below its head read -- so comparing neighbours
    joins every overlap. A run that proved no reach proves nothing once a
    newer head read starts from the top again, and is dropped. Proven history
    never shrinks: runs only extend and join.
    """
    runs: list[_ProvenRun] = []
    newest_trade: float = -math.inf
    for snapshot in snapshots:
        trades = _trade_instants(snapshot.activities)
        newest_trade = max([newest_trade, *trades])
        if snapshot.page_token is not None:
            index = next((index for index, run in enumerate(runs) if run.cursor == snapshot.page_token), None)
            if index is None:
                continue
            runs[index] = runs[index].continued(snapshot, trades)
        elif snapshot.history_complete or snapshot.oldest_occurred_at_ms is not None:
            read = _ProvenRun(
                newest_trade,
                _reach(snapshot, trades),
                None if snapshot.history_complete else snapshot.next_page_token,
            )
            if runs and not snapshot.activities:
                runs[0] = runs[0].joined(read)
            else:
                runs.insert(0, read)
        else:
            continue
        joined: list[_ProvenRun] = []
        for run in runs:
            if joined and run.bottom == math.inf:
                continue
            if joined and joined[-1].bottom <= run.top:
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


def retained_activities(conn: sqlite3.Connection) -> CollapsedDeliveries[BrokerActivity]:
    """Every activity row the recorded evidence retained, each identity once, with any disagreeing copies."""
    return collapse_activity_deliveries(
        activity for snapshot in _recorded_evidence(conn) for activity in snapshot.activities
    )


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
            oldest_trade_at_ms=_earliest(*_trade_instants(activities)),
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
                    facts_json=canonicalize(
                        facts.model_dump(
                            mode="json", exclude={"oldest_trade_at_ms"} if facts.oldest_trade_at_ms is None else None
                        )
                    ),
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
    require_fresh_evidence: bool = True,
) -> FeeAttribution:
    """Lifetime fee projection in the caller's custody snapshot (no network).

    Real accounts are fresh only while the producer's latest successful read
    (``evidence_checked_at_ms``, process-local, see ``fee_attribution`` on the
    repository) is within ``FEE_EVIDENCE_MAX_AGE_MS`` of ``now_ms``; missing
    freshness refuses like missing evidence. Freshness guards new spending:
    ``require_fresh_evidence=False`` values the evidence already recorded, for
    a read that spends nothing -- what a Stop releases (#2555, owner decision
    2026-09-29) -- and every other refusal, missing evidence included, stands.

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
    Reference: ADR 0059 fee attribution amendment; PRD #2540.
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
    # A manual chain's member first seen as foreign is the leg's: its
    # executions are credited to the leg, never awaited as an outside
    # order's (#2787).
    external_orders = filled_outside_order_ids(conn)
    witnessed_external: set[str] = set()
    external_fills: list[FeeFill] = []
    pre_custody_quantities: dict[str, Decimal] = defaultdict(Decimal)
    execution_ids = {row[0] for row in conn.execute("SELECT execution_id FROM fills WHERE execution_id IS NOT NULL")}
    # ``None`` marks the pre-floor executions: they open no fee day.
    for day, rows in [*by_date.items(), (None, before_floor)]:
        for row in rows.values():
            if row.activity_type not in {"FILL", "PARTIAL_FILL"}:
                continue
            if row.native_order_id in owned_orders:
                if execution_id_from_activity_id(row.activity_id) not in execution_ids:
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
        or (require_fresh_evidence and (
            evidence_checked_at_ms is None
            or not 0 <= now_ms - evidence_checked_at_ms <= FEE_EVIDENCE_MAX_AGE_MS
        ))
    ):
        unresolved.append("Account fee evidence is missing or stale.")
    # A day no read proved refuses even with no row custody knows of: its
    # unread rows may hold any fee or outside fill (#2557).
    unproven = (
        set() if simulated or floor.day is None else window.unproven_days(floor.day, et_date_at_ms(now_ms))
    )
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
            activities_complete=day not in unproven,
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
