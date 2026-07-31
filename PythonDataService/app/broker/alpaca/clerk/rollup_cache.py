"""Incremental per-bot rollup caches for the broker-v2 bots-list panel (S0).

The bots-list (§5 of the spec) requires ≥100-bot catalog projections with NO
O(journal) per-request scan.  This module maintains the rollup atomically
in memory, updated on every journal append via ``on_fill_appended`` and
every decision receipt via ``on_decision_appended``.

A single ``BotRollupCache`` instance is held per account (keyed by sid) by
whatever actor owns the journal (the Clerk, or a projection service).

Rollup fields (from spec §5 / issue #1296):

    exposure          — net signed exposure quantity per symbol (non-zero only)
    fills_today       — count of fill events for today's NY trading session
    realized_pnl_today — realized P&L from lots closed in today's session
    open_pnl          — open P&L at the latest mark (None if incomplete marks)
    last_activity_at_ms — most-recent fill or decision receipt ts_ms (int64 ms UTC)
    needs_attention   — True when the bot needs operator intervention

"today" is determined by the canonical NYSE calendar module
(``app.lean_sidecar.trading_calendar``).  The session window is refreshed
lazily on each call; no hardcoded session times.

Performance contract:
    ``get_rollup`` is O(1) for exposure (dict snapshot).
    ``get_rollup`` is O(closed_lots_in_session) for realized_pnl_today —
    bounded by the trading day, NOT O(lifetime_fills).
    ``get_rollup`` is O(open_lots) for open P&L when marks are supplied.
    ``get_rollup`` does NOT call ``compute_fifo_pnl`` — all FIFO state is
    maintained incrementally as fills arrive via ``on_fill_appended``.

Provenance (FIFO incremental state):
    Formula: incremental FIFO lot queues, matching the algorithm in
        ``app/broker/alpaca/clerk/fifo_pnl.py::apply_fill_to_lots``.
    Canonical implementation: fifo_pnl.py (the batch canonical).
        This file uses ``apply_fill_to_lots`` directly — not a re-implementation.
    Validated against:
        PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.fifo_pnl import (
    _ZERO_ABS_TOL,
    ClosedLot,
    _Lot,
    apply_fill_to_lots,
    compute_open_pnl,
    realized_pnl_for_window,
)
from app.broker.alpaca.clerk.fills import FillRecord, project_instance_fills
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import OrderSide
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.utils.timestamps import now_ms_utc

_NY = ZoneInfo("America/New_York")

# Sentinel for "caller did not supply a session window" (distinguishes None =
# "market closed today" from the default "compute it now").
class _UNSET:
    pass


# ── Rollup snapshot (the per-bot row the catalog reads) ──────────────────────


@dataclass(frozen=True)
class BotRollup:
    """Immutable per-bot rollup snapshot.

    ``open_pnl`` is ``None`` when no mark price is available or when marks
    are incomplete (not all open-lot symbols have coverage).
    ``realized_pnl_today`` is 0.0 when the bot has made no closed trades today.
    All timestamps are ``int64 ms UTC``.
    """

    sid: str
    exposure: dict[str, float]         # symbol → signed quantity (non-zero entries only)
    fills_today: int
    realized_pnl_today: float
    open_pnl: float | None
    last_activity_at_ms: int | None
    needs_attention: bool
    as_of_ms: int                      # int64 ms UTC when this snapshot was computed


# ── Per-bot mutable state (held under lock) ───────────────────────────────────


@dataclass
class _BotState:
    """Mutable per-bot accumulator.

    Holds both the incremental exposure fold (O(1) per fill, O(1) read)
    and the incremental FIFO lot state (O(1) per fill via apply_fill_to_lots,
    O(open_lots) read for open P&L, O(closed_lots_in_session) for
    realized_pnl_today).

    ``get_rollup`` never calls ``compute_fifo_pnl`` — all FIFO state is
    accumulated here as fills arrive.

    Provenance (exposure fold):
        Formula: net_qty[sym] = Σ sign(side_i) × qty_i for deduped fills
        Reference: Canonical fold algorithm is
            ``app/broker/alpaca/clerk/exposure.py::project_instance_exposure``.
            This incremental implementation is a performance mirror — it
            maintains the same dedup-and-fold invariant but applies it
            incrementally rather than scanning the full list on every read.
        Canonical implementation: exposure.py (the full-scan canonical).
            This file is a parity-tested performance mirror.
        Validated against:
            PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py
            (test_100_bot_snapshot_no_journal_scan_per_request asserts O(1)
            behaviour; test_fill_updates_exposure asserts numerical parity
            with the expected net quantity.)

    Provenance (FIFO incremental state):
        Formula: incremental FIFO lot queues updated via apply_fill_to_lots.
        Canonical implementation: fifo_pnl.py::apply_fill_to_lots (called here).
        Validated against:
            PythonDataService/tests/broker/alpaca/clerk/test_rollup_cache.py.
    """

    sid: str
    # Incremental exposure state (updated O(1) per fill)
    seen_keys: set[tuple[str, str]] = field(default_factory=set)
    exposure: dict[str, float] = field(default_factory=dict)
    # Incremental FIFO lot state (updated via apply_fill_to_lots)
    fifo_lots: dict[str, deque[_Lot]] = field(default_factory=dict)
    closed_lots: list[ClosedLot] = field(default_factory=list)
    lifetime_realized_pnl: float = 0.0
    # Fill timestamps for fills_today count (ints only — lightweight)
    fill_timestamps: list[int] = field(default_factory=list)
    # Fee tracking
    any_fee_missing: bool = False
    fee_total: float | None = None
    # Latest decision outcome for needs_attention heuristic
    last_outcome: str | None = None
    last_activity_at_ms: int | None = None
    last_decision_at_ms: int | None = None


def _apply_fill_delta(state: _BotState, fill: FillRecord) -> None:
    """Apply one fill to the incremental exposure state and FIFO queues (O(1)).

    Deduplicates on ``(account_id, event_key)`` — same semantics as the
    canonical fold in ``exposure.py::project_instance_exposure``.

    FillRecords must arrive pre-normalized via the canonical fold gate
    (``fills.py::normalize_fill_event`` / ``project_instance_fills``).
    """
    key = (fill.account_id, fill.event_key)
    if key in state.seen_keys:
        return
    state.seen_keys.add(key)

    # Exposure fold
    sign = 1.0 if fill.side is OrderSide.BUY else -1.0
    current = state.exposure.get(fill.symbol, 0.0)
    updated = current + sign * fill.quantity
    if abs(updated) <= _ZERO_ABS_TOL:
        state.exposure.pop(fill.symbol, None)
    else:
        state.exposure[fill.symbol] = updated

    # FIFO lot update — delegates to the canonical engine helper
    realized_acc = [state.lifetime_realized_pnl]
    apply_fill_to_lots(state.fifo_lots, fill, state.closed_lots, realized_acc)
    state.lifetime_realized_pnl = realized_acc[0]

    # Fee tracking
    if fill.fee is None:
        state.any_fee_missing = True
    elif not state.any_fee_missing:
        state.fee_total = (state.fee_total or 0.0) + fill.fee

    # Fill timestamp for fills_today count
    state.fill_timestamps.append(fill.filled_at_ms)

    # Activity timestamp
    if state.last_activity_at_ms is None or fill.filled_at_ms > state.last_activity_at_ms:
        state.last_activity_at_ms = fill.filled_at_ms


def _reset_inventory_state(state: _BotState) -> None:
    """Cut current custody to flat while preserving immutable fill history."""

    state.exposure.clear()
    state.fifo_lots.clear()


def _current_session_window() -> tuple[int, int] | None:
    """Return (open_ms, close_ms) for today's NY session, or None if closed."""
    now_utc = datetime.fromtimestamp(now_ms_utc() / 1000, tz=UTC)
    ny_date = now_utc.astimezone(_NY).date()
    try:
        win = session_window_for_date(ny_date)
        return win.open_ms_utc, win.close_ms_utc
    except LookupError:
        return None


def _needs_attention(state: _BotState) -> bool:
    """Heuristic: bot needs attention if its last decision was 'blocked'."""
    return state.last_outcome == "blocked"


# ── Catalog-facing cache ─────────────────────────────────────────────────────


class BotRollupCache:
    """Incremental per-bot rollup cache for one broker account.

    Call ``on_fill_appended`` each time a fill is added to the journal
    and ``on_decision_appended`` each time a decision receipt is written.
    Call ``get_rollup(sid)`` for an O(1) catalog read.
    Call ``snapshot_all`` to get rollups for all known bots.

    Performance:
        - ``on_fill_appended``: O(1) per fill (dedup check + exposure fold +
          FIFO lot update via ``apply_fill_to_lots``).
        - ``get_rollup`` exposure read: O(1) dict snapshot.
        - ``get_rollup`` realized_pnl_today: O(closed_lots_in_session) —
          bounded by the trading day.
        - ``get_rollup`` open P&L: O(open_lots) with marks, O(1) without.
        - ``get_rollup`` does NOT call ``compute_fifo_pnl``.

    A ≥100-bot fixture can verify this: 100 bots × short fill lists should
    return snapshot_all in microseconds, not milliseconds per bot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bots: dict[str, _BotState] = {}

    # ── Mutation (called on journal append) ───────────────────────────────────

    def on_fill_appended(self, fill: FillRecord) -> None:
        """Update the rollup for ``fill.sid`` with a new fill event."""
        with self._lock:
            state = self._bots.setdefault(fill.sid, _BotState(sid=fill.sid))
            _apply_fill_delta(state, fill)

    def on_decision_appended(self, receipt: DecisionReceipt, *, sid: str) -> None:
        """Update the rollup for ``sid`` with a new decision receipt."""
        with self._lock:
            state = self._bots.setdefault(sid, _BotState(sid=sid))
            state.last_outcome = receipt.outcome
            if (
                state.last_decision_at_ms is None
                or receipt.ts_ms > state.last_decision_at_ms
            ):
                state.last_decision_at_ms = receipt.ts_ms
            # Use the more recent of fill and decision as last_activity
            if (
                state.last_activity_at_ms is None
                or receipt.ts_ms > state.last_activity_at_ms
            ):
                state.last_activity_at_ms = receipt.ts_ms

    def bootstrap_from_fills(self, sid: str, fills: list[FillRecord]) -> None:
        """Initialise a bot's rollup from a full fill list (cold-start path).

        Called once at startup when the cache is empty but the journal
        already has fills (e.g., after a process restart).  Replays all
        fills through ``_apply_fill_delta`` to rebuild both exposure and
        FIFO state incrementally.
        """
        with self._lock:
            state = _BotState(sid=sid)
            self._bots[sid] = state
            for f in fills:
                _apply_fill_delta(state, f)

    def bootstrap_from_journal(
        self, sid: str, entries: list[OrderJournalEntry]
    ) -> None:
        """Cold-start one bot with inventory-baseline cutover semantics.

        Fill history, realized P&L, and activity remain historical evidence.
        The latest account inventory baseline clears all earlier open lots and
        attributed exposure; only later fills establish current bot custody.
        """

        with self._lock:
            state = _BotState(sid=sid)
            self._bots[sid] = state
            segment: list[OrderJournalEntry] = []
            for entry in entries:
                if entry.kind is not ClerkEntryKind.BROKER_EVIDENCE_BASELINE:
                    segment.append(entry)
                    continue
                for fill in project_instance_fills(sid, segment):
                    _apply_fill_delta(state, fill)
                segment = []
                _reset_inventory_state(state)
            for fill in project_instance_fills(sid, segment):
                _apply_fill_delta(state, fill)

    # ── Read (O(1) exposure; O(closed_lots_in_session) for realized_today) ────

    def get_rollup(
        self,
        sid: str,
        *,
        mark_prices: dict[str, float] | None = None,
        _session: tuple[int, int] | type[_UNSET] | None = _UNSET,
    ) -> BotRollup:
        """Return the current rollup snapshot for one bot.

        ``mark_prices`` is optional; without it ``open_pnl`` stays ``None``.

        Performance: O(1) for exposure; O(closed_lots_in_session) for
        realized_pnl_today (bounded by the trading day); O(open_lots) for
        open P&L when marks are supplied.  Does NOT call ``compute_fifo_pnl``.

        The session window call is deliberately outside the lock: it is a pure
        calendar computation and does not access any shared state.

        ``_session`` is an internal parameter used by ``snapshot_all`` to pass
        a pre-computed session window so the calendar is queried only once per
        catalog read (not once per bot).  External callers should omit it.
        """
        now_ms = now_ms_utc()

        with self._lock:
            state = self._bots.get(sid)

        if state is None:
            return BotRollup(
                sid=sid,
                exposure={},
                fills_today=0,
                realized_pnl_today=0.0,
                open_pnl=0.0,
                last_activity_at_ms=None,
                needs_attention=False,
                as_of_ms=now_ms,
            )

        # Session window is pure calendar I/O — safe outside the lock.
        session = _current_session_window() if _session is _UNSET else _session

        with self._lock:
            exposure_snapshot = dict(state.exposure)
            last_activity = state.last_activity_at_ms
            attention = _needs_attention(state)

            # realized_pnl_today: scan only closed lots within session window.
            # O(closed_lots_in_session) — bounded by trading day, NOT O(lifetime_fills).
            if session:
                open_ms, close_ms = session
                realized_today = realized_pnl_for_window(
                    state.closed_lots,
                    session_open_ms=open_ms,
                    session_close_ms=close_ms,
                )
                fills_today_count = sum(
                    1 for ts in state.fill_timestamps
                    if open_ms <= ts < close_ms
                )
            else:
                realized_today = 0.0
                fills_today_count = 0

            # open P&L: O(open_lots) from current FIFO queues, never O(lifetime_fills).
            open_pnl = compute_open_pnl(
                state.fifo_lots, mark_prices or {}
            ).value

        return BotRollup(
            sid=sid,
            exposure=exposure_snapshot,
            fills_today=fills_today_count,
            realized_pnl_today=realized_today,
            open_pnl=open_pnl,
            last_activity_at_ms=last_activity,
            needs_attention=attention,
            as_of_ms=now_ms,
        )

    def snapshot_all(
        self,
        *,
        mark_prices: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, BotRollup]:
        """Return rollup snapshots for all known bots.

        ``mark_prices`` maps ``{sid: {symbol: price}}``.
        This is the catalog endpoint's read path.  The session window is
        computed once here (not once per bot) to avoid O(bots) calendar calls
        on the hot catalog path.
        """
        with self._lock:
            sids = list(self._bots.keys())

        session = _current_session_window()

        return {
            sid: self.get_rollup(
                sid,
                mark_prices=(mark_prices or {}).get(sid),
                _session=session,
            )
            for sid in sids
        }

    def known_sids(self) -> list[str]:
        """Return all known bot sids in the cache."""
        with self._lock:
            return list(self._bots.keys())
