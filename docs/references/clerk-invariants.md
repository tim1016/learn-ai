# Clerk custody invariants (Alpaca SQLite spine)

These are internal custody invariants, not ports from external trading
software. The decisions and their reasons are ADR 0030 (an EXIT reduces the
final instance-attributed quantity exactly), ADR 0036 Decision 1 (the one
flatness boundary) and ADR 0036's 2026-09-30 amendment (fill-quantity
tolerance, delta pricing, the one-cent price conflict). This note keeps the
tests that pin each rule and the coverage-set tolerance.

## 1. EXIT reducing-order quantity

The decision is ADR 0030; the flat boundary it must reach is ADR 0036
Decision 1.

**Validation.**
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py` covers
sibling ENTRY capture, refresh-before-sizing, partial fills,
deterministic reducing identity, retry behavior, and exact
attributed-flat proof.

## 2. Fill-quantity tolerance and delta pricing

The decisions are ADR 0036's 2026-09-30 amendment, items 1-3.

### Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_economic_projection.py`:

- `test_a_same_quantity_price_restatement_records_an_economic_conflict`
  pins the Codex #2428 reproduction (exact 10 @ 100, agreeing total, then a
  10 @ 90 restatement): coverage reads `incomplete`, the recorded economics
  are unchanged, and the matching and quantity-drift controls stay clean.
- `test_refolding_the_same_conflicting_total_records_one_conflict`,
  `test_a_later_agreeing_total_clears_the_conflict`,
  `test_a_vendor_rounding_sized_difference_raises_no_conflict`, and
  `test_the_price_conflict_never_blocks_a_reduction` pin the remaining
  invariants.
- `test_an_older_agreeing_total_does_not_clear_a_newer_conflict`,
  `test_a_manual_order_price_conflict_is_scoped_to_its_custody_subject`, and
  `test_a_refreshed_price_conflict_survives_a_sweep_holding_the_old_identity`
  pin the scoping rules stated in `order_evidence.py`.

`PythonDataService/tests/broker/alpaca/clerk/test_trade_evidence.py`:

- `test_a_websocket_aggregate_clears_a_price_conflict_opened_by_a_snapshot`
  pins that the websocket aggregate route folds the price conflict: a
  snapshot-opened conflict clears when a later frame's exact slice advances
  the fills and its aggregate agrees.

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_enter.py`:

- `test_partial_fill_delta_price_is_the_weighted_average_not_the_cumulative_one`
  pins a two-fill sequence (2 @ $10, then cumulative 5 @ $20) and asserts
  the second delta is priced at `80/3`, not $20.
- `test_fractional_residual_reobservation_does_not_create_a_spurious_fill`
  pins that a re-observation differing only by `4e-13` float residue
  produces no second fill row and does not drift the attributed position.

### Automatic execution-coverage set proof

`PythonDataService/app/broker/alpaca/clerk/sqlite/execution_coverage.py::prove_execution_coverage_set`
is authored project logic, not a port or reuse of an external proof. It is
the canonical predicate consumed by `EXECUTION_COVERAGE_SUPERSEDED` for the
direct one-exact/one-cumulative and accumulated many-to-many replacements.
The shipped S0 one-exact/one-cumulative operator flow remains unchanged
and is an intentionally temporary duplicate.

For the complete cumulative-recovery set `R` and exact set
`E = E_prior ∪ {e_in}`, rows are sorted by immutable `source_id` before
each `math.fsum` calculation:

- `Q_E = Σ qty(E)` and `Q_R = Σ qty(R)` in **shares**;
- `C_E = Σ qty × price(E)` and `C_R = Σ qty × price(R)` in **currency**;
- `P_E = C_E / Q_E` and `P_R = C_R / Q_R` in **currency/share**.

Quantity is strict: `abs(Q_E - Q_R) < QTY_ATOL`, with `QTY_ATOL = 1e-9`
shares and zero relative tolerance. Price is compared at Alpaca's price
precision, never at float precision (ADR 0036, 2026-10-01 amendment, #2791).
With `Q_O` and `C_O` the order's effective fills, `tick` one valid price
increment of `C_O / Q_O` and `max(p)` the highest row price, the proof
requires, in exact decimals,
`abs(C_E - C_R) < COST_ATOL = tick × Q_O + max(p) × abs(Q_E - Q_R)`:
the exacts move the order's average by less than one increment.
Records proven before #2791 carry the float-precision envelope
`max(|Q_E|, |Q_R|) × 1e-9 + max(|P_E|, |P_R|) × 1e-9 + 1e-18`, which the
fold still accepts on replay.
The replacement's `Δposition = Q_E - Q_R` is therefore zero under the
pinned absolute share-tolerance policy; the fold records the aggregates,
tolerance, every cumulative source, and every prior quarantined exact's
original custody clocks before rerunning the proof in its SQLite commit.
It never mutates the position projection.

Fees are excluded from equivalence arithmetic because cumulative recovery
has no fee observation; exact fees remain unchanged on the returned exact
rows. Malformed, unreadable, duplicate, already-effective,
identity-incompatible, overshot, or ambiguous evidence returns a typed
refusal rather than a generic false result. A resolved accumulated episode
deletes all selected cumulative rows, inserts the complete exact set in
source-event/FIFO-sequence order, and retains the incoming exact as the
transition trigger.

The pure proof's independent-equation matrix is
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py`.
Its direct-fold integration is covered by
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_folds_execution.py`
with exact replacement, active-episode refusal, deterministic
stale-revision refusal, partial accumulation, many-to-many replacement,
unreadable/ambiguous episode refusal, mirror rebuild, and exact-redelivery
cases.

## 3. Position-drift tolerance (issues #1378/#1379)

The canonical fold is
`PythonDataService/app/broker/alpaca/clerk/sqlite/reconcile.py::plan_account_reconciliation`.
The flat boundary is ADR 0036 Decision 1.

`delta(symbol) = broker_signed_quantity - clerk_attributed_quantity`

A symbol is flagged `position_drift` when
`position_quantity_is_nonzero(delta)` (`abs(delta) >= POSITION_QTY_EPSILON`)
(`1e-9`, `rtol=0`) **and** the symbol has no non-terminal in-flight order
of ours. A mismatch on a symbol with a working order is not a confirmed
drift, but it is not proven equal either: it is `indeterminate`, and it
fences new exposure until a pass with no in-flight order proves equality
(#1655).

### Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py`:

- `test_plan_flags_position_drift_when_broker_and_attributed_disagree`
  pins a real disagreement above the tolerance.
- `test_plan_marks_indeterminate_for_a_symbol_with_a_non_terminal_in_flight_order`
  pins the in-flight case as indeterminate: neither confirmed drift nor
  clean.
- `test_plan_drift_tolerance_ignores_float_residue_within_epsilon` pins a
  `4e-13` residue as `clean`, not `position_drift`.
- `test_plan_drift_uses_canonical_exact_epsilon_boundary` pins exact
  `1e-9` as drift, matching the shared predicate.
- `test_uncertainty.py::test_new_exposure_uses_the_canonical_attributed_quantity_boundary_fixture`
  pins quantities below, at, and above `1e-9` for fresh-ENTER admission;
  exactly `1e-9` and every larger residual block new exposure.
