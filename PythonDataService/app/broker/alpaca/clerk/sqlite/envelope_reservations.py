"""Cash reservations for accepted ENTERs (ADR 0059 D4, plan R1/R9).

A reservation prices the part of an ENTER the latest cash observation
cannot see: the unfilled remainder of a working order, plus any fill the
Clerk recorded at or after ``seen_before_ms`` — the instant before which the
observation's cash is trusted to include a fill, which the caller supplies
(``AccountObservation.fills_seen_before_ms``). A filled order reserves
everything not recorded before that instant — its later fills and its
not-yet-recorded ones alike, because a filled order's quantity is known. A
dead order (canceled, expired, rejected, replaced) reserves only its fills
recorded at or after it; its unrecorded remainder is cancelled quantity,
never cash. The shadow book fills at submit while the sweep records the
fill later, so a filled order with no fill row is the common case there,
not a corner.

Each part prices at what it costs, not at one number (#2442). A recorded
fill the observation cannot see reserves its *actual* cost — fill price ×
quantity plus any reported fee — because that is the cash the broker
already took at the fill's own price. Only quantity no recorded fill names
prices at the reservation's ``reference_price`` (the decision price the
ENTER was admitted against), which is an estimate, never the fill's cost.

Corrections fold at their restated size. Each order contributes its
*effective* fills — the head of every correction chain, whatever its
``event_kind`` — at the effective quantity, resolved through the canonical
``EFFECTIVE_FILL_LINEAGE_CTE``. Each one is dated by its ROOT execution's
``recorded_at_ms``, not the correction's own: the broker's cash at that
instant already reflected the true quantity, whenever the Clerk got around to
recording the restatement. So a fill of 10 restated to 5 after the observation
prices the remaining 5 as unfilled, and the mirror upward case prices nothing.
Any fill row with no successor counts — including a cumulative-recovery row,
which is its own root and genuinely filled quantity, so its own
``recorded_at_ms`` dates it when the lineage walk supplies no root.

The row is a sibling of the ``ENTER_ACCEPTED`` custody transition, committed
in its transaction but never part of its hashed payload: adding a reservation
must not move a ``custody_transitions.row_hash`` (plan R9).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from app.broker.alpaca.clerk.budgets import entry_requirement
from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money

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
        "(effect_operation_id, quantity, reference_price, reserved_at_ms, exact_reference_price, fee_provision_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            effect_operation_id,
            reservation.quantity,
            reservation.reference_price,
            reserved_at_ms,
            reservation.exact_reference_price,
            reservation.fee_provision_cents,
        ),
    )


@dataclass(frozen=True)
class EntryCashClaim:
    """Disjoint claim components; the bot budget uses only unfilled cost."""

    strategy_instance_id: str
    order_ref: str
    unfilled_cost: Decimal
    unseen_fill_cost: Decimal
    unseen_reported_fees: Decimal
    unfilled_fee: Decimal = ZERO


def entry_cash_claims(conn: sqlite3.Connection, *, seen_before_ms: int) -> tuple[EntryCashClaim, ...]:
    """The reserved notional a cash figure seeing only fills recorded before ``seen_before_ms`` misses.

    A fill recorded strictly before ``seen_before_ms`` counts as seen; one
    recorded at it or later stays reserved. Every reservation is summed; a
    dead order reserves only its unseen recorded fills, each at that fill's
    actual cost (price × quantity plus reported fee); a live order reserves
    its unseen recorded fills at their actual cost plus its unrecorded
    remainder at the reservation's reference price. Nothing is pruned on
    ``orders.updated_at_ms``: ``EXECUTION_SLICE_FILLED`` writes a fill
    without touching ``orders``, and the websocket's acknowledgement is
    skipped when the snapshot has not moved, so a dead order's
    ``updated_at_ms`` can sit *before* an observation that has not seen its
    fills. Only a fill's own ``recorded_at_ms`` can say what an observation
    could have seen — and for a corrected execution that is the *root's*
    ``recorded_at_ms``, which ``roots`` supplies.
    """
    # Imported here, not at module scope: ``repository`` imports this module,
    # and ``economic_projection`` imports ``repository``. The canonical CTE
    # still has exactly one definition; only its arrival is deferred.
    from app.broker.alpaca.clerk.sqlite.economic_projection import (
        EFFECTIVE_FILL_LINEAGE_CTE,
    )

    rows = conn.execute(
        f"{EFFECTIVE_FILL_LINEAGE_CTE} "
        "SELECT r.quantity AS quantity, COALESCE(r.exact_reference_price,r.reference_price) AS reference_price, "
        "r.fee_provision_cents, r.reserved_at_ms, "
        "e.strategy_instance_id, o.order_ref, f.fill_id, f.qty, f.price, f.fee, "
        "LOWER(o.broker_state) AS state, "
        "COALESCE(r2.root_recorded_at_ms, f.recorded_at_ms) AS execution_recorded_at_ms "
        "FROM envelope_reservations r "
        "JOIN orders o ON o.effect_operation_id = r.effect_operation_id AND o.role = 'ENTRY' "
        "JOIN effect_operations e ON e.effect_operation_id = r.effect_operation_id "
        "LEFT JOIN fills f ON f.order_ref = o.order_ref "
        "  AND NOT EXISTS (SELECT 1 FROM fills s "
        "                  WHERE s.superseded_execution_ref = f.execution_id) "
        "LEFT JOIN roots r2 ON r2.effective_fill_id = f.fill_id "
        "ORDER BY o.order_ref, f.fill_id",
    ).fetchall()
    by_order: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_order.setdefault(row["order_ref"], []).append(row)
    claims: list[EntryCashClaim] = []
    with money_context():
        for order_ref, fills in by_order.items():
            first = fills[0]
            filled = unseen_cost = unseen_fees = ZERO
            for fill in fills:
                if fill["fill_id"] is None:
                    continue
                qty = normalize_money(fill["qty"])
                filled += qty
                if fill["execution_recorded_at_ms"] >= seen_before_ms:
                    fee = ZERO if fill["fee"] is None else normalize_money(fill["fee"])
                    unseen_cost += qty * normalize_money(fill["price"]) + fee
                    unseen_fees += fee
            remaining_quantity = (
                ZERO if first["state"] in _DEAD_ORDER_STATES else
                max(ZERO, normalize_money(first["quantity"]) - filled)
            )
            unfilled = remaining_quantity * normalize_money(first["reference_price"])
            fee = ZERO
            if remaining_quantity > ZERO and first["fee_provision_cents"]:
                # Filled shares already belong to the canonical fee projection.
                # Quote only the remainder, using the original reference/date.
                # Its own prospective settlement rounds upward, deliberately
                # retaining conservative per-order rounding headroom.
                _, fee_cents = entry_requirement(
                    quantity=remaining_quantity, price=first["reference_price"], at_ms=first["reserved_at_ms"],
                )
                fee = Decimal(fee_cents) / 100
            claims.append(EntryCashClaim(
                strategy_instance_id=first["strategy_instance_id"], order_ref=order_ref,
                unfilled_cost=unfilled, unseen_fill_cost=unseen_cost,
                unseen_reported_fees=unseen_fees,
                unfilled_fee=fee,
            ))
    return tuple(claims)


def reserved_cash_decimal(conn: sqlite3.Connection, *, seen_before_ms: int) -> Decimal:
    """Exact sum of disjoint order claims; no SQL REAL multiplication.

    Formula: sum(unfilled quantity * reference + unseen fill quantity * actual + fee).
    Reference: PRD #2540 money contract; #2441/#2442 observation overlap policy.
    Canonical implementation: this module and clerk.money normalization.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_envelope_reservations.py.
    """
    with money_context():
        return sum((claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee for claim in
                    entry_cash_claims(conn, seen_before_ms=seen_before_ms)), ZERO)


def reserved_cash_usd(conn: sqlite3.Connection, *, seen_before_ms: int) -> float:
    """Compatibility display view; admission uses reserved_cash_decimal."""
    return float(reserved_cash_decimal(conn, seen_before_ms=seen_before_ms))


__all__ = ["append_envelope_reservation_row", "entry_cash_claims", "reserved_cash_decimal", "reserved_cash_usd"]
