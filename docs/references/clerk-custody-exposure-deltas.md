# Clerk custody exposure deltas (retired JSONL provenance)

This is an internal custody invariant, not a port from external trading
software. The authority is ADR 0030's account-rooted, journal-canonical custody
model plus the repository's strict-float policy in
`.claude/rules/numerical-rigor.md`.

The original JSONL fold was
`PythonDataService/app/broker/alpaca/clerk/exposure.py::account_exposure_deltas`:

`delta(symbol) = broker_observed_quantity - clerk_expected_quantity`

A symbol was divergent when `abs(delta) >= 1e-9`; symbols with an in-flight
order were suppressed for one observation because broker position could lead
the asynchronous fill callback.

ADR 0037 / #1618 deleted that implementation and its product path. The canonical
Alpaca authority is now
`PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py::plan_account_reconciliation`,
which delegates every exposure/flat boundary to
`sqlite/folds.py::position_quantity_is_nonzero`. Its formula, absolute
`1e-9`/`rtol=0` boundary, in-flight suppression, and direct validation are
documented in `docs/references/clerk-position-drift-tolerance.md`. This file is
retained only as provenance for why the SQLite policy did not invent a new
tolerance.
