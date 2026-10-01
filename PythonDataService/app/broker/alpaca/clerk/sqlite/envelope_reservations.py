"""Cash reservations for accepted ENTERs (ADR 0059 D4, plan R1).

A reservation prices the part of an ENTER the latest cash observation
cannot see: the unfilled remainder of a working order, plus any fill the
Clerk recorded at or after ``seen_before_ms`` — the instant before which the
observation's cash is trusted to include a fill, which the caller supplies
(``AccountObservation.fills_seen_before_ms``). A filled order reserves
everything not recorded before that instant — its later fills and its
not-yet-recorded ones alike, because a filled order's quantity is known. An
ended order reserves only its fills recorded at or after it; its unrecorded
remainder is cancelled quantity, never cash. An order has ended when the
broker ended it (canceled, expired, rejected, replaced), or when its ENTER
ended before the broker ever knew it: the effect is terminal (refused before
contact, failed outright, or proven absent) and the order has no broker
identity and no fill (``order_projection.ORDER_ENDED_SQL``, the one definition
``budget_projection`` also reads; the same three facts
``order_evidence.order_never_reached_broker`` reads). An ENTER whose outcome
is still unknown keeps its whole claim until it is resolved. The shadow book
fills at submit while the sweep records the fill later, so a filled order
with no fill row is the common case there, not a corner.

Each part prices at what it costs, not at one number (#2442). A recorded
fill the observation cannot see reserves its *actual* cost — fill price ×
quantity plus any reported fee — because that is the cash the broker
already took at the fill's own price. Only quantity no recorded fill names
prices at the reservation's ``reference_price`` (the decision price the
ENTER was admitted against), which is an estimate, never the fill's cost.

Its fee is the provision the entry requirement recorded at admission
(``budgets.entry_requirement``), never a re-quote from the fee model (#2553):
a later rate change cannot move a past claim. While any of the order is
unfilled, the remainder claims the whole recorded provision -- no share is
computed, so no money is rounded here (owner decision 2026-09-29); the claim
ends when the order fills or ends. Filled shares' fees also sit in the
canonical fee attribution, so a partly filled entry claims up to its provision
twice over: conservative headroom of cents, never a shortfall.

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

The row is folded from ``ENTER_ACCEPTED``'s facts (the exact reference price
and the recorded provision), so it replays with the custody chain. A row an
earlier build wrote beside the transition carries a float price and no
recorded provision (#2553). Its fills still price as above, but while it has
an unfilled remainder its fee is unknown: reading that claim refuses with
``EntryFeeProvisionUnrecorded`` rather than pricing the fee at zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import ENTRY_FEE_PROVISION_UNRECORDED, EnvelopeReservation
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.order_projection import ORDER_ENDED_SQL


class EntryFeeProvisionUnrecorded(BudgetUnavailable):
    """An open entry's reservation recorded no fee provision, so its fee is unknown (#2553)."""

    reason_code = ENTRY_FEE_PROVISION_UNRECORDED


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
    """Disjoint claim components; the bot budget uses only the unfilled cost and fee.

    ``unfilled_fee`` is the one read of the remainder's fee: the whole
    recorded provision while any of the order is unfilled, else zero. It
    refuses instead of answering zero when a remainder is open on a
    reservation that recorded no provision (``_fee`` is then ``None``).
    """

    strategy_instance_id: str
    order_ref: str
    unfilled_cost: Decimal
    unseen_fill_cost: Decimal
    _fee: Decimal | None

    @property
    def unfilled_fee(self) -> Decimal:
        if self._fee is None:
            raise EntryFeeProvisionUnrecorded(
                "An earlier entry order has no recorded fee estimate and can still fill, so the "
                "cash it claims is unknown. It clears once that order fills or is cancelled at "
                "Alpaca; then choose Reconcile now."
            )
        return self._fee


def entry_cash_claims(conn: sqlite3.Connection, *, seen_before_ms: int) -> tuple[EntryCashClaim, ...]:
    """The reserved notional a cash figure seeing only fills recorded before ``seen_before_ms`` misses.

    A fill recorded strictly before ``seen_before_ms`` counts as seen; one
    recorded at it or later stays reserved. Every reservation is summed; an
    ended order reserves only its unseen recorded fills, each at that fill's
    actual cost (price × quantity plus reported fee); a live order reserves
    its unseen recorded fills at their actual cost plus its unrecorded
    remainder at the reservation's reference price and the whole
    recorded fee provision. Nothing is pruned on
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
        # A recorded row carries its exact price; an earlier build's float
        # price converts as historical REAL evidence (clerk.money).
        "SELECT r.quantity AS quantity, COALESCE(r.exact_reference_price,r.reference_price) AS reference_price, "
        "r.exact_reference_price IS NOT NULL AS provision_recorded, r.fee_provision_cents, "
        "e.strategy_instance_id, o.order_ref, f.fill_id, f.qty, f.price, f.fee, "
        f"{ORDER_ENDED_SQL} AS ended, "
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
            filled = unseen_cost = ZERO
            for fill in fills:
                if fill["fill_id"] is None:
                    continue
                qty = normalize_money(fill["qty"])
                filled += qty
                if fill["execution_recorded_at_ms"] >= seen_before_ms:
                    fee = ZERO if fill["fee"] is None else normalize_money(fill["fee"])
                    unseen_cost += qty * normalize_money(fill["price"]) + fee
            quantity = normalize_money(first["quantity"])
            remaining_quantity = ZERO if first["ended"] else max(ZERO, quantity - filled)
            fee: Decimal | None = ZERO
            if remaining_quantity > ZERO:
                fee = Decimal(first["fee_provision_cents"]) / 100 if first["provision_recorded"] else None
            claims.append(EntryCashClaim(
                strategy_instance_id=first["strategy_instance_id"], order_ref=order_ref,
                unfilled_cost=remaining_quantity * normalize_money(first["reference_price"]),
                unseen_fill_cost=unseen_cost, _fee=fee,
            ))
    return tuple(claims)


__all__ = [
    "EntryCashClaim",
    "EntryFeeProvisionUnrecorded",
    "append_envelope_reservation_row",
    "entry_cash_claims",
]
