"""Cash reservations for accepted ENTERs (ADR 0059 D4, plan R1/R9).

A reservation prices the part of an ENTER the latest cash observation
cannot see: the unfilled remainder of a working order, plus any fill the
Clerk recorded at or after the observation. A filled order reserves
everything not recorded before the observation — its later fills and its
not-yet-recorded ones alike, because a filled order's quantity is known. A
dead order (canceled, expired, rejected, replaced) reserves only its
post-observation fills; its unrecorded remainder is cancelled quantity,
never cash. The shadow book fills at submit while the sweep records the
fill later, so a filled order with no fill row is the common case there,
not a corner.

Corrections fold at their restated size. Each order contributes its
*effective* fills — the head of every correction chain, whatever its
``event_kind`` — at the effective quantity, resolved through the canonical
``EFFECTIVE_FILL_LINEAGE_CTE``. Each one is dated by its ROOT execution's
``recorded_at_ms``, not the correction's own: the broker's cash at that
instant already reflected the true quantity, whenever the Clerk got around to
recording the restatement. So a fill of 10 restated to 5 after the observation
prices the remaining 5 as unfilled, and the mirror upward case prices nothing.

The row is a sibling of the ``ENTER_ACCEPTED`` custody transition, committed
in its transaction but never part of its hashed payload: adding a reservation
must not move a ``custody_transitions.row_hash`` (plan R9).
"""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation

_DEAD_ORDER_STATES = ("canceled", "expired", "rejected", "replaced")


def append_envelope_reservation_row(
    conn: sqlite3.Connection,
    *,
    effect_operation_id: str,
    reservation: EnvelopeReservation,
    reserved_at_ms: int,
) -> None:
    """Insert one reservation. Caller supplies the open transaction."""
    conn.execute(
        "INSERT INTO envelope_reservations "
        "(effect_operation_id, quantity, reference_price, reserved_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (
            effect_operation_id,
            reservation.quantity,
            reservation.reference_price,
            reserved_at_ms,
        ),
    )


def reserved_cash_usd(conn: sqlite3.Connection, *, observed_at_ms: int) -> float:
    """The reserved notional an observation taken at ``observed_at_ms`` misses.

    Every reservation is summed; a dead order with no post-observation fill
    contributes zero on its own arithmetic; a filled order with no recorded
    fill contributes its whole notional. Nothing is pruned on
    ``orders.updated_at_ms``: ``EXECUTION_SLICE_FILLED`` writes a fill without
    touching ``orders``, and the websocket's acknowledgement is skipped when
    the snapshot has not moved, so a dead order's ``updated_at_ms`` can
    sit *before* an observation that has not seen its fills. Only a fill's own
    ``recorded_at_ms`` can say what an observation could have seen — and for a
    corrected execution that is the *root's* ``recorded_at_ms``, which
    ``roots`` supplies.
    """
    # Imported here, not at module scope: ``repository`` imports this module,
    # and ``economic_projection`` imports ``repository``. The canonical CTE
    # still has exactly one definition; only its arrival is deferred.
    from app.broker.alpaca.clerk.sqlite.economic_projection import (
        EFFECTIVE_FILL_LINEAGE_CTE,
    )

    rows = conn.execute(
        f"{EFFECTIVE_FILL_LINEAGE_CTE} "
        "SELECT r.quantity AS quantity, r.reference_price AS reference_price, "
        "LOWER(o.broker_state) AS state, "
        "COALESCE(SUM(CASE WHEN r2.root_recorded_at_ms < ? THEN f.qty ELSE 0 END), 0) "
        "  AS filled_before, "
        "COALESCE(SUM(CASE WHEN r2.root_recorded_at_ms >= ? THEN f.qty ELSE 0 END), 0) "
        "  AS filled_after "
        "FROM envelope_reservations r "
        "JOIN orders o ON o.effect_operation_id = r.effect_operation_id AND o.role = 'ENTRY' "
        "LEFT JOIN fills f ON f.order_ref = o.order_ref "
        "  AND NOT EXISTS (SELECT 1 FROM fills s "
        "                  WHERE s.superseded_execution_ref = f.execution_id) "
        "LEFT JOIN roots r2 ON r2.effective_fill_id = f.fill_id "
        "GROUP BY r.effect_operation_id",
        (observed_at_ms, observed_at_ms),
    ).fetchall()
    total = 0.0
    for row in rows:
        dead = row["state"] in _DEAD_ORDER_STATES
        open_quantity = (
            row["filled_after"] if dead else max(0.0, row["quantity"] - row["filled_before"])
        )
        total += open_quantity * row["reference_price"]
    return total


__all__ = ["append_envelope_reservation_row", "reserved_cash_usd"]
