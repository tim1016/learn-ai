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
not duplicates: they operate on different data stores (Alpaca order journal vs.
Postgres) with different consumers (bot-panel P&L vs. portfolio-engine lot
accounting) and are not expected to produce the same numbers (different scope,
different fill sources).

## Tolerance used and why

`atol=1e-9, rtol=0` (strict float, the repository default).

Justification: fill prices are broker-reported values with at most 2 decimal
places.  All arithmetic is addition and multiplication over `float64` values
that are small in magnitude (price × quantity).  Accumulation error is well
below `1e-9` for realistic fill counts and sizes.  This is tighter than the
`atol=1e-6` accumulated-PnL default because the reference is exact arithmetic,
not a floating-point ported from a reference engine.

## Test file

`PythonDataService/tests/broker/alpaca/clerk/test_fifo_pnl.py`

17 test cases covering: simple round-trip, partial close, multi-lot FIFO,
reversal, multi-day, fee=None propagation, fee partial-None, fee sum, empty
fills, single open fill (no mark / with mark), short open lot open P&L,
realized_pnl_today session filter, multi-symbol, and duplicate event_key
idempotency.

## Golden fixture location

`PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md`

The test cases use hand-computed inline reference values rather than a
serialized parquet fixture.  Justification: the formula is exact arithmetic
(no floating-point port); the reference values in the tests are verifiable
by hand from the input fills, making them self-documenting and auditable
without external files.
