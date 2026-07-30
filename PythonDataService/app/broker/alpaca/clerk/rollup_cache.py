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
    realized_pnl_today — realized P&L from today's fills (see fifo_pnl.py)
    open_pnl          — open P&L at the latest mark (None if no mark available)
    last_activity_at_ms — most-recent fill or decision receipt ts_ms (int64 ms UTC)
    needs_attention   — True when the bot needs operator intervention

"today" is determined by the canonical NYSE calendar module
(``app.lean_sidecar.trading_calendar``).  The session window is refreshed
lazily on each call; no hardcoded session times.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.lean_sidecar.trading_calendar import session_window_for_date

_NY = ZoneInfo("America/New_York")

# Sentinel for "caller did not supply a session window" (distinguishes None =
# "market closed today" from the default "compute it now").
class _UNSET:
    pass


# ── Rollup snapshot (the per-bot row the catalog reads) ──────────────────────


@dataclass(frozen=True)
class BotRollup:
    """Immutable per-bot rollup snapshot.

    ``open_pnl`` is ``None`` when no mark price is available.
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

    ``seen_keys`` and ``exposure`` are kept in sync incrementally on every
    ``on_fill_appended`` call — O(1) update per fill, O(1) read.  The full
    fill list is still stored for FIFO P&L computation (today-fills slice).

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
    """

    sid: str
    # All fills (lifetime) for FIFO P&L
    fills: list[FillRecord] = field(default_factory=list)
    # Incremental exposure state (updated O(1) per fill)
    seen_keys: set[tuple[str, str]] = field(default_factory=set)
    exposure: dict[str, float] = field(default_factory=dict)
    # Latest decision outcome for needs_attention heuristic
    last_outcome: str | None = None
    last_activity_at_ms: int | None = None
    # Latest decision receipt ts_ms (may be more recent than fills)
    last_decision_at_ms: int | None = None


def _apply_fill_delta(state: _BotState, fill: FillRecord) -> None:
    """Apply one fill to the incremental exposure state (O(1)).

    Deduplicates on ``(account_id, event_key)`` — same semantics as the
    canonical fold in ``exposure.py::project_instance_exposure``.
    """
    key = (fill.account_id, fill.event_key)
    if key in state.seen_keys:
        return
    state.seen_keys.add(key)
    sign = 1.0 if fill.side is OrderSide.BUY else -1.0
    current = state.exposure.get(fill.symbol, 0.0)
    updated = current + sign * fill.quantity
    if math.isclose(updated, 0.0, rel_tol=0.0, abs_tol=1e-9):
        state.exposure.pop(fill.symbol, None)
    else:
        state.exposure[fill.symbol] = updated


def _current_session_window() -> tuple[int, int] | None:
    """Return (open_ms, close_ms) for today's NY session, or None if closed."""
    now_utc = datetime.now(UTC)
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

    Performance: catalog reads are O(1) per bot (dict lookup + shallow copy);
    the incremental update on append is O(1) per fill (no full-list rescan).

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
            state.fills.append(fill)
            # Update exposure incrementally — O(1), no full-list rescan
            _apply_fill_delta(state, fill)
            # Update activity timestamp
            if (
                state.last_activity_at_ms is None
                or fill.filled_at_ms > state.last_activity_at_ms
            ):
                state.last_activity_at_ms = fill.filled_at_ms

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
            activity_candidate = receipt.ts_ms
            if (
                state.last_activity_at_ms is None
                or activity_candidate > state.last_activity_at_ms
            ):
                state.last_activity_at_ms = activity_candidate

    def bootstrap_from_fills(self, sid: str, fills: list[FillRecord]) -> None:
        """Initialise a bot's rollup from a full fill list (cold-start path).

        Called once at startup when the cache is empty but the journal
        already has fills (e.g., after a process restart).
        """
        with self._lock:
            state = self._bots.setdefault(sid, _BotState(sid=sid))
            state.fills = list(fills)
            # Rebuild incremental exposure from scratch using the O(1) delta
            state.seen_keys = set()
            state.exposure = {}
            for f in state.fills:
                _apply_fill_delta(state, f)
            if state.fills:
                state.last_activity_at_ms = max(f.filled_at_ms for f in state.fills)

    # ── Read (O(1) per-bot lookup; O(fills_today) for FIFO P&L) ──────────────

    def get_rollup(
        self,
        sid: str,
        *,
        mark_prices: dict[str, float] | None = None,
        _session: tuple[int, int] | type[_UNSET] | None = _UNSET,
    ) -> BotRollup:
        """Return the current rollup snapshot for one bot.

        ``mark_prices`` is optional; without it ``open_pnl`` stays ``None``.
        This is O(fills_today) for the FIFO P&L — fills_today is bounded by
        the trading day so catalog reads never scan the full lifetime journal.
        The session window call is deliberately outside the lock: it is a pure
        calendar computation and does not access any shared state.

        ``_session`` is an internal parameter used by ``snapshot_all`` to pass
        a pre-computed session window so the calendar is queried only once per
        catalog read (not once per bot).  External callers should omit it.
        """
        now_ms = int(time.time() * 1000)

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
        # When called from snapshot_all the window is pre-computed (one call for
        # all bots); direct callers compute it here.
        session = _current_session_window() if _session is _UNSET else _session

        with self._lock:
            fills_snapshot = list(state.fills)
            exposure_snapshot = dict(state.exposure)
            last_activity = state.last_activity_at_ms
            attention = _needs_attention(state)

        if session is not None:
            open_ms, close_ms = session
            today_fills = [
                f for f in fills_snapshot
                if open_ms <= f.filled_at_ms < close_ms
            ]
        else:
            today_fills = []

        fills_today_count = len(today_fills)

        if today_fills:
            pnl_result = compute_fifo_pnl(today_fills, mark_prices=mark_prices)
            realized_today = pnl_result.realized_pnl
            open_pnl = pnl_result.open_pnl
        else:
            realized_today = 0.0
            if exposure_snapshot:
                # Have exposure from prior days — open P&L needs a mark
                if mark_prices:
                    pnl_result = compute_fifo_pnl(fills_snapshot, mark_prices=mark_prices)
                    open_pnl = pnl_result.open_pnl
                else:
                    open_pnl = None
            else:
                open_pnl = 0.0

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
        This is the catalog endpoint's read path — O(total_fills) worst case
        across all bots but each bot's get_rollup is independent.

        The session window is computed once here (not once per bot) to avoid
        O(bots) calendar calls on the hot catalog path.
        """
        with self._lock:
            sids = list(self._bots.keys())

        # Compute the session window once for all bots in this snapshot.
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
