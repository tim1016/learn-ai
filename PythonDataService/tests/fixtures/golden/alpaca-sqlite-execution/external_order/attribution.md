# External broker-order containment

## Source

Synthetic reconciliation input authored for PRD #1441, S0.3. The order is
intentionally outside the known bot namespace and must be classified by
`app/engine/live/order_identity.py::classify_ownership` as `OwnershipRung.NONE`.

## Fixture date

2026-08-10.

## Generation and assumptions

`alpaca-console:operator-order-1` deliberately cannot parse into the only
allowed bot namespace, `learn-ai/sqlite-cohort-googl-0810/v1`. The order must
therefore become one `external_orders` observation and one active account hold.
The affected bot begins at verified zero economic state and must end with the
same fills, position, realized P&L, and open P&L. An operator acknowledgement
is an available action; it does not erase the original observation.

This is an S1/S2 acceptance oracle. Any change requires a PRD-reviewed
re-derivation, not a change to make an implementation pass.

## Re-derivation 2026-08-27 — ADR 0048 Decision 2 (issue #1798)

`reason_code` moves from `UNEXPLAINED_ORDER` to `UNEXPLAINED_ORDER_HOLD`.

This is the authorised re-derivation, not a fixture edited to pass. Schema v12
merges `holds` into `uncertainties` and normalises the stored cause to the wire
spelling the panel already published: before v12 the stored value was
`UNEXPLAINED_ORDER` and `broker/v2panel/vocabulary.py` translated it to
`UNEXPLAINED_ORDER_HOLD` on the way out, so **the operator-facing value is
unchanged**. What this fixture captures is the projection below that
translation, which is why it moves and the panel contract does not.

Nothing else in the oracle changes: the same single external observation, the
same single active account-scoped hold, the same fills, position, realized and
open P&L, and the same available acknowledgement action.

## Tolerance

`atol=1e-6, rtol=0` for P&L; counts, identifiers, ownership, and actions are
exact.
