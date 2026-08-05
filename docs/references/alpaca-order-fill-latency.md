# Alpaca order submission-to-fill latency — provenance

## Canonical implementation

`PythonDataService/app/broker/alpaca/adapter.py::fill_latency_seconds` is the
only author of `BrokerOrder.fill_latency_seconds`. The .NET transport passes
the scalar through and Angular renders it; neither recomputes broker timing.

## Reference and formula

Alpaca's order schema supplies the `submitted_at` and `filled_at` lifecycle
timestamps used as the two broker clocks. It defines the former as the time an
order was submitted for execution and the latter as the fill time (nullable
until filled): [Alpaca — Trading / order properties](https://docs.alpaca.markets/us/docs/brokerapi-trading).

```text
fill_latency_seconds = (filled_at_ms - submitted_at_ms) / 1,000
```

The value is intentionally nullable: an order without either broker clock has
no verifiable submission-to-fill interval. It is an elapsed duration in
seconds, not an instant, so the repository's `int64 ms UTC` representation
applies to the two input clocks rather than the derived display scalar.

## Millisecond boundary and tolerance

`rfc3339_to_ms` converts each timezone-aware broker timestamp to the nearest
representable UTC millisecond before subtraction. The calculation therefore
preserves the repository's boundary precision and returns a multiple of
0.001 seconds. `test_filled_order_maps_every_field_and_synthesizes_fill_event`
pins the fixture result to `abs=1e-12, rel=0`; that tolerance admits only the
binary floating-point representation of the exact millisecond-derived value,
not a different clock interval.

## Fixture provenance and validation

`PythonDataService/tests/fixtures/alpaca/orders/orders.json` contains the
committed filled SPY market-order payload captured for HITL #1178, plus a
synthetic unfilled limit order. The filled payload's RFC-3339 timestamps map to
`0.772` seconds; the unfilled payload and a missing-clock variant establish the
nullable boundary.

Validated by:

- `PythonDataService/tests/broker/alpaca/test_adapter_orders.py::test_filled_order_maps_every_field_and_synthesizes_fill_event`
- `PythonDataService/tests/broker/alpaca/test_adapter_orders.py::test_fill_latency_is_unknown_until_both_broker_clocks_exist`
