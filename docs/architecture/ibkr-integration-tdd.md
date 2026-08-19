# IBKR integration design rationale — retired actuation

**Status:** Read/evidence rationale active; order-actuation design retired by
issue #1583 on 2026-08-19.
**Current implementation authority:**
[`../ibkr-integration-authority.md`](../ibkr-integration-authority.md)

This record explains the design choices that still govern the surviving IBKR
read integration. It is not authorization to rebuild the former paper-order or
bot-control product.

## Surviving decisions

### Python owns the broker adapter

`PythonDataService/app/broker/ibkr/` remains the curated boundary around
`ib_async`. Broker library types do not cross into Angular or .NET. This keeps
Gateway/API churn inside one Python module family and preserves Python ownership
of any numerical normalization.

### Read and evidence first

The supported integration observes:

- connection and session capability;
- account summary, positions, P&L, orders, executions, and history;
- option contracts, chains, surfaces, quotes, and real-time/historical bars;
- broker API/error evidence and durable account/order projections.

Connection controls may establish or reset the data session. Local history
purges may remove projection data. Neither operation can submit, cancel, or
flatten a broker order.

### Non-transmitting what-if

`POST /api/broker/orders/what-if` remains because IBKR's `whatIfOrderAsync`
returns margin and commission evidence without transmitting an order. It uses
the paper mode, port, account sentinel, and operator opt-in checks. A successful
preview is evidence only; it is never a submit token.

### Timestamp and streaming boundaries

Wire/storage timestamps are `int64` milliseconds UTC. IBKR-local date/time
formats are converted at the adapter boundary. Streaming reads use SSE with
explicit cleanup of broker subscriptions when a consumer disconnects.

### Market-data capacity

IBKR market-data limits are not increased by opening more client IDs. The
adapter reuses/deduplicates same-contract real-time-bar subscriptions, paces new
requests, and treats broker refusal as the final capacity authority. The public
5-second buffer and 1-minute consolidation may share one underlying real-time
bar line.

## Retired decisions

The original Phase 3 submit/cancel design, four-layer per-order confirmation,
process-local submit idempotency, `LiveEngine`/`LivePortfolio`, Account Clerk
write lane, recovery/flatten operations, and offline replay were removed by
#1583. Phase 4 live trading is not gated or pending; it is not part of this
integration.

The deletion is intentional:

- Alpaca Broker V2 is the sole broker-control product;
- a quarantined real adapter or hidden compatibility mutation would still be a
  second control authority;
- retaining historical journal schemas provides audit compatibility without
  retaining executable broker effects.

No new IBKR submit/cancel abstraction should be designed from this record.

## Verification contract

`PythonDataService/tests/contracts/test_ibkr_order_actuation_retirement.py`
pins both sides of the boundary: mutation modules/routes/clients/contracts stay
absent, while account, position, order/history, what-if, capability, evidence,
bars, reconciliation, event, and transaction reads stay present.

The original May 2026 phase narrative, order-type matrix, risk register, and
deployment plan remain available in git history and `docs/archive/`. They are
historical provenance, not current requirements.
