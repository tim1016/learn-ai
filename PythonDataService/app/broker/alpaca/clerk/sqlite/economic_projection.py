"""SQLite-native execution economics for the Alpaca Account Clerk.

All reads in this module use materialized SQLite folds.  They do not consult
the legacy order journal, process registry, Postgres, or a broker endpoint.
The only P&L arithmetic is delegated to the canonical FIFO implementation in
``app.broker.alpaca.clerk.fifo_pnl``.

Correction time semantics are deliberately split in two:

* ``recorded_at_ms`` remains the correction's immutable audit-arrival time.
* FIFO and session-window projection use the root superseded execution's
  original economic time.  A late correction must not move an already-closed
  lot from its actual closing session into the correction-arrival session.

Formula: FIFO lots over effective SQLite execution slices; realized session
P&L is the sum of closed lots whose close time is in the NYSE half-open
session window.
Reference: ``docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md``
  S2 and ``app.broker.alpaca.clerk.fifo_pnl``.
Canonical implementation: ``app.broker.alpaca.clerk.fifo_pnl`` (this module
  is a SQLite read/projection adapter and calls its FIFO primitives directly).
Validated against: ``PythonDataService/tests/broker/alpaca/clerk/sqlite/``
  ``test_economic_projection.py``.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import threading
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.broker.alpaca.clerk.fifo_pnl import (
    ClosedLot,
    _Lot,
    apply_fill_to_lots,
    compute_fifo_pnl,
    compute_open_pnl,
    realized_pnl_for_window,
)
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.sqlite.economic_projection_models import (
    AccountPnlAttribution,
    EconomicSnapshot,
    ExecutionCoverage,
    ExecutionOrigin,
    ExecutionPage,
    ExecutionRow,
    ExecutionState,
    FeeFidelity,
    FifoAttributionRow,
    FillPage,
    FillWindowProjection,
    MarketMark,
    SessionEconomicProjection,
)
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
)
from app.broker.contract.models import OrderSide
from app.engine.live.order_identity import parse_order_ref
from app.lean_sidecar.trading_calendar import SessionWindow

DEFAULT_FILL_PAGE_LIMIT = 50
MAX_FILL_PAGE_LIMIT = 100
DEFAULT_RECENT_FILL_LIMIT = 20
DEFAULT_CHART_FILL_WINDOW_LIMIT = 3_000
MAX_CHART_FILL_WINDOW_LIMIT = 3_000
MAX_TRANSACTION_DETAIL_EXECUTIONS = 100

# Shared by every reader that must resolve "the fill(s) in effect right now"
# from a correction chain: walk each fill backward via `superseded_execution_ref`
# to its root, then keep only fills with no successor (nothing supersedes them).
# The root's source/recorded time is exposed as `root_source_event_at_ms` /
# `root_recorded_at_ms` for callers that need the *original* economic time of
# a corrected fill, not the correction's arrival time. Continue with `, <name>
# AS (...)` or a direct `SELECT ... LEFT JOIN roots ON ...`.
_EFFECTIVE_FILL_LINEAGE_CTE = """
    WITH RECURSIVE lineage(
        effective_fill_id,
        parent_execution_id,
        depth,
        root_source_event_at_ms,
        root_recorded_at_ms,
        root_transition_sequence
    ) AS (
        SELECT f.fill_id, f.superseded_execution_ref, 0,
               f.source_event_at_ms, f.recorded_at_ms, f.recorded_transition_sequence
        FROM fills f
        WHERE NOT EXISTS (
            SELECT 1 FROM fills successor
            WHERE successor.superseded_execution_ref = f.execution_id
        )
        UNION ALL
        SELECT lineage.effective_fill_id, parent.superseded_execution_ref,
               lineage.depth + 1, parent.source_event_at_ms, parent.recorded_at_ms,
               parent.recorded_transition_sequence
        FROM lineage
        JOIN fills parent ON parent.execution_id = lineage.parent_execution_id
    ), roots AS (
        SELECT effective_fill_id, root_source_event_at_ms, root_recorded_at_ms,
               root_transition_sequence
        FROM lineage
        WHERE parent_execution_id IS NULL
    )"""


class EconomicProjectionError(RuntimeError):
    """SQLite could not produce a coherent execution-economic projection."""


class InvalidEconomicCursor(EconomicProjectionError):
    """A fill/execution page cursor is malformed or mismatched to its query."""


class EconomicProjectionIdentityMismatch(EconomicProjectionError):
    """The read connection is not the expected Clerk authority generation."""


class EconomicProjectionUnavailable(EconomicProjectionError):
    """The authoritative folds cannot safely support an economic answer.

    Callers must render this as unavailable/hold.  They must not turn a
    mismatched ledger into a verified zero or fall back to another authority.
    """


@dataclass
class _CachedFifo:
    """Incremental SQLite-to-canonical FIFO mirror for one bot.

    Formula: apply each newly effective fill through
    ``fifo_pnl.apply_fill_to_lots``.  The canonical FIFO file owns every
    numerical operation; this cache only retains its mutable inputs between
    equal or append-only SQLite revisions.  A correction or historical insert
    invalidates the prefix and rebuilds from authoritative effective rows.
    Reference: ``fifo_pnl.py``.
    Canonical implementation: ``fifo_pnl.py::apply_fill_to_lots``.
    Validated against: ``test_economic_projection.py``.
    """

    records: tuple[FillRecord, ...] = ()
    lots: dict[str, deque[_Lot]] = field(default_factory=dict)
    closed_lots: list[ClosedLot] = field(default_factory=list)
    realized: list[float] = field(default_factory=lambda: [0.0])
    fee_total: float | None = 0.0
    fee_missing: bool = False

    def project(
        self,
        records: tuple[FillRecord, ...],
    ) -> tuple[list[ClosedLot], dict[str, deque[_Lot]], FeeFidelity]:
        if not _is_prefix(self.records, records):
            self.records = ()
            self.lots = {}
            self.closed_lots = []
            self.realized = [0.0]
            self.fee_total = 0.0
            self.fee_missing = False

        for record in records[len(self.records) :]:
            apply_fill_to_lots(self.lots, record, self.closed_lots, self.realized)
            if record.fee is None:
                self.fee_missing = True
                self.fee_total = None
            elif not self.fee_missing:
                self.fee_total = (self.fee_total or 0.0) + record.fee

        self.records = records
        fee_fidelity: FeeFidelity = "not_reported" if self.fee_missing else "reported"
        return self.closed_lots, self.lots, fee_fidelity


def _is_prefix(prefix: tuple[FillRecord, ...], values: tuple[FillRecord, ...]) -> bool:
    return len(prefix) <= len(values) and values[: len(prefix)] == prefix


class SqliteEconomicProjectionReader:
    """Read SQLite-native fills and economics at coherent authority revisions.

    The reader opens its own query-only SQLite connection.  Every public
    method uses one explicit read transaction so the returned revision, fills,
    positions, and P&L cannot straddle a writer commit.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
    ) -> None:
        self._db_path = db_path
        self._account_id = account_id
        self._authority_generation = authority_generation
        self._db_identity_token = db_identity_token
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only = ON")
        self._fifo_cache: dict[str, _CachedFifo] = {}
        with self._read_transaction():
            self._verify_identity()

    @classmethod
    def from_repository(cls, repository: ClerkSqliteRepository) -> SqliteEconomicProjectionReader:
        meta = repository.control_meta_snapshot()
        return cls(
            db_path=repository.db_path,
            account_id=meta.account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def bot_fills(
        self,
        strategy_instance_id: str,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_FILL_PAGE_LIMIT,
        from_ms: int | None = None,
        to_ms: int | None = None,
    ) -> FillPage:
        """Return a bounded newest-first page of current effective fill slices."""
        _validate_window(from_ms=from_ms, to_ms=to_ms)
        limit = _bounded_limit(limit)
        filters = {
            "bot": strategy_instance_id,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "kind": "bot_fills",
        }
        with self._read_transaction():
            meta = self._verified_meta()
            cursor_key = _decode_cursor(
                cursor,
                filters=filters,
                control_revision=meta.control_revision,
            )
            rows = self._effective_fill_rows(
                strategy_instance_ids=(strategy_instance_id,),
                from_ms=from_ms,
                to_ms=to_ms,
                cursor_key=cursor_key,
                limit=limit,
            )
        visible, next_cursor = _visible_page(
            rows,
            limit=limit,
            filters=filters,
            control_revision=meta.control_revision,
        )
        return FillPage(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=meta.authority_generation,
            control_revision=meta.control_revision,
            fills=tuple(_to_fill_record(row, account_id=self._account_id) for row in visible),
            next_cursor=next_cursor,
        )

    def bot_fill_window(
        self,
        strategy_instance_id: str,
        *,
        from_ms: int,
        to_ms: int,
        limit: int = DEFAULT_CHART_FILL_WINDOW_LIMIT,
    ) -> FillWindowProjection | None:
        """Return every effective SQLite fill in a bounded chart window.

        Chart marker consumers need a complete, coherent marker set, not a
        paginated tail. The configured cap is therefore fail-closed: when the
        requested window would exceed it, callers receive an explicit
        unavailable projection instead of silently omitting executions.
        """
        _validate_window(from_ms=from_ms, to_ms=to_ms)
        limit = _bounded_chart_fill_window_limit(limit)
        with self._read_transaction():
            meta = self._verified_meta()
            exists = self._conn.execute(
                "SELECT 1 FROM strategy_instances WHERE strategy_instance_id = ?",
                (strategy_instance_id,),
            ).fetchone()
            if exists is None:
                return None
            rows = self._effective_fill_rows(
                strategy_instance_ids=(strategy_instance_id,),
                from_ms=from_ms,
                to_ms=to_ms,
                cursor_key=None,
                limit=limit,
            )
            if len(rows) > limit:
                raise EconomicProjectionUnavailable(
                    "SQLite chart fill window limit exceeded; narrow the requested range."
                )
            fills = tuple(
                sorted(
                    (_to_fill_record(row, account_id=self._account_id) for row in rows),
                    key=lambda record: (record.filled_at_ms, record.ledger_sequence),
                )
            )
            return FillWindowProjection(
                account_id=self._account_id,
                strategy_instance_id=strategy_instance_id,
                authority_generation=meta.authority_generation,
                control_revision=meta.control_revision,
                fills=fills,
            )

    def account_executions(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_FILL_PAGE_LIMIT,
        origin: ExecutionOrigin | None = None,
        bot: str | None = None,
        symbol: str | None = None,
        state: ExecutionState | None = None,
    ) -> ExecutionPage:
        """Return a bounded account-history execution page from SQLite folds.

        S2 has only registered bot fills.  The ``origin`` filter nevertheless
        accepts the complete future desk vocabulary; ``manual``, ``external``,
        and ``unknown`` therefore return an empty page until S4 writes those
        independent observation rows instead of fabricating bot executions.
        """
        if origin not in (None, "strategy", "manual", "external", "unknown"):
            raise ValueError("unsupported execution origin")
        if state not in (None, "effective", "superseded"):
            raise ValueError("unsupported execution state")
        limit = _bounded_limit(limit)
        filters = {
            "bot": bot,
            "origin": origin,
            "state": state,
            "symbol": symbol.upper() if symbol else None,
            "kind": "account_executions",
        }
        with self._read_transaction():
            meta = self._verified_meta()
            cursor_key = _decode_cursor(
                cursor,
                filters=filters,
                control_revision=meta.control_revision,
            )
            rows = self._account_execution_rows(
                bot=bot,
                origin=origin,
                symbol=symbol,
                state=state,
                cursor_key=cursor_key,
                limit=limit,
            )
        visible, next_cursor = _visible_page(
            rows,
            limit=limit,
            filters=filters,
            control_revision=meta.control_revision,
        )
        return ExecutionPage(
            account_id=self._account_id,
            authority_generation=meta.authority_generation,
            control_revision=meta.control_revision,
            executions=tuple(_to_execution_row(row) for row in visible),
            next_cursor=next_cursor,
        )

    def account_pnl_attribution(
        self,
        *,
        from_ms: int,
        to_ms: int,
        marks: Mapping[str, MarketMark] | None = None,
    ) -> AccountPnlAttribution:
        """Return account-wide FIFO attribution for inclusive UTC-ms bounds.

        Formula: window realized P&L = ``Σ closed_lot.realized_pnl`` for
        ``from_ms <= closed_at_ms <= to_ms``; open P&L delegates to the
        canonical FIFO mark valuation.  The lifetime fill scan is intentional:
        an in-window sell can close a lot opened before ``from_ms``.
        Reference: GAAP/IFRS FIFO; Kieso, Weygandt & Warfield, *Intermediate
          Accounting* (17e), Chapter 8.
        Canonical implementation: ``app.broker.alpaca.clerk.fifo_pnl``; this
          SQLite read model only supplies authoritative effective fills.
        Validated against:
          ``tests/broker/alpaca/clerk/sqlite/test_economic_projection.py``.
        """
        _validate_inclusive_window(from_ms=from_ms, to_ms=to_ms)
        with self._read_transaction():
            meta = self._verified_meta()
            strategy_instance_ids = tuple(
                str(row["strategy_instance_id"])
                for row in self._conn.execute(
                    "SELECT strategy_instance_id FROM strategy_instances ORDER BY strategy_instance_id"
                ).fetchall()
            )
            all_rows = self._effective_fill_rows(
                strategy_instance_ids=strategy_instance_ids,
                from_ms=None,
                to_ms=None,
                cursor_key=None,
                limit=None,
            )

        records = tuple(
            sorted(
                (_to_fill_record(row, account_id=self._account_id) for row in all_rows),
                key=lambda record: (record.filled_at_ms, record.ledger_sequence),
            )
        )
        marks_by_symbol = dict(marks or {})
        fifo_result = compute_fifo_pnl(
            records,
            mark_prices={symbol: mark.price for symbol, mark in marks_by_symbol.items()},
        )
        attribution_rows = tuple(
            _to_fifo_attribution_row(lot) for lot in fifo_result.closed_lots if from_ms <= lot.closed_at_ms <= to_ms
        )
        return AccountPnlAttribution(
            account_id=self._account_id,
            authority_generation=meta.authority_generation,
            control_revision=meta.control_revision,
            from_ms=from_ms,
            to_ms=to_ms,
            attribution_rows=attribution_rows,
            realized_pnl_total=sum(row.realized_pnl for row in attribution_rows),
            open_pnl_total=fifo_result.open_pnl,
            marks_complete=fifo_result.marks_complete,
            mark_observed_at_ms={
                lot.symbol: marks_by_symbol[lot.symbol].observed_at_ms
                for lot in fifo_result.open_lots
                if lot.symbol in marks_by_symbol
            },
        )

    def effective_execution_summaries(
        self,
        order_refs: Sequence[str],
    ) -> dict[str, tuple[ExecutionRow, ...]]:
        """Return at most two effective slices for each requested order.

        Transaction-list summaries only expose economics when precisely one
        effective execution exists.  Reading two rows per order establishes
        that condition without aggregating price, quantity, or fees in a
        presentation adapter.  The returned tuples are newest first and are
        sourced from the same S2 execution-fold read model as account history.
        """
        refs = _distinct_order_refs(order_refs)
        if not refs:
            return {}
        with self._read_transaction():
            self._verified_meta()
            rows = self._effective_execution_rows_for_orders(
                refs,
                per_order_limit=2,
                total_limit=None,
            )
        grouped: dict[str, list[ExecutionRow]] = {order_ref: [] for order_ref in refs}
        for row in rows:
            grouped[row["order_ref"]].append(_to_execution_row(row))
        return {order_ref: tuple(grouped[order_ref]) for order_ref in refs}

    def effective_order_executions(
        self,
        order_refs: Sequence[str],
        *,
        limit: int = MAX_TRANSACTION_DETAIL_EXECUTIONS,
    ) -> tuple[ExecutionRow, ...]:
        """Return every effective execution for one bounded transaction detail.

        Detail views must retain every effective slice rather than inventing
        aggregate execution economics.  The cap is intentionally fail-closed:
        callers receive :class:`EconomicProjectionUnavailable` instead of a
        silently truncated audit trail.
        """
        refs = _distinct_order_refs(order_refs)
        if not refs:
            return ()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("execution detail limit must be a positive integer")
        with self._read_transaction():
            self._verified_meta()
            rows = self._effective_execution_rows_for_orders(
                refs,
                per_order_limit=None,
                total_limit=limit,
            )
        if len(rows) > limit:
            raise EconomicProjectionUnavailable(
                "SQLite transaction execution detail exceeds the safe event limit."
            )
        return tuple(_to_execution_row(row) for row in rows)

    def bot_economic_snapshot(
        self,
        strategy_instance_id: str,
        *,
        session_window: SessionWindow | None,
        marks: Mapping[str, MarketMark] | None = None,
        recent_fill_limit: int = DEFAULT_RECENT_FILL_LIMIT,
    ) -> EconomicSnapshot | None:
        """Project one bot's full FIFO economics from its effective SQLite fills.

        The lifetime fill scan is intentionally distinct from the bounded
        recent-fill list: FIFO must see earlier opening lots to value a sell in
        the requested session correctly.  Equal/append-only revisions reuse
        the incremental ``apply_fill_to_lots`` cache; corrections rebuild
        from the current effective set.
        """
        recent_fill_limit = _bounded_limit(recent_fill_limit)
        with self._read_transaction():
            projection = self._project_bot_session_economics(
                strategy_instance_id,
                session_window=session_window,
                marks=marks,
                recent_fill_limit=recent_fill_limit,
            )
        return None if projection is None else projection.snapshot

    def bot_session_economic_projection(
        self,
        strategy_instance_id: str,
        *,
        session_window: SessionWindow | None,
        marks: Mapping[str, MarketMark] | None = None,
        recent_fill_limit: int = DEFAULT_RECENT_FILL_LIMIT,
    ) -> SessionEconomicProjection | None:
        """Return a chart-safe fill set and economics from one SQLite revision.

        The explicit session window bounds the complete marker list. ``None``
        denotes a non-NYSE date and returns verified zero session metrics.
        Callers use ``snapshot`` for panel economics and ``session_fills`` for
        chart markers; no JSONL-derived fill source may be combined with this
        result.
        """
        recent_fill_limit = _bounded_limit(recent_fill_limit)
        with self._read_transaction():
            return self._project_bot_session_economics(
                strategy_instance_id,
                session_window=session_window,
                marks=marks,
                recent_fill_limit=recent_fill_limit,
            )

    def _project_bot_session_economics(
        self,
        strategy_instance_id: str,
        *,
        session_window: SessionWindow | None,
        marks: Mapping[str, MarketMark] | None,
        recent_fill_limit: int,
    ) -> SessionEconomicProjection | None:
        """Project one bot while the caller holds this reader's read transaction."""
        meta = self._verified_meta()
        exists = self._conn.execute(
            "SELECT 1 FROM strategy_instances WHERE strategy_instance_id = ?",
            (strategy_instance_id,),
        ).fetchone()
        if exists is None:
            return None
        all_rows = self._effective_fill_rows(
            strategy_instance_ids=(strategy_instance_id,),
            from_ms=None,
            to_ms=None,
            cursor_key=None,
            limit=None,
        )
        records = tuple(
            sorted(
                (_to_fill_record(row, account_id=self._account_id) for row in all_rows),
                key=lambda record: (record.filled_at_ms, record.ledger_sequence),
            )
        )
        decision_activity_at_ms = self._decision_activity_at_ms(strategy_instance_id)
        exposure = self._exposure(strategy_instance_id)
        _assert_effective_fills_match_positions(records, exposure)
        coverage = self._execution_coverage(strategy_instance_id, all_rows)
        # The reader's lock covers the incremental cache too: two callers
        # cannot interleave an append-only cache update after observing
        # different SQLite revisions.
        cache = self._fifo_cache.setdefault(strategy_instance_id, _CachedFifo())
        closed_lots, lots, fee_fidelity = cache.project(records)
        marks_by_symbol = dict(marks or {})
        open_result = compute_open_pnl(
            lots,
            {symbol: mark.price for symbol, mark in marks_by_symbol.items()},
        )
        relevant_mark_times = {
            lot.symbol: marks_by_symbol[lot.symbol].observed_at_ms
            for lot in open_result.open_lots
            if lot.symbol in marks_by_symbol
        }
        session_fills = (
            ()
            if session_window is None
            else tuple(
                record
                for record in records
                if session_window.open_ms_utc <= record.filled_at_ms < session_window.close_ms_utc
            )
        )
        snapshot = EconomicSnapshot(
            account_id=self._account_id,
            strategy_instance_id=strategy_instance_id,
            authority_generation=meta.authority_generation,
            control_revision=meta.control_revision,
            session_open_ms=(session_window.open_ms_utc if session_window is not None else None),
            session_close_ms=(session_window.close_ms_utc if session_window is not None else None),
            recent_fills=tuple(records[-recent_fill_limit:][::-1]),
            fills_today=len(session_fills),
            exposure=exposure,
            realized_pnl_today=(
                0.0
                if session_window is None
                else realized_pnl_for_window(
                    closed_lots,
                    session_open_ms=session_window.open_ms_utc,
                    session_close_ms=session_window.close_ms_utc,
                )
            ),
            open_pnl=open_result.value,
            marks_complete=open_result.marks_complete,
            mark_observed_at_ms=relevant_mark_times,
            fee_fidelity=fee_fidelity,
            execution_coverage=coverage,
            last_activity_at_ms=_latest_activity_at_ms(
                max((record.filled_at_ms for record in records), default=None),
                decision_activity_at_ms,
            ),
        )
        return SessionEconomicProjection(snapshot=snapshot, session_fills=session_fills)

    def catalog_economic_rollup(
        self,
        strategy_instance_ids: Sequence[str],
        *,
        session_window: SessionWindow | None,
        marks_by_strategy: Mapping[str, Mapping[str, MarketMark]] | None = None,
    ) -> dict[str, EconomicSnapshot]:
        """Return every requested bot snapshot from one SQLite read transaction."""
        unique_ids = tuple(dict.fromkeys(strategy_instance_ids))
        if not unique_ids:
            return {}
        with self._read_transaction():
            meta = self._verified_meta()
            existing = {
                row["strategy_instance_id"]
                for row in self._conn.execute(
                    f"SELECT strategy_instance_id FROM strategy_instances WHERE strategy_instance_id IN ({_placeholders(unique_ids)})",
                    unique_ids,
                ).fetchall()
            }
            decision_activity_by_strategy = {
                str(row["strategy_instance_id"]): int(row["last_activity_at_ms"])
                for row in self._conn.execute(
                    "SELECT strategy_instance_id, MAX(observed_at_ms) AS last_activity_at_ms "
                    f"FROM decision_receipts WHERE strategy_instance_id IN ({_placeholders(unique_ids)}) "
                    "GROUP BY strategy_instance_id",
                    unique_ids,
                ).fetchall()
            }
            active_ids = tuple(
                strategy_instance_id
                for strategy_instance_id in unique_ids
                if strategy_instance_id in existing
            )
            rows_by_strategy = {strategy_instance_id: [] for strategy_instance_id in active_ids}
            for row in self._effective_fill_rows(
                strategy_instance_ids=active_ids,
                from_ms=None,
                to_ms=None,
                cursor_key=None,
                limit=None,
            ):
                rows_by_strategy[str(row["strategy_instance_id"])].append(row)
            result: dict[str, EconomicSnapshot] = {}
            for strategy_instance_id in unique_ids:
                if strategy_instance_id not in existing:
                    continue
                all_rows = rows_by_strategy[strategy_instance_id]
                records = tuple(
                    sorted(
                        (_to_fill_record(row, account_id=self._account_id) for row in all_rows),
                        key=lambda record: (record.filled_at_ms, record.ledger_sequence),
                    )
                )
                exposure = self._exposure(strategy_instance_id)
                _assert_effective_fills_match_positions(records, exposure)
                coverage = self._execution_coverage(strategy_instance_id, all_rows)
                cache = self._fifo_cache.setdefault(strategy_instance_id, _CachedFifo())
                closed_lots, lots, fee_fidelity = cache.project(records)
                marks = dict((marks_by_strategy or {}).get(strategy_instance_id, {}))
                open_result = compute_open_pnl(
                    lots,
                    {symbol: mark.price for symbol, mark in marks.items()},
                )
                mark_times = {
                    lot.symbol: marks[lot.symbol].observed_at_ms
                    for lot in open_result.open_lots
                    if lot.symbol in marks
                }
                session_fills = (
                    ()
                    if session_window is None
                    else tuple(
                        record
                        for record in records
                        if session_window.open_ms_utc
                        <= record.filled_at_ms
                        < session_window.close_ms_utc
                    )
                )
                result[strategy_instance_id] = EconomicSnapshot(
                    account_id=self._account_id,
                    strategy_instance_id=strategy_instance_id,
                    authority_generation=meta.authority_generation,
                    control_revision=meta.control_revision,
                    session_open_ms=(session_window.open_ms_utc if session_window is not None else None),
                    session_close_ms=(session_window.close_ms_utc if session_window is not None else None),
                    recent_fills=tuple(records[-DEFAULT_RECENT_FILL_LIMIT:][::-1]),
                    fills_today=len(session_fills),
                    exposure=exposure,
                    realized_pnl_today=(
                        0.0
                        if session_window is None
                        else realized_pnl_for_window(
                            closed_lots,
                            session_open_ms=session_window.open_ms_utc,
                            session_close_ms=session_window.close_ms_utc,
                        )
                    ),
                    open_pnl=open_result.value,
                    marks_complete=open_result.marks_complete,
                    mark_observed_at_ms=mark_times,
                    fee_fidelity=fee_fidelity,
                    execution_coverage=coverage,
                    last_activity_at_ms=_latest_activity_at_ms(
                        max((record.filled_at_ms for record in records), default=None),
                        decision_activity_by_strategy.get(strategy_instance_id),
                    ),
                )
        return result

    def _decision_activity_at_ms(self, strategy_instance_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(observed_at_ms) AS last_activity_at_ms FROM decision_receipts "
            "WHERE strategy_instance_id = ?",
            (strategy_instance_id,),
        ).fetchone()
        if row is None or row["last_activity_at_ms"] is None:
            return None
        return int(row["last_activity_at_ms"])

    def _verified_meta(self) -> ControlMetaSnapshot:
        self._verify_identity()
        row = self._conn.execute(
            "SELECT schema_version, account_id, db_identity_token, authority_generation, "
            "control_revision, created_at_ms, last_open_at_ms FROM control_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise EconomicProjectionError("SQLite authority has no control_meta row")
        return ControlMetaSnapshot(**dict(row))

    def _verify_identity(self) -> None:
        row = self._conn.execute(
            "SELECT account_id, authority_generation, db_identity_token FROM control_meta WHERE id = 1"
        ).fetchone()
        if row is None:
            raise EconomicProjectionIdentityMismatch("SQLite authority has no control_meta row")
        if (
            row["account_id"] != self._account_id
            or row["authority_generation"] != self._authority_generation
            or row["db_identity_token"] != self._db_identity_token
        ):
            raise EconomicProjectionIdentityMismatch("SQLite authority identity changed during projection")

    def _effective_fill_rows(
        self,
        *,
        strategy_instance_ids: Sequence[str],
        from_ms: int | None,
        to_ms: int | None,
        cursor_key: tuple[int, str] | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        """Read current fill leaves with their root economic timestamps.

        The recursive CTE follows a correction chain backward from every
        effective row.  The root's source timestamp determines FIFO order and
        session membership; the outer row's ``recorded_at_ms`` remains the
        audit/keyset timestamp.
        """
        if not strategy_instance_ids:
            return []
        where = [
            f"effective.strategy_instance_id IN ({_placeholders(strategy_instance_ids)})"
        ]
        params: list[object] = [*strategy_instance_ids]
        if from_ms is not None:
            where.append("effective.economic_filled_at_ms >= ?")
            params.append(from_ms)
        if to_ms is not None:
            where.append("effective.economic_filled_at_ms < ?")
            params.append(to_ms)
        if cursor_key is not None:
            where.append(
                "(effective.recorded_at_ms < ? OR "
                "(effective.recorded_at_ms = ? AND effective.execution_sort_key < ?))"
            )
            params.extend((cursor_key[0], cursor_key[0], cursor_key[1]))
        limit_sql = "" if limit is None else " LIMIT ?"
        if limit is not None:
            params.append(limit + 1)
        sql = f"""
            {_EFFECTIVE_FILL_LINEAGE_CTE}, effective AS (
                SELECT f.fill_id, f.order_ref, f.qty, f.price, f.side, f.execution_id,
                       f.evidence_source, f.event_kind, f.fee, f.fee_fidelity,
                       f.recorded_at_ms, f.recorded_transition_sequence,
                       e.strategy_instance_id, s.symbol,
                       COALESCE(roots.root_source_event_at_ms, roots.root_recorded_at_ms,
                                f.source_event_at_ms, f.recorded_at_ms) AS economic_filled_at_ms,
                       COALESCE(roots.root_transition_sequence,
                                f.recorded_transition_sequence) AS economic_ledger_sequence,
                       COALESCE(f.execution_id, f.fill_id) AS execution_sort_key
                FROM fills f
                JOIN orders o ON o.order_ref = f.order_ref
                JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id
                JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id
                JOIN roots ON roots.effective_fill_id = f.fill_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM fills successor
                    WHERE successor.superseded_execution_ref = f.execution_id
                )
            )
            SELECT effective.* FROM effective
            WHERE {' AND '.join(where)}
            ORDER BY effective.recorded_at_ms DESC, effective.execution_sort_key DESC{limit_sql}
        """
        return self._conn.execute(sql, params).fetchall()

    def _account_execution_rows(
        self,
        *,
        bot: str | None,
        origin: ExecutionOrigin | None,
        symbol: str | None,
        state: ExecutionState | None,
        cursor_key: tuple[int, str] | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        if origin is not None and origin != "strategy":
            return []
        where = ["1 = 1"]
        params: list[object] = []
        if bot is not None:
            where.append("e.strategy_instance_id = ?")
            params.append(bot)
        if symbol is not None:
            where.append("s.symbol = ?")
            params.append(symbol.upper())
        if state == "effective":
            where.append(
                "NOT EXISTS (SELECT 1 FROM fills successor "
                "WHERE successor.superseded_execution_ref = f.execution_id)"
            )
        elif state == "superseded":
            where.append(
                "EXISTS (SELECT 1 FROM fills successor "
                "WHERE successor.superseded_execution_ref = f.execution_id)"
            )
        if cursor_key is not None:
            where.append(
                "(f.recorded_at_ms < ? OR (f.recorded_at_ms = ? "
                "AND COALESCE(f.execution_id, f.fill_id) < ?))"
            )
            params.extend((cursor_key[0], cursor_key[0], cursor_key[1]))
        params.append(limit + 1)
        sql = f"""
            {_EFFECTIVE_FILL_LINEAGE_CTE}
            SELECT f.fill_id, f.execution_id, f.order_ref, f.qty, f.price, f.side,
                   f.event_kind, f.fee, f.fee_fidelity, f.recorded_at_ms,
                   f.recorded_transition_sequence,
                   e.strategy_instance_id, s.symbol,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM fills successor
                       WHERE successor.superseded_execution_ref = f.execution_id
                   ) THEN 'superseded' ELSE 'effective' END AS state,
                   COALESCE(roots.root_source_event_at_ms, roots.root_recorded_at_ms,
                            f.source_event_at_ms, f.recorded_at_ms) AS filled_at_ms
            FROM fills f
            JOIN orders o ON o.order_ref = f.order_ref
            JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id
            JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id
            LEFT JOIN roots ON roots.effective_fill_id = f.fill_id
            WHERE {' AND '.join(where)}
            ORDER BY f.recorded_at_ms DESC, COALESCE(f.execution_id, f.fill_id) DESC
            LIMIT ?
        """
        return self._conn.execute(sql, params).fetchall()

    def _effective_execution_rows_for_orders(
        self,
        order_refs: tuple[str, ...],
        *,
        per_order_limit: int | None,
        total_limit: int | None,
    ) -> list[sqlite3.Row]:
        """Read current execution leaves for explicit SQLite order identities."""
        placeholders = _placeholders(order_refs)
        params: list[object] = [*order_refs]
        per_order_where = ""
        if per_order_limit is not None:
            per_order_where = "WHERE slice_rank <= ?"
            params.append(per_order_limit)
        total_limit_sql = ""
        if total_limit is not None:
            total_limit_sql = "LIMIT ?"
            params.append(total_limit + 1)
        sql = f"""
            {_EFFECTIVE_FILL_LINEAGE_CTE}, effective AS (
                SELECT f.fill_id, f.execution_id, f.order_ref, f.qty, f.price, f.side,
                       f.event_kind, f.fee, f.fee_fidelity, f.recorded_at_ms,
                       f.recorded_transition_sequence,
                       e.strategy_instance_id, s.symbol,
                       'effective' AS state,
                       COALESCE(roots.root_source_event_at_ms, roots.root_recorded_at_ms,
                                f.source_event_at_ms, f.recorded_at_ms) AS filled_at_ms,
                       COALESCE(f.execution_id, f.fill_id) AS execution_sort_key
                FROM fills f
                JOIN orders o ON o.order_ref = f.order_ref
                JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id
                JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id
                JOIN roots ON roots.effective_fill_id = f.fill_id
                WHERE f.order_ref IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM fills successor
                      WHERE successor.superseded_execution_ref = f.execution_id
                  )
            ), ranked AS (
                SELECT effective.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY order_ref
                           ORDER BY recorded_at_ms DESC, execution_sort_key DESC
                       ) AS slice_rank
                FROM effective
            )
            SELECT * FROM ranked
            {per_order_where}
            ORDER BY recorded_at_ms DESC, execution_sort_key DESC
            {total_limit_sql}
        """
        return self._conn.execute(sql, params).fetchall()

    def _exposure(self, strategy_instance_id: str) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT UPPER(symbol) AS symbol, attributed_qty FROM positions "
            "WHERE strategy_instance_id = ? AND attributed_qty != 0 ORDER BY UPPER(symbol)",
            (strategy_instance_id,),
        ).fetchall()
        return {row["symbol"]: float(row["attributed_qty"]) for row in rows}

    def _execution_coverage(
        self,
        strategy_instance_id: str,
        effective_rows: Sequence[sqlite3.Row],
    ) -> ExecutionCoverage:
        if any(row["evidence_source"] == "cumulative_recovery" for row in effective_rows):
            return "incomplete"
        row = self._conn.execute(
            "SELECT 1 FROM uncertainties WHERE strategy_instance_id = ? "
            "AND reason_code = ? AND resolved_at_ms IS NULL LIMIT 1",
            (strategy_instance_id, EXECUTION_COVERAGE_CONFLICT_REASON_CODE),
        ).fetchone()
        return "incomplete" if row is not None else "complete"


def _to_fill_record(row: sqlite3.Row, *, account_id: str) -> FillRecord:
    try:
        _namespace, intent_id = parse_order_ref(row["order_ref"])
    except ValueError as exc:
        raise EconomicProjectionError(
            f"SQLite fill has an invalid owned order_ref: {row['order_ref']!r}"
        ) from exc
    try:
        side = OrderSide(row["side"].lower())
    except ValueError as exc:
        raise EconomicProjectionError(f"SQLite fill has invalid side {row['side']!r}") from exc
    return FillRecord(
        account_id=account_id,
        sid=row["strategy_instance_id"],
        intent_id=intent_id,
        order_ref=row["order_ref"],
        event_key=row["execution_id"] or row["fill_id"],
        symbol=row["symbol"].upper(),
        side=side,
        quantity=float(row["qty"]),
        fill_price=float(row["price"]),
        filled_at_ms=int(row["economic_filled_at_ms"]),
        fee=(float(row["fee"]) if row["fee"] is not None else None),
        ledger_sequence=_required_ledger_sequence(row, field="economic_ledger_sequence"),
    )


def _to_fifo_attribution_row(lot: ClosedLot) -> FifoAttributionRow:
    """Adapt a canonical FIFO close without recomputing any economic field."""
    return FifoAttributionRow(
        symbol=lot.symbol,
        quantity=lot.qty,
        entry_price=lot.entry_price,
        exit_price=lot.exit_price,
        opened_at_ms=lot.opened_at_ms,
        closed_at_ms=lot.closed_at_ms,
        realized_pnl=lot.realized_pnl,
        fee=lot.fee,
        entry_strategy_instance_id=lot.entry_strategy_instance_id,
        exit_strategy_instance_id=lot.exit_strategy_instance_id,
    )


def _to_execution_row(row: sqlite3.Row) -> ExecutionRow:
    try:
        side = OrderSide(row["side"].lower())
    except ValueError as exc:
        raise EconomicProjectionError(f"SQLite fill has invalid side {row['side']!r}") from exc
    return ExecutionRow(
        fill_id=row["fill_id"],
        execution_id=row["execution_id"],
        order_ref=row["order_ref"],
        strategy_instance_id=row["strategy_instance_id"],
        origin="strategy",
        state=row["state"],
        event_kind=row["event_kind"],
        symbol=row["symbol"].upper(),
        side=side,
        quantity=float(row["qty"]),
        price=float(row["price"]),
        fee=(float(row["fee"]) if row["fee"] is not None else None),
        fee_fidelity=row["fee_fidelity"],
        filled_at_ms=int(row["filled_at_ms"]),
        recorded_at_ms=int(row["recorded_at_ms"]),
        journal_seq=_required_ledger_sequence(row),
    )


def _required_ledger_sequence(
    row: sqlite3.Row,
    *,
    field: str = "recorded_transition_sequence",
) -> int:
    """Return a durable execution transition sequence or fail closed.

    A migrated v7 row can lack this value only when the legacy database could
    not prove which custody transition produced the fill. It must not enter a
    FIFO tie-breaker or a transaction receipt under a guessed sequence.
    """
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EconomicProjectionUnavailable(
            "SQLite execution lacks durable custody transition provenance."
        )
    return value


def _assert_effective_fills_match_positions(
    records: Sequence[FillRecord],
    exposure: Mapping[str, float],
) -> None:
    """Fail closed if the two SQLite economic folds disagree.

    ``positions`` is the durable exposure fold and effective fills are the
    FIFO input.  A snapshot that combines them after corruption or a bad
    replay would be internally inconsistent, so it is explicitly unavailable
    rather than reporting a made-up zero or consulting a legacy authority.
    """
    folded: dict[str, float] = {}
    for record in records:
        sign = 1.0 if record.side is OrderSide.BUY else -1.0
        folded[record.symbol] = folded.get(record.symbol, 0.0) + sign * record.quantity
    symbols = {*folded, *exposure}
    mismatches = {
        symbol: (folded.get(symbol, 0.0), float(exposure.get(symbol, 0.0)))
        for symbol in symbols
        if not math.isclose(
            folded.get(symbol, 0.0),
            float(exposure.get(symbol, 0.0)),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    }
    if mismatches:
        raise EconomicProjectionUnavailable(
            f"effective SQLite fills disagree with attributed positions: {mismatches!r}"
        )


def _validate_window(*, from_ms: int | None, to_ms: int | None) -> None:
    for name, value in (("from_ms", from_ms), ("to_ms", to_ms)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{name} must be an integer")
    if from_ms is not None and from_ms < 0:
        raise ValueError("from_ms must be non-negative")
    if to_ms is not None and to_ms < 0:
        raise ValueError("to_ms must be non-negative")
    if from_ms is not None and to_ms is not None and to_ms < from_ms:
        raise ValueError("to_ms must be greater than or equal to from_ms")


def _validate_inclusive_window(*, from_ms: int, to_ms: int) -> None:
    _validate_window(from_ms=from_ms, to_ms=to_ms)
    if from_ms is None or to_ms is None:
        raise ValueError("from_ms and to_ms are required")


def _bounded_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("page limit must be an integer")
    return min(max(limit, 1), MAX_FILL_PAGE_LIMIT)


def _bounded_chart_fill_window_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("chart fill window limit must be an integer")
    return min(max(limit, 1), MAX_CHART_FILL_WINDOW_LIMIT)


def _visible_page(
    rows: list[sqlite3.Row],
    *,
    limit: int,
    filters: dict[str, object],
    control_revision: int,
) -> tuple[list[sqlite3.Row], str | None]:
    visible = rows[:limit]
    if len(rows) <= limit or not visible:
        return visible, None
    last = visible[-1]
    return visible, _encode_cursor(
        recorded_at_ms=int(last["recorded_at_ms"]),
        execution_sort_key=(last["execution_sort_key"] if "execution_sort_key" in last
                            else last["execution_id"] or last["fill_id"]),
        filters=filters,
        control_revision=control_revision,
    )


def _encode_cursor(
    *,
    recorded_at_ms: int,
    execution_sort_key: str,
    filters: dict[str, object],
    control_revision: int,
) -> str:
    payload = json.dumps(
        {
            "filters": filters,
            "recorded_at_ms": recorded_at_ms,
            "execution_sort_key": execution_sort_key,
            "control_revision": control_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(
    cursor: str | None,
    *,
    filters: dict[str, object],
    control_revision: int,
) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        if payload["filters"] != filters:
            raise ValueError("cursor filters do not match request")
        cursor_revision = payload["control_revision"]
        if isinstance(cursor_revision, bool) or not isinstance(cursor_revision, int):
            raise ValueError("cursor execution revision is invalid")
        if cursor_revision != control_revision:
            raise ValueError("cursor execution revision does not match request")
        recorded_at_ms = payload["recorded_at_ms"]
        execution_sort_key = payload["execution_sort_key"]
        if (
            isinstance(recorded_at_ms, bool)
            or not isinstance(recorded_at_ms, int)
            or recorded_at_ms < 0
            or not isinstance(execution_sort_key, str)
            or not execution_sort_key
        ):
            raise ValueError("cursor shape is invalid")
        return recorded_at_ms, execution_sort_key
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise InvalidEconomicCursor("invalid execution page cursor") from exc


def _latest_activity_at_ms(
    fill_activity_at_ms: int | None,
    decision_activity_at_ms: int | None,
) -> int | None:
    """Return durable strategy activity without inventing execution evidence."""
    values = (value for value in (fill_activity_at_ms, decision_activity_at_ms) if value is not None)
    return max(values, default=None)


def _placeholders(values: Sequence[str]) -> str:
    return ", ".join("?" for _ in values)


def _distinct_order_refs(order_refs: Sequence[str]) -> tuple[str, ...]:
    refs = tuple(dict.fromkeys(order_refs))
    if any(not isinstance(order_ref, str) or not order_ref for order_ref in refs):
        raise ValueError("order_refs must contain non-empty strings")
    return refs


__all__ = [
    "DEFAULT_CHART_FILL_WINDOW_LIMIT",
    "DEFAULT_FILL_PAGE_LIMIT",
    "DEFAULT_RECENT_FILL_LIMIT",
    "MAX_TRANSACTION_DETAIL_EXECUTIONS",
    "AccountPnlAttribution",
    "EconomicProjectionError",
    "EconomicProjectionIdentityMismatch",
    "EconomicProjectionUnavailable",
    "EconomicSnapshot",
    "ExecutionPage",
    "ExecutionRow",
    "FifoAttributionRow",
    "FillPage",
    "FillWindowProjection",
    "InvalidEconomicCursor",
    "MarketMark",
    "SessionEconomicProjection",
    "SqliteEconomicProjectionReader",
]
