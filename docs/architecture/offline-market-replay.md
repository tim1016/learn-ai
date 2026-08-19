# Offline IBKR market replay — retired

**Status:** Retired by issue #1583 on 2026-08-19
**Authority:** `docs/architecture/engine-authority-map.md`

The former offline-hours SPY/TSLA bot replay was coupled to the deprecated IBKR
paper-order runtime. It coordinated two `LiveEngine` instances through a virtual
clock and `ReplaySimBroker`, exposed `/api/offline-replay/*`, and shipped an
Angular replay console.

Issue #1583 removed that complete composition:

- `LiveEngine`, `LiveContext`, `LivePortfolio`, and the replay broker;
- the offline replay data, clock, coordinator, schemas, router, fixtures, and
  tests;
- the generated API contract paths and Angular service/components/types.

No compatibility route or hidden service remains. The retired composition added
no independent indicator, sizing, P&L, commission, or fill authority, so no math
implementation moved. Engine Lab and its research runners remain the supported
backtest/replay authorities.

The structural contract at
`PythonDataService/tests/contracts/test_ibkr_order_actuation_retirement.py`
proves the replay modules and route prefix stay absent while the read-only IBKR
account, position, order/history, evidence, capability, and market-data surfaces
remain registered.

Historical design details remain available in git history before #1583. They are
not a template for new replay or broker-control work.
