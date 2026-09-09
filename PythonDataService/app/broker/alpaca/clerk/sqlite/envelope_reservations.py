"""Cash reservations for accepted ENTERs (ADR 0059 D4, plan R1/R9).

A reservation prices the part of an ENTER the latest cash observation
cannot see: the unfilled remainder of a working order, plus any fill the
Clerk recorded at or after the observation. A terminal order reserves only
its post-observation fills. Corrections (``event_kind='correction'``) are
ignored on purpose — they restate price or quantity of an execution that is
already counted, and over-reserving is the safe direction.

The row is a sibling of the ``ENTER_ACCEPTED`` custody transition, committed
in its transaction but never part of its hashed payload: adding a reservation
must not move a ``custody_transitions.row_hash`` (plan R9).
"""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation

_TERMINAL_ORDER_STATES = ("filled", "canceled", "expired", "rejected", "replaced")


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

    A terminal order whose last update the observation already saw reserves
    nothing, so it is filtered out in SQL rather than summed to zero.
    """
    rows = conn.execute(
        "SELECT r.quantity, r.reference_price, o.order_ref, LOWER(o.broker_state) AS state "
        "FROM envelope_reservations r "
        "JOIN orders o ON o.effect_operation_id = r.effect_operation_id AND o.role = 'ENTRY' "
        "WHERE o.broker_state IS NULL OR LOWER(o.broker_state) NOT IN (?, ?, ?, ?, ?) "
        "   OR o.updated_at_ms >= ?",
        (*_TERMINAL_ORDER_STATES, observed_at_ms),
    ).fetchall()
    total = 0.0
    for row in rows:
        filled_before = 0.0
        filled_after = 0.0
        for fill in conn.execute(
            "SELECT qty, recorded_at_ms FROM fills "
            "WHERE order_ref = ? AND event_kind = 'fill' AND is_correction = 0",
            (row["order_ref"],),
        ):
            if fill["recorded_at_ms"] < observed_at_ms:
                filled_before += fill["qty"]
            else:
                filled_after += fill["qty"]
        dead = row["state"] in _TERMINAL_ORDER_STATES
        open_quantity = filled_after if dead else max(0.0, row["quantity"] - filled_before)
        total += open_quantity * row["reference_price"]
    return total


__all__ = ["append_envelope_reservation_row", "reserved_cash_usd"]
