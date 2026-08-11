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

## Tolerance

`atol=1e-6, rtol=0` for P&L; counts, identifiers, ownership, and actions are
exact.
