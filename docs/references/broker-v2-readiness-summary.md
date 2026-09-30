# Broker-v2 readiness summary — provenance note

## What is computed

The broker-v2 panel emits two presentation aggregates over its finite list of
backend-authored readiness checks:

```text
readiness_ready_count = Σ 1[check.ready]
readiness_blocked_count = number_of_checks - readiness_ready_count
```

The counts do not classify gates or infer readiness. Each `check.ready` value
already comes from the SQLite Clerk's recovery catalog. The aggregate is
authored by `sqlite_panel_adapter.py::adapt_sqlite_panel`, which every served
panel passes through; Angular renders the two response fields verbatim.

## Reference and tolerance

This is an exact cardinality projection, not a port from external software.
Its reference is the `readiness_checks` array in the same immutable
`BotPanelView`. Integer comparison is exact: `atol=0, rtol=0`.

## Golden fixture

`PythonDataService/tests/broker/v2panel/test_panel_projection.py::test_served_readiness_counts_partition_the_recovery_checks`
pins a three-check served panel to `2 ready` and `1 blocked`, and to the
emitted check list those two values partition. The Angular
regression in `operator-lens.component.spec.ts` supplies deliberately different
contract totals and verifies they are displayed unchanged, preventing a second
frontend implementation.

## Related P&L display evidence

The trader P&L cards remain display-only. Their numerical authority and
floating-point tolerance are documented separately in
`docs/references/broker-v2-fifo-pnl.md`; the Angular tests cover labels,
positive/negative styling, and the unavailable-mark state without recomputing
P&L.
