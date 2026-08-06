# Clerk EXIT reducing-order quantity

This is an internal custody calculation, not a port from external software.
Its authority is the cancel-first EXIT acceptance criterion in
`docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` §3/§6 and the
repository numerical-claim guideline in `.claude/rules/numerical-rigor.md`.

## Canonical calculation

`PythonDataService/app/broker/alpaca/clerk/sqlite/exit_resolution.py` is the
canonical implementation. After every captured same-strategy/symbol ENTRY is
terminal, the Clerk refreshes each exact broker identity and reads the current
SQLite-attributed quantity:

`reducing_quantity = abs(final_attributed_quantity)`

The side is `SELL` for a positive attributed quantity and `BUY` for a negative
quantity. The calculation may not use requested ENTRY quantity, broker account
net position, or a pre-cancellation snapshot. A database-unique
`EXIT_REDUCING_ORDER_CREATED` transition records the symbol, side, and exact
quantity before broker submission, making replay and retry deterministic.

## Acceptance criterion

An EXIT succeeds only after terminal reducing-order evidence leaves
`abs(attributed_quantity) < POSITION_QTY_EPSILON`, where
`POSITION_QTY_EPSILON = 1e-9` and `rtol=0`. A terminal partial reduction that
does not meet that criterion fails the EXIT and opens a durable bot-scoped
non-flat fence; it never fabricates flatness or permits new exposure.

## Validation

`PythonDataService/tests/broker/alpaca/clerk/sqlite/test_exit.py` covers sibling
ENTRY capture, refresh-before-sizing, partial fills, deterministic reducing
identity, retry behavior, and exact attributed-flat proof.
