# broker-v2 FIFO P&L — port note

## What was ported

Canonical FIFO lot-level P&L for the broker-v2 bot control panel (S0, issue #1296).
Lot accounting, realized P&L, open P&L, and fee propagation.

## From where

Standard FIFO inventory method (GAAP / IFRS).  Not a software port — well-known
accounting arithmetic.

**Reference:** Kieso, Weygandt & Warfield, *Intermediate Accounting* (17e), Chapter 8
(Inventories: Measurement).  No external repository; the algorithm is
mathematically specified in the module docstring of `fifo_pnl.py`.

## Canonical implementation

`PythonDataService/app/broker/alpaca/clerk/fifo_pnl.py`

**Distinct from** `Backend/Services/Implementation/PositionEngine.cs`, which accounts
over EF/Postgres lots for the portfolio engine.  The two instances are parallel,
not duplicates: they operate on different data stores (Alpaca SQLite effective
fills vs. Postgres lots) with different consumers (bot-panel P&L vs. portfolio-engine lot
accounting) and are not expected to produce the same numbers (different scope,
different fill sources).

## Tolerance used and why

Exact fields: equality (zero tolerance).  Float display views:
`atol=1e-9, rtol=0` (strict float, the repository default).

Justification: the reference is exact arithmetic, and so is the
implementation (#2550).  Each recorded fill quantity and price is normalized
once through `money.normalize_money`; lots, closures, open valuation and totals
accumulate in `Decimal` under `money.money_context`, where inexact arithmetic
raises.  The `exact_*` fields (`PnLResult.exact_realized_pnl`,
`PnLResult.exact_open_pnl` / `OpenPnLResult.exact_value`,
`ClosedLot.exact_realized_pnl`, `OpenLot.exact_qty` / `exact_cost`) are the
money authority that custody budgets and the simulated (Dry Run and Shadow)
account equity read (#2556).  The float attributes (`qty`, `cost`,
`realized_pnl`, `open_pnl`, `value`) are those values rounded once for
display, so their only error is that single rounding — well inside the
historical `1e-9` fixtures — but it can still cross a whole cent, so money
never normalizes a float view back into `Decimal`.  The lot-closing threshold (a lot at or below `1e-9` shares
closes) is the unchanged FIFO matching rule, now applied to exact quantities.

## Test file

`PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py`

17 test cases covering: simple round-trip, partial close, multi-lot FIFO,
reversal, multi-day, fee=None propagation, fee partial-None, fee sum, empty
fills, single open fill (no mark / with mark), short open lot open P&L,
realized_pnl_today session filter, multi-symbol, and duplicate event_key
idempotency.  `test_multi_lot_partial_closes_match_exact_fraction_oracle`
pins the exact fields (fractional multi-lot partial closes and a reversal)
against an independent `Fraction` oracle and the float views bit-exactly.
`test_open_pnl_is_exact_and_its_float_view_is_rounded_once` does the same for
open valuation (long remainder and short lot, missing mark, flat), and
`test_open_pnl_money_is_exact_at_a_whole_cent_boundary` pins why money reads
the exact field: 0.048360857 shares from $100 to $100.3101682007 gain exactly
$0.0149999999999999999 (1 cent), while the 17-digit float view 0.015 rounds
half-even to 2 cents.  `test_no_money_normalizes_a_float_fifo_view` is the grep
guard that no `normalize_money(...)` in the service takes a float view.

## Golden fixture location

`PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md`

The test cases use hand-computed inline reference values rather than a
serialized parquet fixture.  Justification: the formula is exact arithmetic
(no floating-point port); the reference values in the tests are verifiable
by hand from the input fills, making them self-documenting and auditable
without external files.
