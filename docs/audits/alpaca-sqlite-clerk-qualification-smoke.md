# Alpaca SQLite Clerk qualification evidence

- Profile: `smoke`
- Generated at: `1786026222170` ms UTC
- Host: `macOS-26.5.2-arm64-arm-64bit`; Python `3.12.13`
- Broker dependency: none (deterministic fixtures only)
- Performance budget: `PASSED`

| Bots | Transitions | Account p95 ms | Bot p95 ms | Timeline p95 ms | DB bytes | WAL bytes | Mirror bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1000 | 0.148 | 0.142 | 0.117 | 626688 | 0 | 1053790 |

The scale loader is an offline, batched, hash-chained fixture builder; it is not represented as production capture-before-contact throughput. Capture latency is measured separately through real repository registration commits.

## Adversarial campaign

- Status: `PASSED`
- Tests: `35` selected path(s)/node(s)
