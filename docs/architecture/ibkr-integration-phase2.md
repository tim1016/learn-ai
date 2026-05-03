# IBKR integration — Phase 2: account, positions, P&L

**Status**: Phase 2a in progress (account summary + positions, sync). Phase 2b (P&L SSE streams) and 2c (per-position P&L) not yet started.
**Predecessor**: [Phase 1](./ibkr-integration-phase1.md). Same paper-vs-live safety, same tight-coupling-internally / curated-externally pattern, same wire-model conventions.

## Goal

Mirror your TWS Account + Portfolio tabs inside Learn AI, so the engine's expected position/PnL view sits next to IBKR's reality. The Phase 1 reconciliation play extended one layer down: **did the backtester predict the right cash impact, not just the right Greeks?**

Each disagreement decomposes cleanly:
- Engine-predicted **position** vs IBKR-reported position → fill-model error.
- Engine-predicted **mark** vs IBKR-reported market value → Greeks-model error (already covered by Phase 1).
- Engine-predicted **realised P&L** vs IBKR-reported realised P&L → commission/slippage error.

## Defaults locked for Phase 2

These were the open questions at end-of-Phase-1; the answers are baked into the design:

1. **Account history**: persistence is opt-in via a future `BROKER_PERSIST_ACCOUNT` env var, default `false`. Phase 2a does not write any state to disk.
2. **Multi-leg position grouping**: not in this phase. The wire is raw legs; the UI groups visually if it wants. Strategy-ID grouping is a Phase 2.5 follow-up.
3. **Commission reconciliation**: against the existing engine `FillModel` (`PythonDataService/app/engine/execution/fill_model.py`) on first pass. Porting LEAN's `InteractiveBrokersFeeModel` is tracked in `docs/lean-engine-phase2-plan.md` § 3.2 and is independent.

## Phase 2a — account summary + positions (SYNC)

### Files

```
PythonDataService/
├── app/broker/ibkr/
│   ├── account.py           ← NEW. fetch_account_summary, fetch_positions.
│   └── models.py            ← extended with IbkrPosition, IbkrPositionsSnapshot,
│                              SecType. IbkrAccountSummary gained margin and
│                              account-level P&L fields.
├── app/routers/broker.py    ← extended with /account and /positions endpoints.
└── tests/broker/ibkr/
    ├── test_account.py      ← NEW. Tag parsing, position mapping, error skipping.
    └── test_router.py       ← extended with 503 fallbacks for the new endpoints.
```

### Endpoints

#### `GET /api/broker/account`

One round-trip via `reqAccountSummaryAsync`. Returns the full Phase 2 `IbkrAccountSummary`:

```json
{
  "account_id": "DU1234567",
  "is_paper": true,
  "base_currency": "USD",
  "cash_balance": 100000.50,
  "net_liquidation": 100123.45,
  "buying_power": 400000.00,
  "init_margin": 0.0,
  "maint_margin": 0.0,
  "excess_liquidity": 100000.0,
  "equity_with_loan_value": 100123.45,
  "available_funds": 99987.65,
  "day_pnl": null,            // populated by Phase 2b stream
  "unrealized_pnl": 123.45,
  "realized_pnl": 0.0,
  "fetched_at_ms": 1761234567890
}
```

503 if the broker isn't connected. 502 on a broker error mid-fetch.

#### `GET /api/broker/positions`

```json
{
  "account_id": "DU1234567",
  "is_paper": true,
  "fetched_at_ms": 1761234567890,
  "positions": [
    { "symbol": "SPY", "sec_type": "STK", "con_id": 756733, "quantity": 10, "avg_cost": 590.5, "multiplier": 1, "expiry_ms": null, "strike": null, "right": null, ... },
    { "symbol": "SPY", "sec_type": "OPT", "con_id": 700001, "quantity": -2, "avg_cost": 350.0, "multiplier": 100, "expiry_ms": 1782259200000, "strike": 580.0, "right": "C", ... }
  ]
}
```

Stocks and options share the `IbkrPosition` model; option-specific fields are `None` for stocks. Quantity is signed (negative = short). `avg_cost` is per-unit *as IBKR reports it* — per share for stocks, per contract for options. Consumers reconciling against the engine's per-share cost basis must multiply by `multiplier`.

### Data flow

```
                  ┌─────────────────┐
                  │  IB Gateway     │
                  └────────┬────────┘
                           │
                           ▼
              ┌─────────────────────────────┐
              │ app.broker.ibkr.account     │
              │  fetch_account_summary      │ ← reqAccountSummaryAsync
              │  fetch_positions            │ ← reqPositionsAsync
              └────────┬────────────────────┘
                       │ (curated wire: IbkrAccountSummary, IbkrPositionsSnapshot)
                       ▼
             ┌──────────────────────────┐
             │ app.routers.broker       │
             │  GET /account            │
             │  GET /positions          │
             └────────┬─────────────────┘
                      │ HTTP
                      ▼
        (.NET backend → GraphQL → Angular UI)
```

### Reconciliation play (the actual point)

In `PortfolioService` (.NET) or `PortfolioRepository` (Python), per open paper position the engine knows:
- Expected quantity (from cumulative fills)
- Expected avg cost (from FillModel)
- Expected market value (from QuantLib mark on current bid/ask)
- Expected unrealised P&L

Phase 2a lets the Angular page render side-by-side: **Engine vs IBKR**. Each persistent gap classifies into:

| Gap | Likely cause | Where to fix |
|---|---|---|
| Quantity mismatch | Fill that engine missed (timing, partial fill semantic) | `FillModel.fill_at_close` semantics |
| Avg-cost mismatch | Commission missed in FillModel | `commission_per_order` config or LEAN port |
| Market-value mismatch | Greeks-model error | already covered Phase 1 |
| UnrealizedPnL mismatch | Mark + commission combination | both above |

That decomposition is what makes Phase 2 worth building, not the table itself.

## Phase 2b — P&L SSE streams (IMPLEMENTED)

`app/broker/ibkr/pnl.py` — async iterators that wrap ib_async's
in-place-updating PnL objects:

- `stream_account_pnl(client, debounce_seconds=1.0)` — wraps
  `IB.reqPnL(account)`. First yield is the initial snapshot;
  subsequent yields re-read the same PnL object after each debounce.
- `stream_position_pnl(client, con_ids, debounce_seconds=1.0)` —
  wraps `IB.reqPnLSingle(account, '', con_id)` once per requested
  contract. One tick per (contract × debounce window). Caller
  pre-resolves `con_ids` from `GET /api/broker/positions` (Phase 2a).

Both cancel their subscriptions in `finally` so consumer disconnect
doesn't leak server-side streaming lines.

### Endpoints

#### `GET /api/broker/pnl/stream?debounce_ms=1000`

Server-Sent Events. Each `event: pnl` carries an `IbkrPnLTick` with
`con_id=null` and the account's daily/unrealised/realised P&L.

```
event: pnl
data: {"account_id":"DU1234567","con_id":null,"daily_pnl":12.5,"unrealized_pnl":100.0,"realized_pnl":-5.0,"market_value":null,"position":null,"ts_ms":1761234568145}
```

#### `GET /api/broker/pnl/positions/stream?con_ids=700001&con_ids=700002&debounce_ms=1000`

Server-Sent Events. One tick per requested contract per debounce
window. Use the `con_id` field on each tick to demultiplex on the
client.

`IbkrPnLTick` (from `models.py`):

```python
class IbkrPnLTick(BaseModel):
    account_id: str
    con_id: int | None       # None for account-level
    daily_pnl: float | None
    unrealized_pnl: float | None
    realized_pnl: float | None
    market_value: float | None
    position: float | None
    ts_ms: int
```

### Reconciliation play in 2b

The Angular `account-monitor` page (Phase 2a + 2b) joins the static
`/positions` table with the live `/pnl/positions/stream`. Per-row the UI
renders **engine-predicted unrealised P&L** vs **IBKR-streamed
unrealised P&L** at 1 Hz. Persistent gaps decompose the same way as the
Phase 2a static reconciliation:

| Disagreement | Likely cause |
|---|---|
| Mark differs but quantity matches | Greeks-model error (Phase 1) |
| Quantity matches but unrealised drifts cumulatively | Commission missed in `FillModel` |
| Realised P&L disagrees on a closed leg | Slippage model |

## Phase 2c — Persistence (deferred)

Same shape as Phase 1's `ParquetTickWriter`. Behind `BROKER_PERSIST_ACCOUNT=false` default. When flipped on, account snapshots and P&L ticks land as Parquet partitions under `IBKR_PERSIST_DIR`. Schema decision is independent from Phase 2a/2b — captured in `app/broker/ibkr/persistence.py` follow-up notes.

## Tests (Phase 2a)

```
podman exec polygon-data-service python -m pytest tests/broker/ibkr/test_account.py tests/broker/ibkr/test_router.py -v
```

Covered:
- `_coerce_float_or_none` handles strings, empties, marker tokens, non-numeric junk.
- `fetch_account_summary` filters by account ID, accepts `BASE` currency rows, ignores other-currency rows.
- `_ibkr_position_to_model` decodes option contracts (expiry/strike/right/multiplier) and leaves them None for stocks.
- `fetch_positions` skips zero-quantity rows and rows for other accounts; one bad row doesn't drop the snapshot.
- Router 503 fallback for both new endpoints when no client is initialized.

ib_async types are stubbed with `SimpleNamespace`; nothing reaches the wire. Integration tests against a live Gateway live in `tests/integration/broker/` (not yet written).

## Safety (unchanged from Phase 1)

Read-only. `client.require_connected()` at the top of every fetch. Account-ID filtering in both `fetch_account_summary` and `fetch_positions` so a multi-account login can't bleed positions across accounts. The three Phase 1 sentinel layers still gate the connection itself.

## Phase 1 commit included tests/scripts that this builds on

- The `IbkrClient` + sentinel from Phase 1 is the only entry point used here.
- The `OptionRight` literal and `yyyymmdd_to_expiry_ms` helper from Phase 1 are reused.
- The "tight coupling internally, curated externally" rule from Phase 1's design philosophy is unchanged.

## Open follow-ups specific to Phase 2

1. **Multi-account FA support**: `fetch_*` filters to `client.connected_account`; FA users with sub-accounts get just the master.
2. **Real-time position marks**: position fetches don't subscribe to market data, so `market_price`/`market_value` are `None`. Phase 2b's P&L stream will provide these per second; the static endpoint stays bare.
3. **Currency conversion**: Phase 2a is USD-only. Multi-currency FX handling adds rows we currently filter out — surfacing FX P&L is a separate ticket.
