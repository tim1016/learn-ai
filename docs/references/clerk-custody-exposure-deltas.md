# Clerk custody exposure deltas

This is an internal custody invariant, not a port from external trading
software. The authority is ADR 0030's account-rooted, journal-canonical custody
model plus the repository's strict-float policy in
`.claude/rules/numerical-rigor.md`.

The canonical fold is
`PythonDataService/app/broker/alpaca/clerk/exposure.py::account_exposure_deltas`:

`delta(symbol) = broker_observed_quantity - clerk_expected_quantity`

A symbol is divergent when `abs(delta) >= 1e-9`, delegated to
`sqlite/folds.py::position_quantity_is_nonzero`. The boundary is an
absolute tolerance with `rtol=0`: position quantities are compared in shares,
so scaling the accepted error with position size would hide real custody drift.
Symbols with an in-flight order are suppressed for one observation because the
broker position can legitimately lead the asynchronous fill callback; a later
snapshot decides once the order becomes terminal.

Validation lives in
`PythonDataService/tests/broker/alpaca/clerk/test_custody_diagnosis.py::test_exposure_delta_golden_cases_pin_aggregation_inflight_and_tolerance`.
The synthetic golden cases pin duplicate-symbol aggregation, in-flight
suppression, values immediately below and above `1e-9`, and exact-boundary
parity with the canonical SQLite predicate.
