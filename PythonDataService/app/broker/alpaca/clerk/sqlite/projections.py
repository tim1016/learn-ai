"""Bounded materialized-fold projections for SQLite Clerk surfaces.

No read in this module replays ``custody_transitions``.  Current state comes
from the fold tables committed with each transition.  The timeline is the sole
log read and uses a keyset-bounded immutable sequence window.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.order_projection import (
    OrderProjectionReadError,
    read_current_orders,
    read_orders_by_operation,
)
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    OperationPage,
    ProjectedCommand,
    ProjectedHold,
    ProjectedOperation,
    ProjectedOrder,
    ProjectedPosition,
    ProjectedReceipt,
    ProjectedReconciliation,
    ProjectedRun,
    ProjectedUncertainty,
    TimelineEntry,
    TimelinePage,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_projection_guidance,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.utils.timestamps import Clock, now_ms_utc

DEFAULT_OPERATION_LIMIT = 50
MAX_OPERATION_LIMIT = 100
DEFAULT_RECEIPT_LIMIT = 20
CURRENT_STATE_LIMIT = 100
DEFAULT_TIMELINE_PAGE_SIZE = 25
MAX_TIMELINE_PAGE_SIZE = 100


class ProjectionReadError(Exception):
    """The verified authority could not produce a coherent read snapshot."""


class ProjectionIdentityMismatch(ProjectionReadError):
    """The read connection does not point at the repository identity supplied."""


class InvalidTimelineCursor(ProjectionReadError):
    """A timeline cursor is malformed or belongs to another projection."""


class SqliteClerkProjectionReader:
    """One reusable read-only connection over an already-verified authority."""

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._authority_generation = authority_generation
        self._db_identity_token = db_identity_token
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only = ON")
        with self._read_transaction():
            self._verify_identity()

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        """One coherent snapshot for a multi-query projection read (#1396 P2).

        The ``threading.Lock`` alone only serializes this reader against
        itself; it does not stop the live repository connection from
        committing a fold between two of this reader's own ``SELECT``s, which
        could combine an old ``control_revision`` with newer rows. An
        explicit ``BEGIN`` closes that gap: in WAL mode every read inside one
        transaction sees the snapshot as of ``BEGIN``, regardless of writer
        commits landing concurrently.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @classmethod
    def from_repository(
        cls,
        repository: ClerkSqliteRepository,
        *,
        clock: Clock = now_ms_utc,
    ) -> SqliteClerkProjectionReader:
        meta = repository.control_meta_snapshot()
        return cls(
            db_path=repository.db_path,
            account_id=meta.account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            clock=clock,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def account_snapshot(
        self,
        *,
        operation_limit: int = DEFAULT_OPERATION_LIMIT,
    ) -> ClerkProjection:
        return self._snapshot(
            strategy_instance_id=None,
            operation_limit=operation_limit,
        )

    def bot_snapshot(
        self,
        strategy_instance_id: str,
        *,
        operation_limit: int = DEFAULT_OPERATION_LIMIT,
    ) -> ClerkProjection | None:
        operation_limit = _bounded_limit(operation_limit)
        with self._read_transaction():
            self._verify_identity()
            exists = self._conn.execute(
                "SELECT 1 FROM strategy_instances WHERE strategy_instance_id = ?",
                (strategy_instance_id,),
            ).fetchone()
        if exists is None:
            return None
        return self._snapshot(
            strategy_instance_id=strategy_instance_id,
            operation_limit=operation_limit,
        )

    def _snapshot(
        self,
        *,
        strategy_instance_id: str | None,
        operation_limit: int,
    ) -> ClerkProjection:
        operation_limit = _bounded_limit(operation_limit)
        now_ms = self._clock()
        with self._read_transaction():
            self._verify_identity()
            meta = self._meta()
            context = self._policy_context(
                strategy_instance_id=strategy_instance_id,
                control_revision=meta["control_revision"],
                now_ms=now_ms,
            )
            operations = self._operations(strategy_instance_id, operation_limit)
            commands = self._commands(strategy_instance_id, operation_limit)
            holds = self._holds(strategy_instance_id)
            reconciliation = self._latest_reconciliation(strategy_instance_id, now_ms)
            receipts = self._terminal_receipts(strategy_instance_id)
        return ClerkProjection(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=self._authority_generation,
            db_identity_token=self._db_identity_token,
            authority_health="healthy",
            authority_health_reason=None,
            control_revision=meta["control_revision"],
            custody_owner="ACCOUNT_CLERK",
            runs=context.runs,
            commands=commands,
            operations=operations,
            positions=context.positions,
            holds=holds,
            uncertainties=context.uncertainties[:CURRENT_STATE_LIMIT],
            latest_reconciliation=reconciliation,
            terminal_receipts=receipts,
            guidance=build_projection_guidance(context),
            recovery_actions=build_recovery_catalog(context),
            generated_at_ms=now_ms,
        )

    def recovery_context(
        self,
        *,
        strategy_instance_id: str | None,
    ) -> RecoveryPolicyContext | None:
        now_ms = self._clock()
        with self._read_transaction():
            self._verify_identity()
            if strategy_instance_id is not None:
                exists = self._conn.execute(
                    "SELECT 1 FROM strategy_instances WHERE strategy_instance_id = ?",
                    (strategy_instance_id,),
                ).fetchone()
                if exists is None:
                    return None
            meta = self._meta()
            return self._policy_context(
                strategy_instance_id=strategy_instance_id,
                control_revision=meta["control_revision"],
                now_ms=now_ms,
            )

    def _policy_context(
        self,
        *,
        strategy_instance_id: str | None,
        control_revision: int,
        now_ms: int,
    ) -> RecoveryPolicyContext:
        return RecoveryPolicyContext(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=self._authority_generation,
            db_identity_token=self._db_identity_token,
            authority_health="healthy",
            authority_health_reason=None,
            control_revision=control_revision,
            now_ms=now_ms,
            runs=self._runs(strategy_instance_id),
            current_orders=self._current_orders(strategy_instance_id),
            positions=self._positions(strategy_instance_id),
            uncertainties=self._uncertainties(strategy_instance_id, now_ms),
            latest_account_reconciliation=self._latest_reconciliation(
                None,
                now_ms,
                account_wide_only=True,
            ),
        )

    def timeline_page(
        self,
        *,
        strategy_instance_id: str | None = None,
        order_ref: str | None = None,
        effect_operation_id: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_TIMELINE_PAGE_SIZE,
    ) -> TimelinePage:
        page_size = min(max(page_size, 1), MAX_TIMELINE_PAGE_SIZE)
        with self._read_transaction():
            self._verify_identity()
            meta = self._meta()
            if cursor is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
                ).fetchone()
                anchor_sequence = int(row["sequence"])
                before_sequence = anchor_sequence + 1
            else:
                anchor_sequence, before_sequence = self._decode_cursor(
                    cursor,
                    strategy_instance_id=strategy_instance_id,
                    order_ref=order_ref,
                    effect_operation_id=effect_operation_id,
                )
            where = "sequence <= ? AND sequence < ?"
            parameters: list[object] = [anchor_sequence, before_sequence]
            if strategy_instance_id is not None:
                where += " AND strategy_instance_id = ?"
                parameters.append(strategy_instance_id)
            if order_ref is not None:
                where += " AND order_ref = ?"
                parameters.append(order_ref)
            if effect_operation_id is not None:
                where += " AND effect_operation_id = ?"
                parameters.append(effect_operation_id)
            rows = self._conn.execute(
                "SELECT sequence, effect_operation_id, command_id, order_ref, broker_order_id, "
                "transition_kind, operation_state, broker_state, custody_owner, "
                "execution_authority, summary_code, proof_reference, source_event_at_ms, "
                "clerk_observed_at_ms, recorded_at_ms FROM custody_transitions "
                f"WHERE {where} ORDER BY sequence DESC LIMIT ?",
                (*parameters, page_size + 1),
            ).fetchall()
            total_where = "sequence <= ?"
            total_parameters: list[object] = [anchor_sequence]
            if strategy_instance_id is not None:
                total_where += " AND strategy_instance_id = ?"
                total_parameters.append(strategy_instance_id)
            if order_ref is not None:
                total_where += " AND order_ref = ?"
                total_parameters.append(order_ref)
            if effect_operation_id is not None:
                total_where += " AND effect_operation_id = ?"
                total_parameters.append(effect_operation_id)
            total = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM custody_transitions WHERE {total_where}",
                total_parameters,
            ).fetchone()
        has_more = len(rows) > page_size
        visible_rows = rows[:page_size]
        entries = tuple(_timeline_entry(row) for row in visible_rows)
        next_cursor = None
        if has_more and entries:
            next_cursor = self._encode_cursor(
                anchor_sequence=anchor_sequence,
                before_sequence=entries[-1].sequence,
                strategy_instance_id=strategy_instance_id,
                order_ref=order_ref,
                effect_operation_id=effect_operation_id,
            )
        return TimelinePage(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=self._authority_generation,
            control_revision=meta["control_revision"],
            anchor_sequence=anchor_sequence,
            total_entries=int(total["count"]),
            entries=entries,
            next_cursor=next_cursor,
        )

    def operation_page(
        self,
        *,
        strategy_instance_id: str | None = None,
        lifecycle_state: str | None = None,
        run_id: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_TIMELINE_PAGE_SIZE,
    ) -> OperationPage:
        """Read one keyset page from the materialized operation fold."""
        page_size = min(max(page_size, 1), MAX_TIMELINE_PAGE_SIZE)
        with self._read_transaction():
            self._verify_identity()
            meta = self._meta()
            high_water = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
            ).fetchone()
            if cursor is None:
                head = self._operation_head(
                    strategy_instance_id=strategy_instance_id,
                    lifecycle_state=lifecycle_state,
                    run_id=run_id,
                )
                anchor = (
                    None
                    if head is None
                    else (int(head["created_at_ms"]), str(head["effect_operation_id"]))
                )
                before = None
            else:
                anchor, before = self._decode_operation_cursor(
                    cursor,
                    strategy_instance_id=strategy_instance_id,
                    lifecycle_state=lifecycle_state,
                    run_id=run_id,
                )
            rows = self._operation_rows(
                strategy_instance_id=strategy_instance_id,
                lifecycle_state=lifecycle_state,
                run_id=run_id,
                anchor=anchor,
                before=before,
                limit=page_size + 1,
            )
            operations = self._project_operations(rows[:page_size])
        has_more = len(rows) > page_size
        next_cursor = None
        if has_more and operations and anchor is not None:
            last = operations[-1]
            next_cursor = self._encode_operation_cursor(
                strategy_instance_id=strategy_instance_id,
                lifecycle_state=lifecycle_state,
                run_id=run_id,
                anchor=anchor,
                before=(last.created_at_ms, last.effect_operation_id),
            )
        return OperationPage(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=self._authority_generation,
            control_revision=meta["control_revision"],
            high_water_sequence=int(high_water["sequence"]),
            operations=operations,
            next_cursor=next_cursor,
        )

    def operation_timeline(self, effect_operation_id: str) -> tuple[TimelineEntry, ...]:
        """Return bounded immutable events for one exact Clerk operation."""
        with self._read_transaction():
            self._verify_identity()
            rows = self._conn.execute(
                "SELECT sequence, effect_operation_id, command_id, order_ref, broker_order_id, "
                "transition_kind, operation_state, broker_state, custody_owner, "
                "execution_authority, summary_code, proof_reference, source_event_at_ms, "
                "clerk_observed_at_ms, recorded_at_ms FROM custody_transitions "
                "WHERE effect_operation_id = ? ORDER BY sequence ASC LIMIT ?",
                (effect_operation_id, MAX_TIMELINE_PAGE_SIZE),
            ).fetchall()
        return tuple(_timeline_entry(row) for row in rows)

    def operation(self, effect_operation_id: str) -> ProjectedOperation | None:
        """Read one exact materialized operation and its nested orders."""
        with self._read_transaction():
            self._verify_identity()
            rows = self._operation_rows(
                strategy_instance_id=None,
                lifecycle_state=None,
                run_id=None,
                anchor=None,
                before=None,
                limit=1,
                effect_operation_id=effect_operation_id,
            )
            operations = self._project_operations(rows)
        return operations[0] if operations else None

    def _verify_identity(self) -> None:
        row = self._conn.execute(
            "SELECT account_id, authority_generation, db_identity_token "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
        actual = (
            None
            if row is None
            else (row["account_id"], row["authority_generation"], row["db_identity_token"])
        )
        expected = (
            self._account_id,
            self._authority_generation,
            self._db_identity_token,
        )
        if actual != expected:
            raise ProjectionIdentityMismatch(
                "SQLite projection identity no longer matches the verified authority"
            )

    def _meta(self) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT control_revision FROM control_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ProjectionReadError("SQLite authority has no control_meta row")
        return row

    def _runs(self, strategy_instance_id: str | None) -> tuple[ProjectedRun, ...]:
        where, params = _scope_filter("strategy_instance_id", strategy_instance_id)
        rows = self._conn.execute(
            "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
            f"stopped_at_ms FROM runs {where} ORDER BY started_at_ms DESC LIMIT ?",
            (*params, CURRENT_STATE_LIMIT),
        ).fetchall()
        return tuple(ProjectedRun(**dict(row)) for row in rows)

    def _operations(
        self,
        strategy_instance_id: str | None,
        limit: int,
    ) -> tuple[ProjectedOperation, ...]:
        rows = self._operation_rows(
            strategy_instance_id=strategy_instance_id,
            lifecycle_state=None,
            run_id=None,
            anchor=None,
            before=None,
            limit=limit,
        )
        return self._project_operations(rows)

    def _project_operations(
        self,
        rows: list[sqlite3.Row],
    ) -> tuple[ProjectedOperation, ...]:
        operation_ids = tuple(row["effect_operation_id"] for row in rows)
        orders_by_operation = self._orders_by_operation(operation_ids)
        return tuple(
            ProjectedOperation(
                effect_operation_id=row["effect_operation_id"],
                kind=row["kind"],
                state=row["state"],
                custody_owner=row["custody_owner"],
                strategy_instance_id=row["strategy_instance_id"],
                run_id=row["run_id"],
                created_at_ms=row["created_at_ms"],
                updated_at_ms=row["updated_at_ms"],
                latest_transition_sequence=row["latest_transition_sequence"],
                transition_count=row["transition_count"],
                terminal_receipt_id=row["terminal_receipt_id"],
                command=ProjectedCommand(
                    command_id=row["command_id"],
                    kind=row["command_kind"],
                    action=row["action"],
                    state=row["command_state"],
                    run_id=row["command_run_id"],
                    receipt_id=row["receipt_id"],
                    created_at_ms=row["command_created_at_ms"],
                    updated_at_ms=row["command_updated_at_ms"],
                ),
                orders=orders_by_operation.get(row["effect_operation_id"], ()),
            )
            for row in rows
        )

    def _operation_head(
        self,
        *,
        strategy_instance_id: str | None,
        lifecycle_state: str | None,
        run_id: str | None,
    ) -> sqlite3.Row | None:
        rows = self._operation_rows(
            strategy_instance_id=strategy_instance_id,
            lifecycle_state=lifecycle_state,
            run_id=run_id,
            anchor=None,
            before=None,
            limit=1,
        )
        return rows[0] if rows else None

    def _operation_rows(
        self,
        *,
        strategy_instance_id: str | None,
        lifecycle_state: str | None,
        run_id: str | None,
        anchor: tuple[int, str] | None,
        before: tuple[int, str] | None,
        limit: int,
        effect_operation_id: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("e.strategy_instance_id", strategy_instance_id),
            ("e.state", lifecycle_state),
            ("e.run_id", run_id),
            ("e.effect_operation_id", effect_operation_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        # Anchored/paged by `created_at_ms` (#1396 P2), not `updated_at_ms`: the
        # latter is mutated by every fold (SUBMIT_ACKED, fill, reconciliation),
        # so an operation below the first page could jump above the anchor
        # mid-traversal and silently vanish from every remaining page. Each
        # `effect_operation_id` row's `created_at_ms` is written once at
        # ENTER/EXIT acceptance and never updated — an immutable ordering key.
        if anchor is not None:
            clauses.append(
                "(e.created_at_ms < ? OR "
                "(e.created_at_ms = ? AND e.effect_operation_id <= ?))"
            )
            parameters.extend((anchor[0], anchor[0], anchor[1]))
        if before is not None:
            clauses.append(
                "(e.created_at_ms < ? OR "
                "(e.created_at_ms = ? AND e.effect_operation_id < ?))"
            )
            parameters.extend((before[0], before[0], before[1]))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._conn.execute(
            "SELECT e.effect_operation_id, e.kind, e.state, e.custody_owner, "
            "e.strategy_instance_id, e.run_id, e.created_at_ms, e.updated_at_ms, "
            "e.terminal_receipt_id, c.command_id, c.kind AS command_kind, "
            "c.action, c.state AS command_state, c.run_id AS command_run_id, "
            "c.receipt_id, c.created_at_ms AS command_created_at_ms, "
            "c.updated_at_ms AS command_updated_at_ms, "
            "(SELECT MAX(t.sequence) FROM custody_transitions t "
            "WHERE t.effect_operation_id = e.effect_operation_id) "
            "AS latest_transition_sequence, "
            "(SELECT COUNT(*) FROM custody_transitions t "
            "WHERE t.effect_operation_id = e.effect_operation_id) AS transition_count "
            "FROM effect_operations e "
            f"JOIN commands c ON c.command_id = e.command_id {where} "
            "ORDER BY e.created_at_ms DESC, e.effect_operation_id DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()

    def _commands(
        self,
        strategy_instance_id: str | None,
        limit: int,
    ) -> tuple[ProjectedCommand, ...]:
        where, params = _scope_filter("strategy_instance_id", strategy_instance_id)
        rows = self._conn.execute(
            "SELECT command_id, kind, action, state, run_id, receipt_id, created_at_ms, "
            f"updated_at_ms FROM commands {where} "
            "ORDER BY updated_at_ms DESC, command_id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return tuple(ProjectedCommand(**dict(row)) for row in rows)

    def _orders_by_operation(
        self,
        operation_ids: tuple[str, ...],
    ) -> dict[str, tuple[ProjectedOrder, ...]]:
        try:
            return read_orders_by_operation(self._conn, operation_ids)
        except OrderProjectionReadError as exc:
            raise ProjectionReadError(str(exc)) from exc

    def _current_orders(
        self,
        strategy_instance_id: str | None,
    ) -> tuple[ProjectedOrder, ...]:
        try:
            return read_current_orders(self._conn, strategy_instance_id)
        except OrderProjectionReadError as exc:
            raise ProjectionReadError(str(exc)) from exc

    def _positions(self, strategy_instance_id: str | None) -> tuple[ProjectedPosition, ...]:
        where, params = _scope_filter("strategy_instance_id", strategy_instance_id)
        rows = self._conn.execute(
            "SELECT strategy_instance_id, symbol, attributed_qty, updated_at_ms "
            f"FROM positions {where} ORDER BY strategy_instance_id, symbol",
            params,
        ).fetchall()
        return tuple(ProjectedPosition(**dict(row)) for row in rows)

    def _holds(self, strategy_instance_id: str | None) -> tuple[ProjectedHold, ...]:
        where = "state = 'ACTIVE'"
        params: tuple[object, ...] = ()
        if strategy_instance_id is not None:
            where += " AND (scope = 'ACCOUNT_CLERK' OR strategy_instance_id = ?)"
            params = (strategy_instance_id,)
        rows = self._conn.execute(
            "SELECT hold_id, scope, strategy_instance_id, reason_code, opened_at_ms, "
            f"evidence_refs_json FROM holds WHERE {where} "
            "ORDER BY opened_at_ms DESC LIMIT ?",
            (*params, CURRENT_STATE_LIMIT),
        ).fetchall()
        return tuple(
            ProjectedHold(
                hold_id=row["hold_id"],
                scope=row["scope"],
                strategy_instance_id=row["strategy_instance_id"],
                reason_code=row["reason_code"],
                opened_at_ms=row["opened_at_ms"],
                evidence_refs=_json_string_tuple(row["evidence_refs_json"]),
            )
            for row in rows
        )

    def _uncertainties(
        self,
        strategy_instance_id: str | None,
        now_ms: int,
    ) -> tuple[ProjectedUncertainty, ...]:
        where = "resolved_at_ms IS NULL"
        params: tuple[object, ...] = ()
        if strategy_instance_id is not None:
            where += " AND (scope = 'ACCOUNT_CLERK' OR strategy_instance_id = ?)"
            params = (strategy_instance_id,)
        rows = self._conn.execute(
            "SELECT uncertainty_id, scope, severity, blocks_new_exposure, allows_reduction, "
            "custody_owner, strategy_instance_id, reason_code, headline, explanation, "
            "operator_impact, next_step, observed_at_ms, evidence_refs_json FROM uncertainties "
            f"WHERE {where} ORDER BY observed_at_ms DESC",
            params,
        ).fetchall()
        return tuple(
            ProjectedUncertainty(
                uncertainty_id=row["uncertainty_id"],
                scope=row["scope"],
                severity=row["severity"],
                blocks_new_exposure=bool(row["blocks_new_exposure"]),
                allows_reduction=bool(row["allows_reduction"]),
                custody_owner=row["custody_owner"],
                strategy_instance_id=row["strategy_instance_id"],
                reason_code=row["reason_code"],
                headline=row["headline"],
                explanation=row["explanation"],
                operator_impact=row["operator_impact"],
                next_step=row["next_step"],
                observed_at_ms=row["observed_at_ms"],
                evidence_age_ms=max(0, now_ms - row["observed_at_ms"]),
                evidence_refs=_json_string_tuple(row["evidence_refs_json"]),
            )
            for row in rows
        )

    def _latest_reconciliation(
        self,
        strategy_instance_id: str | None,
        now_ms: int,
        *,
        account_wide_only: bool = False,
    ) -> ProjectedReconciliation | None:
        joins = ""
        where = ""
        params: tuple[object, ...] = ()
        if account_wide_only:
            where = " WHERE r.effect_operation_id IS NULL AND r.order_ref IS NULL"
        elif strategy_instance_id is not None:
            joins = " LEFT JOIN effect_operations e ON e.effect_operation_id = r.effect_operation_id"
            where = " WHERE e.strategy_instance_id = ? OR r.effect_operation_id IS NULL"
            params = (strategy_instance_id,)
        row = self._conn.execute(
            "SELECT r.reconciliation_id, r.effect_operation_id, r.order_ref, r.trigger, "
            "r.attempted_at_ms, r.outcome, r.evidence_refs_json FROM reconciliations r"
            f"{joins}{where} ORDER BY r.attempted_at_ms DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        return ProjectedReconciliation(
            reconciliation_id=row["reconciliation_id"],
            effect_operation_id=row["effect_operation_id"],
            order_ref=row["order_ref"],
            trigger=row["trigger"],
            attempted_at_ms=row["attempted_at_ms"],
            outcome=row["outcome"],
            evidence_age_ms=max(0, now_ms - row["attempted_at_ms"]),
            evidence_refs=_json_string_tuple(row["evidence_refs_json"]),
        )

    def _terminal_receipts(
        self,
        strategy_instance_id: str | None,
    ) -> tuple[ProjectedReceipt, ...]:
        joins = ""
        where = ""
        params: tuple[object, ...] = ()
        if strategy_instance_id is not None:
            joins = " JOIN commands c ON c.command_id = r.command_id"
            where = " WHERE c.strategy_instance_id = ?"
            params = (strategy_instance_id,)
        rows = self._conn.execute(
            "SELECT r.receipt_id, r.command_id, r.effect_operation_id, r.terminal_state, "
            "r.summary_code, r.proof_reference, r.recorded_at_ms FROM receipts r"
            f"{joins}{where} ORDER BY r.recorded_at_ms DESC LIMIT ?",
            (*params, DEFAULT_RECEIPT_LIMIT),
        ).fetchall()
        return tuple(ProjectedReceipt(**dict(row)) for row in rows)

    def _encode_cursor(
        self,
        *,
        anchor_sequence: int,
        before_sequence: int,
        strategy_instance_id: str | None,
        order_ref: str | None,
        effect_operation_id: str | None,
    ) -> str:
        encoded = json.dumps(
            {
                "account_id": self._account_id,
                "authority_generation": self._authority_generation,
                "strategy_instance_id": strategy_instance_id,
                "order_ref": order_ref,
                "effect_operation_id": effect_operation_id,
                "anchor_sequence": anchor_sequence,
                "before_sequence": before_sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def _decode_cursor(
        self,
        cursor: str,
        *,
        strategy_instance_id: str | None,
        order_ref: str | None,
        effect_operation_id: str | None,
    ) -> tuple[int, int]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload: Any = json.loads(
                base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
            )
            expected_scope = (
                payload["account_id"],
                payload["authority_generation"],
                payload["strategy_instance_id"],
                payload["order_ref"],
                payload["effect_operation_id"],
            )
            actual_scope = (
                self._account_id,
                self._authority_generation,
                strategy_instance_id,
                order_ref,
                effect_operation_id,
            )
            anchor = payload["anchor_sequence"]
            before = payload["before_sequence"]
            if (
                expected_scope != actual_scope
                or not isinstance(anchor, int)
                or not isinstance(before, int)
                or anchor < 0
                or before < 1
                or before > anchor + 1
            ):
                raise ValueError("cursor scope or bounds do not match")
            return anchor, before
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidTimelineCursor(
                "Timeline cursor is malformed, stale, or belongs to another scope"
            ) from exc

    def _encode_operation_cursor(
        self,
        *,
        strategy_instance_id: str | None,
        lifecycle_state: str | None,
        run_id: str | None,
        anchor: tuple[int, str],
        before: tuple[int, str],
    ) -> str:
        encoded = json.dumps(
            {
                "kind": "operation_page",
                "account_id": self._account_id,
                "authority_generation": self._authority_generation,
                "strategy_instance_id": strategy_instance_id,
                "lifecycle_state": lifecycle_state,
                "run_id": run_id,
                "anchor": anchor,
                "before": before,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def _decode_operation_cursor(
        self,
        cursor: str,
        *,
        strategy_instance_id: str | None,
        lifecycle_state: str | None,
        run_id: str | None,
    ) -> tuple[tuple[int, str], tuple[int, str]]:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload: Any = json.loads(
                base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
            )
            expected_scope = (
                "operation_page",
                payload["account_id"],
                payload["authority_generation"],
                payload["strategy_instance_id"],
                payload["lifecycle_state"],
                payload["run_id"],
            )
            actual_scope = (
                "operation_page",
                self._account_id,
                self._authority_generation,
                strategy_instance_id,
                lifecycle_state,
                run_id,
            )
            anchor = _operation_cursor_key(payload["anchor"])
            before = _operation_cursor_key(payload["before"])
            if expected_scope != actual_scope or before > anchor:
                raise ValueError("operation cursor scope or bounds do not match")
            return anchor, before
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidTimelineCursor(
                "Operation cursor is malformed, stale, or belongs to another scope"
            ) from exc


def _bounded_limit(limit: int) -> int:
    return min(max(limit, 1), MAX_OPERATION_LIMIT)


def _operation_cursor_key(value: object) -> tuple[int, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], int)
        or not isinstance(value[1], str)
        or value[0] < 0
        or not value[1]
    ):
        raise ValueError("invalid operation cursor key")
    return value[0], value[1]


def _scope_filter(column: str, strategy_instance_id: str | None) -> tuple[str, tuple[object, ...]]:
    if strategy_instance_id is None:
        return "", ()
    return f"WHERE {column} = ?", (strategy_instance_id,)


def _json_string_tuple(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectionReadError("Fold evidence_refs_json is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ProjectionReadError("Fold evidence_refs_json must contain a string list")
    return tuple(decoded)


def _operation_ref(row: sqlite3.Row) -> str:
    for key in ("effect_operation_id", "command_id", "order_ref"):
        value = row[key]
        if value is not None:
            return str(value)
    return f"transition:{row['sequence']}"


def _timeline_entry(row: sqlite3.Row) -> TimelineEntry:
    return TimelineEntry(
        sequence=row["sequence"],
        operation_ref=_operation_ref(row),
        effect_operation_id=row["effect_operation_id"],
        command_id=row["command_id"],
        order_ref=row["order_ref"],
        broker_order_id=row["broker_order_id"],
        transition_kind=row["transition_kind"],
        operation_state=row["operation_state"],
        broker_state=row["broker_state"],
        custody_owner=row["custody_owner"],
        execution_authority=row["execution_authority"],
        summary_code=row["summary_code"],
        proof_reference=row["proof_reference"],
        source_event_at_ms=row["source_event_at_ms"],
        clerk_observed_at_ms=row["clerk_observed_at_ms"],
        recorded_at_ms=row["recorded_at_ms"],
    )


def timeline_sequences(entries: Iterable[TimelineEntry]) -> tuple[int, ...]:
    """Small public seam used by qualification tests and cursor assertions."""
    return tuple(entry.sequence for entry in entries)
