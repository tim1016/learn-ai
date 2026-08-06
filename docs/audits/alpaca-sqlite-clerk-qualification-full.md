# Alpaca SQLite Clerk qualification evidence

- Profile: `full`
- Generated at: `1785997783731` ms UTC
- Host: `macOS-26.5.2-arm64-arm-64bit`; Python `3.12.13`
- Broker dependency: none (deterministic fixtures only)
- Performance budget: `PASSED`

| Bots | Transitions | Account p95 ms | Bot p95 ms | Timeline p95 ms | DB bytes | WAL bytes | Mirror bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 10000 | 0.146 | 0.147 | 0.584 | 4067328 | 0 | 10566794 |
| 10 | 100000 | 0.223 | 0.147 | 5.711 | 39055360 | 0 | 105967158 |
| 100 | 1000000 | 0.142 | 0.152 | 51.637 | 390717440 | 0 | 1062670672 |

The scale loader is an offline, batched, hash-chained fixture builder; it is not represented as production capture-before-contact throughput. Capture latency is measured separately through real repository registration commits.

## Adversarial campaign

- Status: `PASSED`
- Tests: `2` selected path(s)/node(s)
