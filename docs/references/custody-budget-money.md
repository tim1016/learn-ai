# Custody budget money

Decision: [PRD #2540](https://github.com/tim1016/learn-ai/issues/2540),
implemented by [#2545](https://github.com/tim1016/learn-ai/issues/2545).
This is platform accounting policy, not an external software port.

The normalization boundary is `app/broker/alpaca/clerk/money.py`. Decimal
strings and cents enter exactly. Legacy finite floats enter through
`Decimal(str(value))`, before multiplication or summation. Precision already
lost by historical SQLite REAL storage or canonical FIFO is not recoverable.
Historical fills, hashes and FIFO matching are unchanged.

Supported input values have magnitude below 10^309 and at most 324 decimal
places. A 1400-digit local context preserves their products and sums (including
the maximum representable SQLite row count); inexact arithmetic raises.
Nonfinite, unsupported and unknown evidence refuses admission. A decimal
context elsewhere in the process cannot change the result.

Consent is a positive whole-cent amount fitting SQLite INTEGER, and never
rounds. Required cents round upward and spendable cents round downward only
at the actionable boundary. Internal affordability compares unrounded exact
amounts with no epsilon. Display conversion cannot authorize spending.

Validation uses independent integer/Fraction receipts in `test_money.py`,
including 0.125 shares at $10.01 = $1.25125, requiring 126 cents; ambient-context
and magnitude/scale cases; and a shortage smaller than the previous epsilon.
Existing order-reservation regressions cover partial fills, terminal orders,
actual execution prices, reported fees, observation grace and correction
lineage (#2441/#2442). Exact decimal results use equality (zero tolerance).
Compatibility float assertions retain the existing tests' tolerance; these
adapters never determine admission.

The deployment projection is `clerk/budgets.py`, composed over canonical
FIFO, effective fills and fee attribution by `sqlite/budget_projection.py`.
An active deployment claims only its positive free balance. Working orders,
unseen fills and unsettled fees retain separate, disjoint claims after Stop.
A historical deficit cannot reserve future deposits. The immutable commitment
and launch/release outcomes replay through the existing custody mirror.

Independent conservation fixtures in `test_budgets.py` cover the $1,000/$600
example, partial fills, observed cash, settlement replacement and historical
deficits. Real repository tests in `sqlite/test_budget_commands.py` prove
concurrent commitment exclusion, response-loss idempotency, startup-failure
release and mirror rebuild retaining stopped order claims. Exact decimal
assertions use zero tolerance. These tests do not claim complete UI delivery.
