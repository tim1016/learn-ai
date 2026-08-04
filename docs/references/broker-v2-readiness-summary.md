# Broker-v2 readiness summary — provenance note

## What is computed

The broker-v2 panel emits two presentation aggregates over its finite list of
backend-authored readiness checks:

```text
readiness_ready_count = Σ 1[check.ready]
readiness_blocked_count = number_of_checks - readiness_ready_count
```

The counts do not classify gates or infer readiness. Each `check.ready` value
already comes from the Python-owned presented-action contract. The aggregate is
authored by `panel_projection_service.py::build_panel_view`; Angular renders the
two response fields verbatim.

## Reference and tolerance

This is an exact cardinality projection, not a port from external software.
Its reference is the `readiness_checks` array in the same immutable
`BotPanelView`. Integer comparison is exact: `atol=0, rtol=0`.

## Golden fixture

`PythonDataService/tests/broker/v2panel/test_panel_projection.py::test_panel_composes_cards_rail_and_actions`
pins a representative eight-action panel to `4 ready` and `4 blocked`, then
also proves that both values partition the emitted check list. The Angular
regression in `operator-lens.component.spec.ts` supplies deliberately different
contract totals and verifies they are displayed unchanged, preventing a second
frontend implementation.

## Related P&L display evidence

The trader P&L cards remain display-only. Their numerical authority and
floating-point tolerance are documented separately in
`docs/references/broker-v2-fifo-pnl.md`; the Angular tests cover labels,
positive/negative styling, and the unavailable-mark state without recomputing
P&L.
