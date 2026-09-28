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

Completed manual BUY executions retain their exact corrected debit (including
reported fill fees) until the trusted cash observation passes the original
execution-recording boundary. A terminal effect is not proof that cash already
contains its debit. Broker acknowledgements with missing execution quantity
make fee population and new spending unknown, including tiny fractional-share
gaps; zero-fill cancellations remain complete. The governing acknowledgement
and effective correction lineage are reused, with no admission epsilon.
New order acknowledgements retain every positive reported cumulative quantity
and every changed finite quantity exactly after normalization, including
same-state/time corrections. Existing custody hashes are never rewritten.

External orders keep their current broker state and cumulative filled quantity
in the existing observation transition, read through the external-order
projection. Older facts omit those fields and retain their original bytes;
their cash obligation is unknown until refreshed. Operator acknowledgement is
review evidence, never cancellation or a cash release. A working or unknown
external order refuses new spending until the broker proves its outcome.
A terminal observation also requires an exactly matching retained execution
population, including partially canceled orders; no quantity epsilon applies.
The fee evidence boundary supplies normalized external FILL activities to the
same cash projection, so even a fill that arrives before order reconciliation
retains its unseen BUY debit. Its first observation controls recognition;
duplicate polling cannot renew that claim. External sale proceeds are not
advanced before the cash observation recognizes them. These facts never enter
a bot's FIFO or commitment. `sqlite/test_budget_claims.py` covers those cases,
historical serialization, and mirror rebuild with exact Decimal assertions.

Pending entry fee quotes price only the unfilled remainder at the original
reference price and quote date, through the canonical regulatory model. Filled
shares belong exclusively to the canonical fee attribution. A remaining order
quote rounds its prospective component settlement upward independently, so it
can retain conservative cent headroom relative to a final combined day charge;
it is not an exact forecast of that later settlement. Original zero-provision
legacy reservations are preserved rather than silently rewritten.

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
`sqlite/test_budget_claims.py` additionally proves manual cash overlap,
missing terminal executions, partial-fill fee replacement and same-symbol
interleaved deployment FIFO through correction and Stop.

Budget readiness and actual ENTER use `budgets.py::budget_entry_decision` to
price the immutable next position plus the canonical BUY fee provision, compare
it with its own free budget, and recheck cash against all other claims. Positive
free cash is insufficient by itself: a one-cent remainder cannot fund a $100
position whose exact requirement is $100.01. Read-only panel risk uses
`risk_admission.current_risk_readiness`, the same judgement wrapped by the
commit-time writer. Repeated GETs neither withdraw an observation nor append a
loss hold. The panel labels these budget/account-risk checks explicitly; it does
not promise strategy, session or broker execution permission. Regressions:
`test_budgets.py`, `test_risk_fee_evidence.py`, `v2panel/test_budget_deploy.py`.
