# Clerk position-drift tolerance (SQLite spine, #1378/#1379)

This is an internal custody invariant, not a port from external trading
software. The authority is the pinned contract's reconciliation acceptance
criteria (issue #1378, `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`)
plus the repository's strict-float policy in `.claude/rules/numerical-rigor.md`.

The canonical fold is
`PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py::plan_account_reconciliation`.

## Formula

`delta(symbol) = broker_signed_quantity - clerk_attributed_quantity`

A symbol is flagged `position_drift` when
`position_quantity_is_nonzero(delta)` (`abs(delta) >= POSITION_QTY_EPSILON`)
(`1e-9`, `rtol=0`) **and** the symbol has no non-terminal in-flight order of
ours — a working order legitimately explains a temporary mismatch (the fill
hasn't landed/folded yet), so it is suppressed for that pass rather than
flagged as drift.

This is the same epsilon and the same in-flight-suppression policy as the
pre-SQLite Alpaca clerk's `exposure.py::account_exposure_deltas` (documented in
`docs/references/clerk-custody-exposure-deltas.md`) — a deliberate re-statement
of an already-proven policy on the new SQLite `positions` fold, not an
independent invention. The duplication is a migration artifact, not a
permanent one: `exposure.py`'s JSONL-journal implementation is retired at the
SQLite cutover (#1382), at which point this is the sole implementation.

Both paths now call
`sqlite/folds.py::position_quantity_is_nonzero` for the tolerance boundary.
Parity is pinned by
`test_legacy_exposure_delta_uses_canonical_sqlite_nonzero_boundary`; the JSONL
and SQLite inputs remain different projections, but they can no longer drift on
the shared exposure/flat threshold during migration.

An absolute tolerance, not relative: share quantities are compared directly, so
scaling the accepted error with position size would hide real drift on small
positions — same reasoning as `docs/references/clerk-fill-quantity-tolerance.md`'s
`FILL_QTY_EPSILON`.

## Reuse (#1379)

All SQLite custody paths call the canonical
`folds.py::position_quantity_is_nonzero` predicate when deciding whether a
position is exposure. It defines `abs(qty) >= epsilon` as nonzero, so exactly
`1e-9` is never classified as both flat and nonzero by different workflows.
Residual drift and exposure/flat decisions use the same inclusive boundary, so
exactly `1e-9` cannot be accepted as flat by one authority and nonzero by another.

## Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py`:

- `test_plan_flags_position_drift_when_broker_and_attributed_disagree` pins a
  real disagreement above the tolerance.
- `test_plan_suppresses_drift_for_a_symbol_with_a_non_terminal_in_flight_order`
  pins the in-flight suppression.
- `test_plan_drift_tolerance_ignores_float_residue_within_epsilon` pins a
  `4e-13` residue as `clean`, not `position_drift`.
- `test_plan_drift_uses_canonical_exact_epsilon_boundary` pins exact `1e-9`
  as drift, matching the shared predicate.
