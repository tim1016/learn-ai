# Clerk fill-quantity tolerance and delta pricing

This is an internal custody invariant, not a port from external trading
software. The authority is the pinned contract's ENTER fold (§3d,
`docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`) plus the
repository's strict-float policy in `.claude/rules/numerical-rigor.md`.

The canonical fold is
`PythonDataService/app/broker/alpaca/clerk/sqlite/folds.py::_fold_order_fill_observed`.

## Delta quantity

Alpaca reports `filled_quantity` as the order's *cumulative* filled
quantity, not a per-execution delta. The fold recovers the delta as:

`delta_qty = cumulative_filled_quantity - SUM(prior recorded fills' qty)`

Quantities are floats (fractional shares are legal), so a repeated
observation of the same cumulative state can differ from the recorded sum
by float64 accumulation residue rather than exactly zero. The gate is
`FILL_QTY_EPSILON = 1e-9` with `rtol=0`: an absolute tolerance, not scaled
to the position size, matching the precedent in
`docs/references/clerk-custody-exposure-deltas.md` for the same reason —
share quantities are absolute, so a relative tolerance would hide real
drift on small positions. `1e-9` sits several orders of magnitude above the
residue a handful of `SUM`/subtraction operations on float64 doubles can
produce (~1e-12–1e-13 at the quantity magnitudes this fold handles), so it
filters that noise without being large enough to discard a real fractional
fill.

`fill_id` is built from `cumulative_filled_quantity` formatted at the same
fixed precision (`:.9f`) rather than the float's raw `str()` repr, so two
observations of a mathematically-identical cumulative state dedup even if
their underlying float representations differ by residue.

The two constants are coupled by design, not independently tunable: the
formatting precision matches the epsilon's decimal place so a cumulative
quantity at or below `FILL_QTY_EPSILON` (e.g. `4e-10`) formats to the same
string as a literal zero (`"0.000000000"`). This never collides with a real
recorded fill, because the order of operations in
`_fold_order_fill_observed` makes it moot: the `delta_qty < FILL_QTY_EPSILON`
gate below the dedup check means a sub-epsilon cumulative quantity is never
inserted into `fills` in the first place — there is no zero-quantity row for
a later, larger fill's `fill_id` to accidentally match against. Re-observing
a sub-epsilon quantity repeatedly is simply idempotent (the epsilon gate
no-ops every time), not a dedup edge case.

## Delta price

Alpaca's `filled_avg_price` is the volume-weighted average price over the
*whole* order, not the price of the latest delta. Copying it verbatim as
the delta's price is wrong once an order fills in more than one clip at
different prices. The fold instead derives the delta's own price from the
cumulative cost bases:

`delta_price = (cumulative_qty * cumulative_avg_price - prior_qty * prior_avg_price) / delta_qty`

using `SUM(qty * price)` over this order's already-recorded fills for the
prior cost basis (no separate column needed — every previously-recorded
fill row already carries its own qty/price).

## Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_enter.py`:

- `test_partial_fill_delta_price_is_the_weighted_average_not_the_cumulative_one`
  pins a two-fill sequence (2 @ $10, then cumulative 5 @ $20) and asserts the
  second delta is priced at `80/3`, not $20.
- `test_fractional_residual_reobservation_does_not_create_a_spurious_fill`
  pins that a re-observation differing only by `4e-13` float residue
  produces no second fill row and does not drift the attributed position.

## Automatic execution-coverage set proof

`PythonDataService/app/broker/alpaca/clerk/sqlite/execution_coverage.py::prove_execution_coverage_set`
is authored project logic, not a port or reuse of an external proof. It is the
canonical predicate for a future automatic exact-after-cumulative replacement;
the shipped S0 one-exact/one-cumulative operator flow remains unchanged and is
an intentionally temporary duplicate.

For the complete cumulative-recovery set `R` and exact set
`E = E_prior ∪ {e_in}`, rows are sorted by immutable `source_id` before each
`math.fsum` calculation:

- `Q_E = Σ qty(E)` and `Q_R = Σ qty(R)` in **shares**;
- `C_E = Σ qty × price(E)` and `C_R = Σ qty × price(R)` in **currency**;
- `P_E = C_E / Q_E` and `P_R = C_R / Q_R` in **currency/share**.

`QTY_ATOL = 1e-9` shares and `PRICE_ATOL = 1e-9` currency/share use zero
relative tolerance. Quantity comparison is strict:
`abs(Q_E - Q_R) < QTY_ATOL`. Gross-cost comparison is inclusive:
`abs(C_E - C_R) <= max(Q_E, Q_R) × PRICE_ATOL`. The replacement's
`Δposition = Q_E - Q_R` is therefore zero under that same pinned absolute
share-tolerance policy; the fold that will consume a proven result must not
mutate the position projection.

This deliberately separate cost gate rejects a high-price, sub-share-epsilon
quantity residue when its economic consequence is material. Fees are excluded
from the equivalence arithmetic because cumulative recovery has no fee
observation; exact fees remain unchanged on the returned exact observations.
Malformed, unreadable, duplicate, already-effective, or identity-incompatible
evidence returns a typed refusal rather than a generic false result.

The pure proof's independent-equation matrix is
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_execution_coverage_set_proof.py`.
