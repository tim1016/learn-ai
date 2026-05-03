# IBKR integration — Phase 3: paper order placement

**Status**: Phase 3a implemented (place market/limit orders, paper-only).
Phase 3b (cancel + order event SSE stream + bracket/OCO) not yet started.
**Predecessors**: [Phase 1](./ibkr-integration-phase1.md) (option chain + connection safety), [Phase 2](./ibkr-integration-phase2.md) (account + positions + P&L).

## Goal

Move from read-only observation to controlled-write paper trading. Place real orders against the IBKR paper account so backtested strategies can run live (paper) and the engine's `FillModel` can be reconciled against IBKR's actual fills. Live trading remains explicitly out of scope until Phase 4 — the safety surface is built so the live transition is a deliberate flag flip rather than an architectural change.

## Four-layer paper safety

Every order POST runs through all four layers; any one false aborts before `placeOrder`. The layers are independent so a misconfiguration in one cannot accidentally pass through the others.

| # | Layer | Where it lives | What it catches |
|---|---|---|---|
| 1 | `IBKR_MODE=paper` env var | `config.py` (Phase 1) | Operator typed `live` somewhere; Pydantic validation refuses startup. |
| 2 | Port-vs-mode validator | `config.py::IbkrSettings._enforce_port_mode_consistency` (Phase 1) | `IBKR_MODE=paper` but `IBKR_PORT=4001`. Refuses at config time. |
| 3 | `DU` account-id sentinel | `client.py::IbkrClient.connect` (Phase 1) | Gateway login routed us to a non-paper account despite paper-mode env. Hard fail at connect time. |
| 4 | `confirm_paper=true` per request | `orders.py::_enforce_paper_safety` (Phase 3a) | Defense-in-depth: even with all three above true, the POST body must explicitly opt in. |

Phase 4 (live) will add a fifth: `confirm_live=true` symmetrically, with the body fields gated on `IBKR_MODE=live`. The shape stays identical so the live flip is a tiny diff, not a redesign.

## Phase 3a — what's in the box

```
PythonDataService/
├── app/broker/ibkr/
│   ├── orders.py                  ← NEW. place_paper_order, _enforce_paper_safety, _build_contract, _build_order.
│   └── models.py                  ← extended with IbkrOrderSpec, IbkrOrderAck,
│                                    OrderAction/OrderType/OrderTimeInForce/OrderStatus literals.
├── app/routers/broker.py          ← extended with POST /api/broker/orders.
└── tests/broker/ibkr/
    ├── test_orders.py             ← NEW. Each safety layer; market/limit dispatch; option-spec validation.
    └── test_router.py             ← extended with /orders POST 503 + 422 cases.
```

### Endpoint

#### `POST /api/broker/orders`

Body (`IbkrOrderSpec`):

```json
{
  "symbol": "SPY",
  "sec_type": "OPT",
  "action": "BUY",
  "quantity": 1,
  "order_type": "LMT",
  "limit_price": 1.50,
  "time_in_force": "DAY",
  "expiry_ms": 1782259200000,
  "strike": 580.0,
  "right": "C",
  "multiplier": 100,
  "confirm_paper": true
}
```

Response on success (`HTTP 201`, `IbkrOrderAck`):

```json
{
  "account_id": "DU1234567",
  "is_paper": true,
  "order_id": 42,
  "perm_id": 99,
  "client_id": 1,
  "con_id": 700001,
  "symbol": "SPY",
  "action": "BUY",
  "quantity": 1.0,
  "order_type": "LMT",
  "limit_price": 1.50,
  "status": "PendingSubmit",
  "placed_at_ms": 1761234568145
}
```

Failure modes:

| HTTP | When |
|---|---|
| `403` | Any safety layer refused (mode, port, account, confirm flag, OPT missing fields). |
| `422` | Pydantic validation failed (e.g. `confirm_paper` missing, `quantity <= 0`). |
| `502` | Broker call failed (contract qualification, order rejected). |
| `503` | Client not connected. |

### Order-type coverage

Phase 3a:
- `MKT` — market order. `time_in_force` defaults to `DAY`.
- `LMT` — limit order. `limit_price` required.
- `time_in_force` ∈ {`DAY`, `GTC`, `IOC`, `OPG`}.
- `sec_type` ∈ {`STK`, `OPT`}.

Deferred (Phase 3b+):
- Brackets, OCO, trailing stops.
- Market-on-close, market-on-open as their dedicated types.
- IB algos (TWAP / VWAP / Adaptive).
- Cancel (`DELETE /api/broker/orders/{order_id}`).
- Order-event SSE stream.
- Multi-leg combos (spreads).

## Reconciliation play

Each fill produced by the paper account becomes an entry on the **engine's expected fills** vs **IBKR-reported fills** ledger. The engine's `FillModel` (`PythonDataService/app/engine/execution/fill_model.py`) predicts fill price + commission per order; IBKR's actual fill (delivered as a `commissionReport` event in Phase 3b) is the ground truth. Persistent disagreements decompose into:

| Disagreement | Likely cause | Fix |
|---|---|---|
| Predicted fill price ≠ actual | Slippage model wrong | tune `slippage_per_share` or port LEAN's `ImmediateFillModel` |
| Predicted commission ≠ actual | Commission constant wrong | port LEAN's `InteractiveBrokersFeeModel` (tracked in `docs/lean-engine-phase2-plan.md` § 3.2) |
| Engine signal didn't fire but IBKR sent a stale order | Idempotency bug | add `client_order_id` upstream so duplicate POSTs are no-ops |

Phase 3b's commission-report wiring is what closes this loop end-to-end.

## What's NOT in Phase 3a

- **No cancel endpoint.** If you need to bail mid-test, cancel from TWS or IB Gateway directly. `DELETE /api/broker/orders/{order_id}` lands in 3b.
- **No fill stream.** Status updates after the synchronous ack don't surface through this app yet — TWS / Gateway shows them. Phase 3b adds the SSE stream.
- **No idempotency.** Submit the same POST twice and you get two orders. A `clientOrderId` UUID and a server-side dedupe table is a Phase 3b ticket.
- **No risk pre-checks.** No "this order would violate margin," no "this trades against an open position." The IBKR gateway itself rejects truly invalid orders, but bad-but-allowed orders go through. This is a paper account; the cost of a bad order is zero. Live mode (Phase 4) will add risk gates *before* the safety layers reach `placeOrder`.

## Tests

```
podman exec polygon-data-service python -m pytest tests/broker/ibkr/test_orders.py tests/broker/ibkr/test_router.py -v
```

Covered:
- Each of the four safety layers refuses with a distinct error message.
- Happy path: market and limit orders dispatch with the right contract + order objects, ack carries broker-assigned `orderId`.
- Option spec validation (missing expiry/strike/right).
- Limit-without-price refused.
- Router-level 503 (disconnected) and 422 (missing `confirm_paper` field).

ib_async types are stubbed; `placeOrder` is a `MagicMock` so the test asserts what would have been submitted to IBKR without round-tripping a wire.

## Open questions (deferred to Phase 3b)

1. **Cancel semantics**: cancel by `orderId` (broker's), `permId` (durable across reconnect), or our own `clientOrderId`? Safest is `permId` once IBKR assigns one, falling back to `orderId` for orders that haven't been ack'd yet.
2. **Fill stream architecture**: one SSE per order, or one global SSE with order-id demultiplexing? Likely the latter — cheaper, fewer subscriptions.
3. **Reconciliation persistence**: every Phase 3 fill writes to a Parquet alongside the engine's expected-fill log so `docs/references/reconciliations/` can build per-day diff reports.
4. **Bracket / OCO**: same `IbkrOrderSpec` extended with optional `take_profit` / `stop_loss` legs, or a different model? Trade-off between API simplicity and IBKR's actual bracket semantics.
