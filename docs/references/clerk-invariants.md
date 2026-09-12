# Clerk custody invariants (Alpaca SQLite spine)

> **Status:** active. Consolidated 2026-09-12 from
> `clerk-exit-reducing-quantity.md`, `clerk-fill-quantity-tolerance.md`,
> and `clerk-position-drift-tolerance.md` (git history retains the
> originals).

These are internal custody invariants, not ports from external trading
software. The authority is the pinned contract
`docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` (ENTER fold
§3d, EXIT acceptance criteria §3/§6, reconciliation acceptance criteria)
plus the repository's strict-float policy in
`.claude/rules/numerical-rigor.md`.

## 1. EXIT reducing-order quantity

The canonical implementation is
`PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py`.
After every captured same-strategy/symbol ENTRY is terminal, the Clerk
refreshes each exact broker identity and reads the current
SQLite-attributed quantity:

`reducing_quantity = abs(final_attributed_quantity)`

The side is `SELL` for a positive attributed quantity and `BUY` for a
negative quantity. The calculation may not use requested ENTRY quantity,
broker account net position, or a pre-cancellation snapshot. A
database-unique `EXIT_REDUCING_ORDER_CREATED` transition records the
symbol, side, and exact quantity before broker submission, making replay
and retry deterministic.

**Acceptance criterion.** An EXIT succeeds only after terminal
reducing-order evidence leaves
`abs(attributed_quantity) < POSITION_QTY_EPSILON`, where
`POSITION_QTY_EPSILON = 1e-9` and `rtol=0`. A terminal partial reduction
that does not meet that criterion fails the EXIT and opens a durable
bot-scoped non-flat fence; it never fabricates flatness or permits new
exposure.

**Validation.**
`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py` covers
sibling ENTRY capture, refresh-before-sizing, partial fills,
deterministic reducing identity, retry behavior, and exact
attributed-flat proof.

## 2. Fill-quantity tolerance and delta pricing

The canonical fold is
`PythonDataService/app/broker/alpaca/clerk/sqlite/folds.py::_fold_order_fill_observed`.

### Delta quantity

Alpaca reports `filled_quantity` as the order's *cumulative* filled
quantity, not a per-execution delta. The fold recovers the delta as:

`delta_qty = cumulative_filled_quantity - SUM(prior recorded fills' qty)`

Quantities are floats (fractional shares are legal), so a repeated
observation of the same cumulative state can differ from the recorded sum
by float64 accumulation residue rather than exactly zero. The gate is
`FILL_QTY_EPSILON = 1e-9` with `rtol=0`: an absolute tolerance, not scaled
to the position size, matching the canonical position boundary in §3 for
the same reason — share quantities are absolute, so a relative tolerance
would hide real drift on small positions. `1e-9` sits several orders of
magnitude above the residue a handful of `SUM`/subtraction operations on
float64 doubles can produce (~1e-12–1e-13 at the quantity magnitudes this
fold handles), so it filters that noise without being large enough to
discard a real fractional fill.

`fill_id` is built from `cumulative_filled_quantity` formatted at the same
fixed precision (`:.9f`) rather than the float's raw `str()` repr, so two
observations of a mathematically-identical cumulative state dedup even if
their underlying float representations differ by residue.

The two constants are coupled by design, not independently tunable: the
formatting precision matches the epsilon's decimal place so a cumulative
quantity at or below `FILL_QTY_EPSILON` (e.g. `4e-10`) formats to the same
string as a literal zero (`"0.000000000"`). This never collides with a
real recorded fill, because the order of operations in
`_fold_order_fill_observed` makes it moot: the `delta_qty < FILL_QTY_EPSILON`
gate below the dedup check means a sub-epsilon cumulative quantity is
never inserted into `fills` in the first place — there is no
zero-quantity row for a later, larger fill's `fill_id` to accidentally
match against. Re-observing a sub-epsilon quantity repeatedly is simply
idempotent (the epsilon gate no-ops every time), not a dedup edge case.

### Delta price

Alpaca's `filled_avg_price` is the volume-weighted average price over the
*whole* order, not the price of the latest delta. Copying it verbatim as
the delta's price is wrong once an order fills in more than one clip at
different prices. The fold instead derives the delta's own price from the
cumulative cost bases:

`delta_price = (cumulative_qty * cumulative_avg_price - prior_qty * prior_avg_price) / delta_qty`

using `SUM(qty * price)` over this order's already-recorded fills for the
prior cost basis (no separate column needed — every previously-recorded
fill row already carries its own qty/price).

### Validation

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

`QTY_ATOL = 1e-9` shares and `PRICE_ATOL = 1e-9` currency/share use zero
relative tolerance. Quantity and VWAP comparisons are strict:
`abs(Q_E - Q_R) < QTY_ATOL` and `abs(P_E - P_R) < PRICE_ATOL`.
Gross-cost comparison is inclusive against the propagated envelope:
`COST_ATOL = max(|Q_E|, |Q_R|) × PRICE_ATOL + max(|P_E|, |P_R|) × QTY_ATOL
+ QTY_ATOL × PRICE_ATOL`, requiring `abs(C_E - C_R) <= COST_ATOL`.
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

`delta(symbol) = broker_signed_quantity - clerk_attributed_quantity`

A symbol is flagged `position_drift` when
`position_quantity_is_nonzero(delta)` (`abs(delta) >= POSITION_QTY_EPSILON`)
(`1e-9`, `rtol=0`) **and** the symbol has no non-terminal in-flight order
of ours — a working order legitimately explains a temporary mismatch (the
fill hasn't landed/folded yet), so it is suppressed for that pass rather
than flagged as drift.

This re-stated the proven pre-SQLite exposure policy on the SQLite
`positions` fold rather than inventing another threshold. ADR 0037 /
#1618 completed that migration: the JSONL `exposure.py` implementation
(whose provenance note — `account_exposure_deltas` with the same formula,
absolute `1e-9`/`rtol=0` boundary, and in-flight suppression — was
previously tracked in a separate retired-provenance doc) is deleted and
`sqlite/folds.py::position_quantity_is_nonzero` is now the sole Alpaca
exposure/flat boundary. The former migration parity test retired with the
legacy projection; the SQLite boundary and reconciliation cases below
remain the direct proof.

An absolute tolerance, not relative: share quantities are compared
directly, so scaling the accepted error with position size would hide real
drift on small positions — same reasoning as §2's `FILL_QTY_EPSILON`.

### Reuse (#1379)

All SQLite custody paths call the canonical
`folds.py::position_quantity_is_nonzero` predicate when deciding whether a
position is exposure. That includes
`sqlite/uncertainty.py::_has_attributed_exposure`, which fences fresh
ENTER admission after a repaired or legacy attributed-position projection.
It defines `abs(qty) >= epsilon` as nonzero, so exactly `1e-9` is never
classified as both flat and nonzero by different **SQLite custody**
workflows. Within that SQLite scope, residual drift and exposure/flat
decisions use the same inclusive boundary, so exactly `1e-9` cannot be
accepted as flat by one SQLite custody path and nonzero by another.
ADR 0036 extends that target beyond SQLite by requiring every other
exposure/flat workflow to call the same predicate.

### Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py`:

- `test_plan_flags_position_drift_when_broker_and_attributed_disagree`
  pins a real disagreement above the tolerance.
- `test_plan_suppresses_drift_for_a_symbol_with_a_non_terminal_in_flight_order`
  pins the in-flight suppression.
- `test_plan_drift_tolerance_ignores_float_residue_within_epsilon` pins a
  `4e-13` residue as `clean`, not `position_drift`.
- `test_plan_drift_uses_canonical_exact_epsilon_boundary` pins exact
  `1e-9` as drift, matching the shared predicate.
- `test_new_exposure_uses_the_canonical_attributed_quantity_boundary_fixture`
  pins quantities below, at, and above `1e-9` for fresh-ENTER admission;
  exactly `1e-9` and every larger residual block new exposure.
