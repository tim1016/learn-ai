# IB-MARK-001

Synthetic books pin the existing IBKR display-mark selection rule, independently
of source/cache filtering. The oracle is the installed ib_async 2.1.0
`Ticker.marketPrice`, `hasBidAsk`, `midpoint`, and `isUnset` implementations;
`output.json` pins their combined source SHA-256. The public reference is
https://ib-api-reloaded.github.io/ib_async/api.html#ib_async.ticker.Ticker.marketPrice.
No application implementation generates expected outputs.

Generated 2026-10-22 with Python 3.12, ib_async 2.1.0:
`python PythonDataService/tests/fixtures/golden/market-data/IB-MARK-001/v1/regenerate.py`.
Inputs are synthetic prices and displayed sizes; null represents an unset value.
The reference's default unset is NaN, normalized to null in the fixture. Zero
and the -1 sentinel retain the reference semantics. There are no timestamps,
fees, calendars or traded capital in this calculation. Tolerance is absolute
1e-9, relative 0: the same float64 addition/multiplication and branch selection,
with no transcendental or iterative operation.
