# Alpaca SQLite Clerk qualification evidence

- Profile: `full`
- Generated at: `1786462235899` ms UTC
- Host: `macOS-26.5.2-arm64-arm-64bit`; Python `3.12.13`
- Broker dependency: none (deterministic fixtures only)
- Performance budget: `PASSED`

| Bots | Transitions | Account p95 ms | Bot p95 ms | Timeline p95 ms | DB bytes | WAL bytes | Mirror bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 10000 | 0.155 | 0.137 | 0.660 | 4116480 | 0 | 10567064 |
| 10 | 100000 | 0.132 | 0.141 | 5.018 | 39104512 | 0 | 105969858 |
| 100 | 1000000 | 0.133 | 0.192 | 47.521 | 390799360 | 0 | 1062697672 |

The scale loader is an offline, batched, hash-chained fixture builder; it is not represented as production capture-before-contact throughput. Capture latency is measured separately through real repository registration commits.

## Adversarial campaign

- Status: `PASSED`
- Tests: `2` selected path(s)/node(s)
