# Technical Design: Interactive Brokers paper-trading integration

**Status**: Phase 1 + 2a + 2b + 2c + 3a + 3b shipped. Phase 4 (live) gated behind explicit operator approval.
**Authors**: Inkant (driver), Claude (research + code), Codex (case-sensitive bug fix).
**Date**: May 2026.
**Scope**: Server-side IBKR integration in `PythonDataService/`. Frontend pointers in §10. Phase docs (§4) are archived frozen snapshots: [phase 1](../archive/plans/ibkr-integration-phase1.md), [phase 2](../archive/plans/ibkr-integration-phase2.md), [phase 3](../archive/plans/ibkr-integration-phase3.md).

## 1. Motivation

The user's uncle — his closest finance mentor — advised paper-trading the strategies he's been backtesting **before** going live, to build physical intuition for how option prices move during market hours. The progression is: paper → live, with rigorous reconciliation between the engine's predictions and IBKR's reality at every layer along the way.

This integration delivers the broker-side scaffolding that makes that intuition possible:

- A live option chain from IBKR rendered side-by-side with the engine's QuantLib / py_vollib Greeks (Phase 1).
- Account, positions, and live P&L surfaced inside the Learn AI app (Phase 2).
- Read-only first; paper order placement only after the read-only surface is stable (Phase 3).
- Live trading deferred until paper-mode reconciliation is clean (Phase 4, not built).

The wider system goal — beyond intuition — is that every IBKR fact becomes a third independent authority alongside the engine's existing Hull / py_vollib / QuantLib reconciliation. Persistent gaps decompose into concrete bugs (slippage model, commission constant, Greeks-model error) rather than vibes.

## 2. Research summary

### 2.1 IBKR API surface

Surveyed four:

| Surface | Pro | Con | Verdict |
|---|---|---|---|
| **TWS API via IB Gateway** | full feature set, well-supported, socket-based | needs Gateway running locally | **chosen** |
| **TWS API via TWS desktop** | same as Gateway | heavier; full GUI | rejected — Gateway is ~40% lighter |
| **Client Portal API (REST)** | no Gateway needed | missing options chain features, sparse docs | rejected |
| **FIX** | exchange-grade | massive overkill | rejected |

IB Gateway runs natively on Windows. The Python service connects to `host:4002` (paper) or `host:4001` (live). Login is required via Gateway UI; post-login it's headless.

### 2.2 Python library

Three candidates evaluated:

| Library | Status | Notes |
|---|---|---|
| **`ib_insync`** | unmaintained since 2024 | Original maintainer Ewald de Wit passed; org locked |
| **`ib_async`** | actively maintained | Direct fork under `ib-api-reloaded` org; same author lineage; `ibapi` not required |
| **`ibapi`** (raw) | official | callback-spaghetti; you reinvent ib_async's reactor |

**Chose `ib_async>=2.0,<3.0`**, pinned tight in `requirements-light.txt` because IBKR wire protocol revs are coupled to Gateway versions. NautilusTrader uses raw `ibapi` for performance reasons; we don't have those constraints, so the cleaner async API wins.

### 2.3 Open-source reference architectures

The `app/broker/ibkr/` file layout is modelled directly on **NautilusTrader's** `adapters/interactive_brokers/` split (client / data / execution). The four-layer paper safety borrows shape from **QuantConnect LEAN's** `IBrokerage` plugin. **MMR** (`9600dev/mmr`) provided the "expose a curated subset over a typed boundary" pattern that we mirror in the FastAPI router. Containerised IBC patterns (e.g. `gnzsnz/ib-gateway-docker`) inform Phase 4 deployment but aren't used yet — Gateway runs natively for now.

### 2.4 Costs

Paper account: free. Real-time market data must be subscribed on the **live** account; paper inherits.

| Subscription | Monthly (NP) | Waiver |
|---|---|---|
| US Securities Snapshot and Futures Value Bundle (prerequisite) | $10.00 | $30/mo commissions |
| US Equity and Options Add-On Streaming Bundle (NBBO + OPRA) | $4.50 | OPRA waived at $20/mo commissions |
| **Total during paper learning phase** | **$14.50/mo** | |

Both auto-waive once live trading commissions accrue. Top-of-book only; no Level 2 (deferred).

## 3. Architectural choices

### 3.1 Where the integration lives

**`PythonDataService/`**, not `Backend/`. Three reasons:

1. The IBKR Python ecosystem (`ib_async`, options-math libraries, pandas) is what the engine already uses.
2. The reconciliation play sits next to QuantLib + py_vollib, which are Python.
3. Phase 1 added one new dependency (`ib_async`); Phase 2/3 added zero. Whole integration ships in the existing light/heavy requirements split.

The .NET backend reaches IBKR exclusively through the Python service's HTTP/SSE endpoints. No `ib_async` types cross the language boundary.

### 3.2 Tight coupling internally, curated externally

The user's stated rule:

> "When we integrate an external API, WE take whatever endpoints they expose and we use them although we might expose only a few of them in the functionality but like to have tight coupling there."

Translated:

- `app/broker/ibkr/*` wraps the **full** `ib_async` surface area we'll plausibly need. The `IbkrClient.ib` accessor exposes the underlying `IB()` instance so future phases can reach for any primitive (e.g. `reqHistoricalData`, `reqContractDetails`) without a new module.
- `app/routers/broker.py` exposes only the **curated** subset to the .NET / Angular layers. Eleven endpoints today; new features cost a router-line, not a new internal abstraction.
- IBKR schema breakage from Gateway upgrades stays inside `app/broker/ibkr/`. The wire layer (Pydantic v2 models) holds the line.

### 3.3 Paper-first with four-layer safety

| # | Layer | Where it lives | What it catches |
|---|---|---|---|
| 1 | `IBKR_MODE=paper` env var | `config.py` (Phase 1) | Operator typed `live` somewhere; Pydantic refuses startup. |
| 2 | Port-vs-mode validator | `config.py::_enforce_port_mode_consistency` | `IBKR_MODE=paper` + `IBKR_PORT=4001`. Refuses at config time. |
| 3 | `DU` account-id sentinel | `client.py::IbkrClient.connect` | Gateway routed to a non-paper account. Hard fail at connect time. |
| 4 | `confirm_paper=true` per request | `orders.py::_enforce_paper_safety` (Phase 3a) | Body must explicitly opt in even with all three above true. |

Phase 4 (live) adds a fifth — `confirm_live=true` symmetrically — and risk pre-checks before `placeOrder`. The flip is intentionally tiny because the safety surface is already shaped right.

### 3.4 Timestamp policy

All wire and storage use **`int64` ms UTC**. Per `.claude/rules/numerical-rigor.md` § "Timestamp rigor". Conversion from IBKR's two formats happens at exactly two boundaries:

- IBKR `YYYYMMDD` (option expiry) ↔ `int64 ms UTC` in `contracts.py::expiry_ms_to_yyyymmdd` / `yyyymmdd_to_expiry_ms`.
- IBKR `datetime` (Ticker.time) → `int64 ms UTC` in `market_data.py::_ticker_to_quote`.

Nothing else converts. No ISO strings, no naive datetimes, anywhere in the broker module.

### 3.5 SSE everywhere

Three streaming endpoints — option chain, P&L, order events — all use the same first-yield-then-debounce pattern, returning `text/event-stream`. Consumers see the same lifecycle:

```
event: <topic>
data: <Pydantic JSON>

event: <topic>
data: <next snapshot>

event: error                # only on broker error
data: {"error": "..."}      # then close
```

Cancellation: every async iterator wraps the IBKR subscription in `try / finally` and calls the matching `cancelMktData` / `cancelPnL` / `cancelPnLSingle` on consumer disconnect. No streaming-line quota leaks.

### 3.6 Container networking (Podman-on-Windows)

Three lessons learned during bring-up, captured in [phase 1 doc § "Lessons learned"](../archive/plans/ibkr-integration-phase1.md#lessons-learned-debugging-notes-from-initial-bring-up):

- `pydantic-settings` `case_sensitive=True` silently dropped uppercase env vars. **Fixed** in `config.py`. Regression test `test_uppercase_ibkr_env_vars_are_honored`.
- Gateway "Trusted IPs" filters by **source** IP (the WSL VM's peer), not the destination IP that lives in `IBKR_HOST`. Two different IPs, two different roles.
- `.env` changes need `compose down && up -d`, not `compose restart`.

`IBKR_HOST=auto` in `IbkrSettings` reads `/proc/net/route` to find the container's default gateway as a Docker-Desktop fallback. On Podman-Windows the user must set `IBKR_HOST` to the WSL bridge IP explicitly because the bridge gateway != Windows host.

## 4. Implementation summary

### 4.1 File map

```
PythonDataService/app/broker/ibkr/
  __init__.py
  config.py            # IbkrSettings: env-var safety + persist flags
  models.py            # Pydantic v2 wire types (int64 ms UTC throughout)
  client.py            # IbkrClient lifecycle + DU sentinel + auto-host
  contracts.py         # qualify_underlying, list_*, build_chain_contracts
  market_data.py       # stream_option_chain (Phase 1) — pre-qualify all,
                       # fail-fast on partial, finally cancels every reqMktData
  account.py           # fetch_account_summary, fetch_positions (Phase 2a)
  pnl.py               # stream_account_pnl, stream_position_pnl (Phase 2b)
  orders.py            # place_paper_order, cancel_paper_order, list_open_orders,
                       # stream_order_events (Phase 3a + 3b)
                       # + four-layer safety + idempotency cache
  persistence.py       # NoopTickWriter + ParquetTickWriter (Phase 1)
                       # NoopAccountWriter + ParquetAccountWriter (Phase 2c)
                       # NoopPnLWriter + ParquetPnLWriter (Phase 2c)

PythonDataService/app/routers/broker.py
  GET    /api/broker/health                          (Phase 1)
  GET    /api/broker/expirations/{symbol}            (Phase 1)
  GET    /api/broker/option-chain/{symbol}           (Phase 1, SSE)
  GET    /api/broker/account                         (Phase 2a)
  GET    /api/broker/positions                       (Phase 2a)
  GET    /api/broker/pnl/stream                      (Phase 2b, SSE)
  GET    /api/broker/pnl/positions/stream            (Phase 2b, SSE)
  POST   /api/broker/orders                          (Phase 3a)
  GET    /api/broker/orders/open                     (Phase 3b)
  DELETE /api/broker/orders/{order_id}               (Phase 3b)
  GET    /api/broker/orders/stream                   (Phase 3b, SSE)

PythonDataService/tests/broker/ibkr/
  test_config.py        (8)    test_models.py        (5)
  test_client.py        (14)   test_contracts.py     (3)
  test_market_data.py   (5)    test_persistence.py   (5)
  test_account.py       (9)    test_pnl.py           (7)
  test_orders.py        (17)   test_router.py        (13)
  Total: 86 tests, all stubbing ib_async — no live Gateway needed.

docs/architecture/
  ibkr-integration-phase1.md   # design + lessons learned
  ibkr-integration-phase2.md   # account/positions/PnL + reconciliation play
  ibkr-integration-phase3.md   # orders + paper safety details
  ibkr-integration-tdd.md      # this file (cross-phase narrative)
```

### 4.2 Phase deliverables (each one is one or two commits)

| Phase | What ships | LOC (src+tests) |
|---|---|---|
| 1 | option chain SSE + connection lifecycle + safety | ~1,100 + 570 |
| 2a | account summary + positions sync | ~213 + 278 |
| 2b | account-level + per-position P&L SSE streams | ~161 + 193 |
| 2c | persistence stubs (account writer, P&L writer) | ~170 + 0 |
| 3a | place paper order with four-layer safety | ~204 + 170 |
| 3b | cancel + open orders + order event SSE + idempotency | ~200 + 200 |
| **Total** | | **~3,500 LOC** |

### 4.3 Reconciliation play across phases

| Phase | What's compared | Where the gap shows | Likely cause |
|---|---|---|---|
| 1 | IBKR-computed Greeks/IV vs engine QuantLib/py_vollib | live option-chain table | Greeks-model assumptions (rate curve, dividend, American vs European) |
| 2a | IBKR positions/avg_cost vs engine `PortfolioService` | account-monitor table | Fill-model error (timing, partial fills) |
| 2b | IBKR live unrealized PnL vs engine mark | per-position P&L row | Greeks (Phase 1) + commission (Phase 3) compounded |
| 3 | IBKR fills vs engine `FillModel` predictions | per-fill diff | Slippage / commission / IB algo assumptions |

The decomposition matters more than any single comparison. Persistent gaps in (4) that aren't explained by Greeks-model differences (1) point to commission errors; persistent gaps in (1) that aren't size-related point to Greeks-model differences. Both feed `docs/math-sources-of-truth.md`.

## 5. Order placement (Phase 3) details

Order types covered in 3a + 3b: `MKT`, `LMT`. Time-in-force: `DAY`, `GTC`, `IOC`, `OPG`. Securities: `STK`, `OPT` on SMART/USD. Brackets, OCO, trailing stops, IB algos, multi-leg combos — all deferred (3.5+).

Idempotency lives in a process-local dict keyed by caller-supplied `client_order_id`. Survives across requests within a uvicorn worker; does **not** survive container restart. Durable idempotency (Redis-backed) is a Phase 3.5 ticket — for paper trading the in-memory cache is sufficient since the cost of a duplicate order is zero.

Order event stream uses **polling against `IB.trades()`** rather than ib_async's eventkit `orderStatusEvent`. Trade-off: a high-frequency burst could collapse two transitions into a single yield. For paper trading at 1 Hz polling that almost never matters; eventkit edge-trigger semantics is a 3.5 ticket if it does.

## 6. Risks and mitigations

| Risk | Where it bites | Mitigation |
|---|---|---|
| Streaming-line quota exhaustion (100/client) | option chain over-subscription | callers pre-narrow strikes; `market_data.py` fail-fasts on partial qualification |
| Pacing violations (50 msg/s, 1 historical req per 2s) | Phase 1.5 historical fetch | not relevant Phase 1-3; Phase 2.5 will add explicit pacing |
| Gateway nightly auto-restart | session expiry | reconnect loop in `IbkrClient.connect` with backoff; lifespan event re-tries on next request |
| 2FA mid-session | manual re-auth required | user requested IB to relax 2FA for API; otherwise daily Gateway login |
| IBKR Greeks model returns NaN/-1 sentinel | option chain rendering | `models._coerce_iv` and `_coerce_optional_float` translate to `None`; `greeks_source` records which block was used |
| Paper Greeks drift from live during fast markets | reconciliation false-positives | wider tolerance bands; document in `docs/references/reconciliations/` |
| Test-env env-var leakage | flaky CI | `cfg.reset_settings_for_testing()` in test try/finally |
| Live order placed in paper-mode build | catastrophic | four-layer safety; `placeOrder.assert_not_called()` regression test in live mode |

## 7. What's NOT in scope

Captured here so the next person doesn't accidentally re-architect:

- **Live trading**. Phase 4. Requires explicit operator approval, `confirm_live=true` flag, risk pre-checks, and `IBKR_MODE=live` flip.
- **Order modification (replace)**. IBKR's TWS API supports it; we don't. Cancel + new order is fine for paper.
- **Multi-account FA structures**. `IbkrClient.connect` warns on >1 managed account and uses the first. Multi-account selection is Phase 2.5+.
- **Multi-currency**. Phase 2 is USD-only. Other-currency rows are filtered out at the `fetch_account_summary` boundary. FX P&L is a separate ticket.
- **Brokerage selection**. The integration is hard-coded to IBKR. Swappable brokers (e.g. Tradier, Alpaca) would require an `IBrokerage`-style abstraction; not built.
- **Deep order book**. Phase 1 is top-of-book. Level 2 is more expensive and not currently warranted.
- **Historical data via IBKR**. Polygon is the canonical historical source; IBKR is for live only. The skill registry lists IBKR as a fallback historical source, but no code paths use it.

## 8. Open follow-ups (tracked, not promised)

In rough priority order:

1. **Frontend `account-monitor` page** — wires the seven non-stream endpoints and three SSE streams into the Angular UI. The reconciliation table is the headline feature. Backend contract is stable.
2. **CI fix** — Phase 1's failing pytest run was never paste'd back. Worth grabbing now before Phase 4 makes the test count larger.
3. **Phase 3.5: durable idempotency** — Redis or Postgres-backed `client_order_id` cache.
4. **Phase 3.5: bracket / OCO orders** — extend `IbkrOrderSpec` with optional `take_profit` / `stop_loss` legs.
5. **Phase 4: live trading** — `confirm_live=true`, risk pre-checks, mode flip.
6. **Persistence schema decisions** — Phase 2c shipped writer stubs but the partition / retention / replay decision is deferred.
7. **LEAN `InteractiveBrokersFeeModel` port** — tracked separately in `docs/archive/plans/lean-engine-phase2-plan.md` § 3.2 (archived); closes the commission-reconciliation gap.

## 9. How we got here — engineering process retrospective

Today's loop was unusually rough on plumbing for what should have been a straightforward green-field build. The lessons are worth capturing so the next big integration doesn't repeat them.

**The case-sensitive bug ate ~3 hours.** `pydantic-settings` defaults to `case_sensitive=True`, which means `IBKR_HOST=172.x.x.x` (compose) doesn't match field `host` (Pydantic). Codex spotted it because Codex could see the running process state — a reminder that "the env is set in the container" and "the running app sees the env" are separate facts that must both be checked. **Mitigation**: the regression test `test_uppercase_ibkr_env_vars_are_honored` pins this forever.

**Podman-on-Windows networking ate ~2 hours.** `host.docker.internal` resolves to `169.254.x.x` (APIPA, not the host). Auto-detected default gateway resolves to `10.89.0.1` (the Podman bridge, not the host). The actual answer was the **WSL vEthernet bridge IP** plus a Gateway-side **trusted-IP allowlist update** for the WSL VM peer IP (different from the destination IP). Three IPs, three different roles. **Mitigation**: the Phase 1 doc captures this in detail.

**Filesystem inconsistency between my Read tool view and the on-disk view ate ~1 hour.** Multiple times during this session, my `Read` tool showed a file that compiled while the disk file was truncated mid-line. The fix was always the same: re-write the file fresh, treat `bash sed` and `grep` as ground truth, never trust the `Read` view alone for files I just edited. **Mitigation**: rebuild large files in one shot rather than incremental edits when the file size grows past ~250 lines.

**Bottom line**: the code was easy. The build process around the code was hard. The TDD captures both so the next phase doesn't pay the same tax twice.

## 10. Frontend testing pointers

For when you build the Angular `account-monitor` and `option-chain-live` pages. Order of complexity, easiest first:

### 10.1 Health smoke (~5 minutes)

```typescript
// Trivial Apollo / fetch — verify the connection and the paper banner.
const health = await fetch('/api/broker/health').then(r => r.json());
console.log(health.connected, health.is_paper, health.account_id);
```

Visual: yellow `PAPER` pill in the app header iff `health.is_paper === true`. Red `BROKER DISCONNECTED` toast iff `health.connected === false`. **Never** trust `IBKR_MODE` env alone for the UI banner — it could be paper-mode but `is_paper === false` if the sentinel got bypassed.

### 10.2 Account + positions table (~30 min)

Both are plain REST GETs returning Pydantic-serialised JSON. No SSE, no streaming.

```typescript
const account = await fetch('/api/broker/account').then(r => r.json());
const positions = await fetch('/api/broker/positions').then(r => r.json());
// account.cash_balance, account.net_liquidation, account.buying_power, ...
// positions.positions[i].{symbol, sec_type, quantity, avg_cost, multiplier, ...}
```

Render side-by-side with the engine's `PortfolioService` view. The first time you see a discrepancy, that's a Phase 2a reconciliation finding — log it, don't paper over it.

### 10.3 Option chain SSE (~1 hour)

Use the native `EventSource` API rather than Apollo subscriptions for SSE. Apollo's transport is GraphQL-WS over websockets, which doesn't fit text/event-stream cleanly.

```typescript
const url = `/api/broker/option-chain/SPY?expiry_ms=${expiryMs}&strike_min=580&strike_max=600&debounce_ms=500`;
const sse = new EventSource(url);
sse.addEventListener('chain', (event) => {
  const snapshot = JSON.parse(event.data);
  // snapshot.symbol, snapshot.expiry_ms, snapshot.underlying_price,
  // snapshot.quotes[i].{strike, right, bid, ask, iv, delta, gamma, theta, vega, greeks_source}
  this.chainSignal.set(snapshot);
});
sse.addEventListener('error', (event) => {
  // Server-emitted error event (BrokerError or ValueError from qualification)
  // is delivered as `event: error\ndata: {"error": "..."}`. Browser also fires
  // `error` on transport failure — distinguish via `event.data` presence.
});
```

Test cases:

1. **Happy path**: connect, see `event: chain` snapshots arriving every ~500ms during market hours.
2. **Disconnect**: kill Gateway. The browser's `EventSource` auto-reconnects every ~3s. Verify the UI shows "broker disconnected" until reconnect succeeds.
3. **Bad symbol**: request `?symbol=ZZZZZZ`. Expect `event: error` and stream close.
4. **Out-of-band strikes**: request `strike_min=999999`. Server returns `404` synchronously — no SSE stream opens.

### 10.4 P&L streams (~1 hour)

Same `EventSource` shape, two endpoints:

```typescript
// Account-level
const accountPnl = new EventSource('/api/broker/pnl/stream?debounce_ms=1000');
accountPnl.addEventListener('pnl', (e) => {
  const tick = JSON.parse(e.data);
  // tick.con_id === null for account-level
  // tick.daily_pnl, tick.unrealized_pnl, tick.realized_pnl
});

// Per-position (subscribe after fetching positions)
const positions = await fetch('/api/broker/positions').then(r => r.json());
const conIds = positions.positions.map(p => p.con_id);
const url = `/api/broker/pnl/positions/stream?` +
  conIds.map(c => `con_ids=${c}`).join('&');
const positionPnl = new EventSource(url);
positionPnl.addEventListener('pnl', (e) => {
  const tick = JSON.parse(e.data);
  // tick.con_id is non-null; demultiplex on this.
  this.positionPnlSignal.update(map => ({ ...map, [tick.con_id]: tick }));
});
```

Test cases:

1. **Initial snapshot fires immediately** (no waiting for the first debounce window). The Pydantic model carries `daily_pnl: null` in the very first tick when IBKR hasn't computed yet — handle null gracefully.
2. **Multi-position demultiplexing**: subscribe to two `con_ids` and verify both yield ticks within one second.
3. **Unsubscribe-on-page-leave**: closing the EventSource must cancel the IBKR subscription. Verify in container logs: `cancelPnLSingle(...)` should appear.

### 10.5 Order placement form (~2 hours)

```typescript
// POST /api/broker/orders
const spec: IbkrOrderSpec = {
  symbol: 'SPY', sec_type: 'STK', action: 'BUY',
  quantity: 1, order_type: 'MKT',
  time_in_force: 'DAY',
  confirm_paper: true,                  // visual checkbox required to enable submit
  client_order_id: crypto.randomUUID(), // for idempotent retry
};
const ack = await fetch('/api/broker/orders', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(spec),
}).then(r => {
  if (r.status === 403) throw new OrderRefused(r.statusText);
  if (r.status === 422) throw new ValidationError();
  if (r.status === 503) throw new BrokerOffline();
  return r.json();
});
```

UI patterns:

1. **`confirm_paper` checkbox is required** to enable the submit button — even though the server enforces it, the user-facing form should mirror the safety pattern. Default state: unchecked. Submit button disabled.
2. **Banner above the form**: `PAPER MODE — DU1234567` in big text. Read from `/api/broker/health`. If `is_paper === false`, the form is locked and shows "Live trading not enabled."
3. **Show the last-placed-order ack** in a "recent orders" list. Don't auto-clear the form on submit; let the user see what they did.

Test cases:

1. **Submit without confirm_paper** → 422 (validation, FastAPI rejects before handler).
2. **Submit with `confirm_paper: false`** → 403 (handler refuses).
3. **Same `client_order_id` twice** → second response is the cached ack; verify `ack.placed_at_ms` is the original.
4. **Live mode (env=`live`)** → 403 with a distinct error message about the mode mismatch.
5. **Disconnected** → 503.

### 10.6 Open orders + cancel (~30 min)

```typescript
const open = await fetch('/api/broker/orders/open').then(r => r.json());
// open[i].{order_id, perm_id, symbol, action, quantity, status, cumulative_filled, remaining}

// Cancel
await fetch(`/api/broker/orders/${orderId}`, {method: 'DELETE'});
// Returns the trade snapshot; status will be 'PendingCancel' until the
// terminal 'Cancelled' arrives via the order event stream.
```

UI patterns: the open-orders table shows `cumulative_filled / remaining` so partial fills are visible. Cancel button disabled when `remaining === 0`.

### 10.7 Order events SSE (~1 hour)

```typescript
const orderEvents = new EventSource('/api/broker/orders/stream?poll_ms=500');
orderEvents.addEventListener('order', (e) => {
  const ev = JSON.parse(e.data);
  // ev.event_type ∈ {'status', 'fill', 'cancel', 'error'}
  // ev.order_id is the demux key.
  switch (ev.event_type) {
    case 'fill': /* update fill ledger */; break;
    case 'status': /* update order status pill */; break;
    case 'cancel': /* mark cancelled in the open-orders table */; break;
  }
});
```

The stream is **global** (covers every order the connected client has placed). Use `ev.order_id` to demultiplex on the client side.

### 10.8 The reconciliation table — the actual point

The whole stack pays off when you render IBKR vs Engine side-by-side. For each open position:

| Strike | Right | IBKR Δ | Engine Δ | Δ diff (bps) | IBKR IV | Engine IV | IV diff (bps) | IBKR PnL | Engine PnL | PnL diff |
|---|---|---|---|---|---|---|---|---|---|---|
| 580 | C | 0.55 | 0.553 | 30 | 0.21 | 0.215 | 50 | $123 | $128 | $5 |

Mark each diff column green if within tolerance, yellow if drifting, red if persistent. Persistent reds become entries in `docs/math-sources-of-truth.md` — the same way the SPX-vs-SPY 19 bps CBOE reconciliation already lives there.

### 10.9 Test fixtures for the frontend

Use **SPY only** initially. Pick the nearest weekly expiry from `GET /api/broker/expirations/SPY`. Strike band: spot ± $20. That's about 40 strikes × 2 (call/put) = 80 contracts, well under the 100-line market-data quota.

Don't subscribe multiple expiries at once until you've added the quota-management code. The streaming-line quota is per-client; oversubscribe and the chain stream silently degrades.

### 10.10 Generating types from the OpenAPI schema

FastAPI publishes OpenAPI at `http://localhost:8000/openapi.json`. Generate Angular client types with `openapi-typescript`:

```bash
npx openapi-typescript http://localhost:8000/openapi.json \
  -o Frontend/src/app/api/broker.types.ts
```

This gets you `IbkrAccountSummary`, `IbkrPosition`, `IbkrOrderSpec`, `IbkrOrderAck`, etc. as TypeScript types automatically. Re-run after every backend change. Beats hand-typing.

## 11. Acceptance criteria for this TDD

- [x] All four IBKR API surfaces evaluated, decision documented (§2.1).
- [x] All three Python library candidates evaluated, decision pinned in `requirements-light.txt` (§2.2).
- [x] Open-source reference architectures cited for the file layout decision (§2.3).
- [x] Cost analysis with current 2026 pricing (§2.4).
- [x] Four-layer paper safety described and located in code (§3.3, §5).
- [x] Reconciliation play described per phase (§4.3).
- [x] Lessons learned captured for next person (§3.6, §9).
- [x] Out-of-scope explicitly listed (§7).
- [x] Frontend testing pointers concrete enough to start without re-asking (§10).
- [x] Risk register with mitigations (§6).
- [x] All cross-references to other docs land on real anchors.

This document supersedes the per-phase docs as the **first read** for someone joining the integration. The phase docs remain authoritative for their own implementation details.
