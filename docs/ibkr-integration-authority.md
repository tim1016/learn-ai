# IBKR Integration — Authority

> **Canonical reference** for what the Interactive Brokers integration
> ships today. Source-of-truth implementation snapshot, not a design
> document — when this page disagrees with code, the code is right and
> this page must be updated in the same PR.
>
> **Authority boundary (2026-07-22):** this document covers the IBKR integration.
> The current Clerk, lifecycle, Bot Control, and trader-operating behavior is owned by
> [`bot-control-operator-manual.md`](bot-control-operator-manual.md), ADR-0030,
> ADR-0026, and the engine authority map. Do not use the historical deployment plan or
> a changelog entry below as an operator procedure.
>
> **Sibling docs** (different jobs, do not duplicate):
> - [`architecture/ibkr-integration-tdd.md`](architecture/ibkr-integration-tdd.md) — design rationale (why we chose `ib_async`, four-layer paper safety, SSE everywhere). Read first to understand "why."
> - Archived phase and deployment records — historical provenance under `docs/archive/`, not current authority.
> - [`codex-phase-1-4-audit.md`](codex-phase-1-4-audit.md) — most recent code audit; tracks Phase 10 prereqs.
>
> **Owner:** the engineer editing `PythonDataService/app/broker/ibkr/*` or `PythonDataService/app/engine/live/*`. Same-PR rule: if you touch those files, update the matching section here and bump **Last reviewed**.
>
> **Last reviewed:** 2026-07-23 (live-run evidence and validation status reconciled).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Architecture map](#2-architecture-map)
  - [2.1 Connection and market-data capacity contract](#21-connection-and-market-data-capacity-contract)
- [3. Configuration and four-layer paper safety](#3-configuration-and-four-layer-paper-safety)
- [4. Broker module surface (`app/broker/ibkr/`)](#4-broker-module-surface-appbrokeribkr)
- [5. REST + SSE endpoints (`/api/broker/*`)](#5-rest--sse-endpoints-apibroker)
- [6. Live runtime (`app/engine/live/`)](#6-live-runtime-appengineLive)
- [7. Frontend pages (`/broker/*`)](#7-frontend-pages-broker)
- [8. Persistence](#8-persistence)
- [9. Diagnostics](#9-diagnostics)
- [10. Test coverage](#10-test-coverage)
- [11. What does NOT ship today](#11-what-does-not-ship-today)
- [12. Operational checklist (paper week pre-flight)](#12-operational-checklist-paper-week-pre-flight)
- [13. Code cross-reference](#13-code-cross-reference)

---

## 1. Scope and authority

The IBKR integration answers: **can the system safely place paper orders, stream the data needed to manage them, and run a strategy that produces the same trades as the backtest?**

It does **not** answer:

- Whether real-money (live) trading is supported. **It is not.** Live mode is gated by `IBKR_MODE` and a separate runner that does not exist; see §11.
- Whether multi-symbol live trading is supported. **It is not.** `LiveEngine` raises `NotImplementedError` if `len(ctx.symbols) != 1`. See `live_engine.py:106`.
- Whether the backtest math is correct. That is the strategy / engine math layer's job; see [`feature-runner-authority.md`](feature-runner-authority.md) for research, and the SPY parity tests at `app/engine/tests/test_spy_*` for backtest math.

**Authority precedence** for IBKR integration behavior when this doc, the TDD, and
the code disagree: code wins, then this doc, then the TDD. For Clerk/lifecycle and
operator behavior, ADR-0030/ADR-0026 and the Bot Control manual take precedence over
this integration reference. The TDD captures design intent which can be older than the
implementation.

---

## 2. Architecture map

```
                      ┌──────────────────────┐
                      │  Strategy            │  shared with backtest
                      │  (SpyEmaCrossover)   │  unchanged from research
                      └──────┬───────────────┘
                             │ uses StrategyContext shape
              ┌──────────────┴──────────────┐
              │                              │
   ┌──────────▼──────────┐         ┌────────▼────────────┐
   │ StrategyContext +    │         │ LiveContext +        │
   │ Portfolio (sim)      │         │ LivePortfolio        │
   │ engine/strategy/     │         │ engine/live/         │
   └──────────┬───────────┘         └────────┬─────────────┘
              │                              │
   ┌──────────▼──────────┐         ┌────────▼─────────────┐
   │  BacktestEngine     │         │  LiveEngine           │
   │  engine/engine.py   │         │  engine/live/         │
   │  for-loop driver    │         │  asyncio driver        │
   └──────────┬──────────┘         └────────┬─────────────┘
              │                              │
   ┌──────────▼──────────┐         ┌────────▼─────────────┐
   │ LeanMinuteDataReader│         │  app.broker.ibkr.*   │
   │ FillModel           │         │  curated subset       │
   └─────────────────────┘         │  one ib_async import  │
                                   └───────────────────────┘
                                                ▲
                                                │ REST + SSE
                                   ┌────────────┴───────────┐
                                   │  Frontend /broker/*    │
                                   │  Angular 21 SPA        │
                                   └────────────────────────┘
```

**Boundary invariant** (do not violate without updating this section): `app/broker/ibkr/` is the **only** place in the repo that imports `ib_async`. Routers, the live engine, and the frontend talk to IBKR through the broker module's curated Python surface or its REST/SSE endpoints. Verified by `git grep -l "import ib_async"` across the repo.

### 2.1 Connection and market-data capacity contract

These limits have different scopes. Treating every number as a global
Gateway limit—or every number as a per-client limit—produces the wrong
architecture.

| Concern | Current IBKR contract | learn-ai policy |
|---|---|---|
| API connections | One TWS/IB Gateway instance accepts up to **32 simultaneous API connections**. A `clientId` must be unique among active connections and **may be any integer**; `0..31` is not the valid-ID range. | Never open one client per ticker. learn-ai deliberately accepts the non-negative ID subset. The public FastAPI data plane reuses its lifespan-owned `IbkrClient`. Host-runner children still use distinct clients because client/order identity and failure isolation are safety contracts, not market-data scaling tricks. |
| Per-second request pacing | IBKR Campus defines the rate per **client connection** as maximum market-data lines ÷ 2 per second: 50 requests/s at the default 100-line allocation. | `IbkrClient` explicitly pins `ib_async` to 45 requests per 1-second interval, leaving headroom below the default allocation. Do not add another ticker client to evade this pace. |
| User market-data lines | The default is **100 active lines shared by TWS and every API connection** for that username. The allocation can rise with commissions, equity, or quote boosters. | Local stream caps are conservative admission checks, not proof of remaining user capacity: this process cannot see TWS watchlists or sibling processes. IBKR error 101 remains the authoritative refusal when the shared pool is exhausted. |
| `reqRealTimeBars` | Emits only 5-second bars. Each active subscription consumes a Level-I market-data line. No more than **60 new real-time-bar requests per 600 seconds**. | `_RealtimeBarSubscriptionRegistry` keys by `(client, conId, bar size, whatToShow, useRTH)`, reference-counts consumers, enforces the component-local `IBKR_REALTIME_BAR_MAX_ACTIVE` admission cap (100 by default), and cancels only after the final consumer exits. `_RealtimeBarRequestPacer` enforces the 60/600 sliding window before a new broker request. `ib_async` owns the internal unique reqId and callback routing. |
| `reqHistoricalData` | At most **50 simultaneous open historical requests**. Small-bar historical requests also have duplicate/same-contract/60-per-10-minute pacing rules. With `keepUpToDate=True`, IBKR updates the in-flight bar approximately every 4–6 seconds and can emit the same bar timestamp repeatedly. | Historical backfill remains finite (`keepUpToDate=False`). The 50-open-request ceiling is not documented as the active `reqRealTimeBars` ceiling and must not be applied to that path. |

The public 5-second snapshot buffer and public 1-minute consolidator are
separate consumers but share one underlying real-time-bar line for the same
symbol. A late consumer starts with the next broker delivery instead of
replaying the shared `RealTimeBarList`; persisted bar replay remains the
historical recovery authority.

The registry is intentionally process-local. A host-runner child cannot share
its in-memory list with the FastAPI process or another bot. A future truly
central data plane would need a host-owned publisher with explicit liveness,
backpressure, timestamp, and failure-propagation contracts; silently coupling
order-owning children to an ad-hoc IPC feed is not acceptable.

**Scaling rule:** switching from `reqRealTimeBars` to `reqMktData` and building
bars locally does **not** create more market-data lines—both draw from the same
user allocation. `reqMktData` is also watchlist/top-of-book data sampled at
IBKR-defined intervals, not a raw exchange tick stream. Actual
`reqTickByTickData` has a much smaller specialized allocation (approximately
5% of market-data lines) and does not provide real-time options ticks. Local
aggregation is justified only when those different semantics are required.
When the symbol universe exceeds the available allocation, narrow the active
universe, increase the IBKR allocation, or use a specialized external
market-data provider. Multiple `clientId` values are not a capacity multiplier.

Official sources: [TWS API documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/),
[TWS API reference](https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-ref/),
and [market-data line allocation](https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/).

---

## 3. Configuration and four-layer paper safety

Settings live in `app/broker/ibkr/config.py:IbkrSettings`, env-prefixed `IBKR_`. Loaded from a `.env` file at repo root. Singleton — instantiated once via `get_settings()`.

Host-runner start requests have a separate authority split. The browser-facing
data plane validates only `ibkr_host` shape (bare host name or IP address, no
URL/path/userinfo). The host daemon enforces the configured connection policy
at start time. Its launcher contract is `--env-file <path>` (default
`<repo-root>/.env`), from which it loads only `IBKR_HOST_ALLOWLIST`,
`IBKR_HOST`, and `LIVE_RUNNER_IBKR_CLIENT_ID_POOL`; exported process env
values take precedence. This keeps policy in the process that spawns the
runner while avoiding shell-sourcing the full env file.

| Field | Default | Notes |
|---|---|---|
| `mode` | `paper` | `paper` or `live`. Default refuses to drift to live. |
| `host` | `auto` | `auto` resolves the container default gateway via `/proc/net/route`; literal IP or hostname accepted. |
| `port` | `4002` | Paper Gateway. Validated against `mode` — see Layer 2 below. |
| `client_id` | `1` | Reserved for the FastAPI lifespan client; later phases (recorder, etc.) get higher IDs. |
| `realtime_bar_max_active` | `100` | Component-local `reqRealTimeBars` cap. It does not prove user-level headroom because TWS and sibling clients share the allocation. Override with `IBKR_REALTIME_BAR_MAX_ACTIVE` only when the username's allocation supports it. |
| `connect_attempts` | `3` | Each attempt wraps `ib_async.connectAsync` with a 5s timeout. |
| `readonly` | `True` | Defense-in-depth at order placement time (Layer 0). Must be `false` in `.env` for Phase 3 endpoints. |
| `persist_ticks` | `False` | Tick stream → Parquet. Off by default; flip when forensic queries are needed. |
| `persist_account` | `False` | Account snapshot → Parquet. Off by default. |
| `persist_pnl` | `False` | P&L tick → Parquet. Off by default. |
| `persist_dir` | `/data/ibkr-ticks` | Parquet partition root. |

### The four layers

Defined in the TDD §3.3, implemented across `config.py`, `client.py`, and `orders.py`. **Order matters** — each layer is checked sequentially, and the first failure aborts the operation:

| # | Layer | Where | What it catches |
|---|---|---|---|
| 0 | `IBKR_READONLY=true` | `orders._enforce_paper_safety:76-138` | Operator opt-in lockdown. When true, every `place_paper_order` raises `OrderRefusedError` before any contract is built. |
| 1 | `IBKR_MODE` env var | `config.py:48` | Refuses to default to live. Settable per-process. |
| 2 | Port-vs-mode validator | `config.py:108-127` | `mode=paper` rejects ports `{7496, 4001}`; `mode=live` rejects `{7497, 4002}`. Catches typos in copy-pasted snippets. |
| 3 | Account-ID sentinel | `client.py:212-224` | After `connectAsync`, asserts `mode=paper` ↔ account ID begins with `DU` (paper). Disconnects and raises `ConnectionRefusedDueToSentinelError` on disagreement. |
| 4 | `confirm_paper=true` per order | `orders._enforce_paper_safety:76-138` | Every `IbkrOrderSpec` request body must explicitly set `confirm_paper=true`. Frontend gates this at the form level. |

**Single-account-FA assertion**: `client.py:201-208` refuses to connect if `managedAccounts()` returns more than one account. Multi-account FA structures are not supported because the sentinel would only validate one of them while orders could route to a sibling.

---

## 4. Broker module surface (`app/broker/ibkr/`)

Public surface only — private helpers (prefix `_`) and support modules are omitted unless they define an operator-facing contract.

| Module | Public surface | Purpose |
|---|---|---|
| `client.py` | `IbkrClient`, `get_client`, `set_client`, `BrokerError`, `ConnectionRefusedDueToSentinelError`, `NotConnectedError` | `ib_async.IB` lifecycle wrapper. Owns the singleton client, pins transport pacing to 45 requests per 1-second interval, and implements Layers 1+3 of paper safety. |
| `config.py` | `IbkrSettings`, `get_settings`, `reset_settings_for_testing`, `PAPER_PORTS`, `LIVE_PORTS` | Env-var-backed settings + Layer 2. |
| `contracts.py` | `qualify_underlying`, `list_expirations`, `list_strikes`, `build_option_contract`, `list_qualified_strikes`, `build_chain_contracts`, `expiry_ms_to_yyyymmdd`, `yyyymmdd_to_expiry_ms` | Stock + Option contract resolution. SMART/USD only. |
| `bars.py` | `aggregate_realtime_bar`, `stream_raw_5s_bars`, `stream_minute_bars`, `fetch_historical_minute_bars`, `LiveBarCounters`, `IBKRBarStreamError`, `IbkrMinuteBar` (model) | One IBKR 5-second TRADES source supports raw 5-second consumers and closed 1-minute aggregation. Same-process/same-contract consumers multiplex through one reference-counted `reqRealTimeBars` list; the final release cancels the broker line. New broker subscriptions are sliding-window paced at 60 per 600 seconds. Two duplicate policies remain: `strict` (default) fails fast on duplicate/non-monotonic timestamps; `live_idempotent` absorbs or corrects the most recent active-subscription redelivery and surfaces counters/logs. Per-bar provenance fields are `provenance`, `venue`, `session_phase`, and `use_rth`. Historical IBKR bars use finite `reqHistoricalDataAsync` (`keepUpToDate=False`), timeout, monotonicity guard, and `ibkr_historical` provenance. |
| `capability.py` | `probe_session_data_capability`, `parse_ibkr_schedule`, `classify_entitlement` | Issue #1005 Slice 0 read-only capability probe. Uses `reqContractDetailsAsync` to retain `tradingHours` / `liquidHours` / `timeZoneId` / `validExchanges`, requests a brief market-data line after `reqMarketDataType(1)` to classify live/delayed/frozen/none, cancels the line promptly, and calls `whatIfOrderAsync` with `whatIf=True` plus `outsideRth=True`. It never calls `placeOrder`. Schedule windows are parsed through IBKR's instrument timezone and serialized as `int64 ms UTC`; malformed schedule strings fail loudly. |
| `services/session_authority.py` | `session_state_at_ms` | Issue #1005 Slice 1/2 session authority. Consumes persisted `SessionDataCapability` windows when available, falls back to the canonical NYSE calendar otherwise, and emits PRE/RTH/POST/OVERNIGHT/CLOSED with next-transition ms. Strategy activity permission is evaluated from `allowed_sessions`, defaulting to RTH-only. Operator surface reads this authority instead of deriving PRE/POST locally. Authority split ratified in ADR 0029. |
| `market_data.py` | `stream_option_chain` | `reqMktData` with generic ticks `100,101,106` → debounced `IbkrChainSnapshot` SSE. Greeks selection: `model > bid > ask > last > none`. |
| `account.py` | `fetch_account_summary`, `fetch_positions` | One-shot reads of NLV / cash / margin / per-position state. |
| `account_truth.py` | `fetch_account_truth`, `compose_account_truth` | Account-wide broker truth projection. Joins account summary, positions, open orders, completed orders, executions, durable account-registry bot namespaces, app-minted manual namespaces, retired-owner live-exposure anomalies, and foreign/unclaimed facts into backend-authored invariants and operator copy. |
| `order_history.py` | `list_completed_orders` | `reqCompletedOrdersAsync(apiOnly=false)` sweep for recent terminal orders no longer present in the open-order list. This is live TWS evidence, not the delayed official statement. |
| `order_previews.py` | `preview_paper_order` | Non-submitting `whatIfOrderAsync` preview for manual paper orders. Reuses paper-sentinel checks but does not require submit confirmation because it does not place an order. |
| `orders.py` | `place_paper_order`, `list_open_orders`, `cancel_paper_order`, `stream_order_events`, `OrderRefusedError`, `OrderNotFoundError` | Layers 0+4 of paper safety. Idempotency cache keyed on `client_order_id` (process-local, not durable). Polling-based event stream (default 0.5s). Non-manual submits still need a namespace; router-level manual submits are server-stamped before this function is called. `IbkrOrderSpec.outside_rth` stamps IBKR `Order.outsideRth` explicitly; the live strategy path uses it only with `LMT` orders for declared extended sessions. |
| `pnl.py` | `stream_account_pnl`, `stream_position_pnl`, `DEFAULT_PNL_DEBOUNCE_S` | `reqPnL` / `reqPnLSingle` → debounced `IbkrPnLTick` SSE. |
| `persistence.py` | `TickWriter`, `make_writer`, `AccountSnapshotWriter`, `make_account_writer`, `PnLTickWriter`, `make_pnl_writer` (+ Noop / Parquet implementations) | Optional Parquet archive of ticks / snapshots / P&L. Off by default. |
| `diagnostics.py` | `run_diagnostics` | Self-test for the connection chain (8 checks). See §9. |
| `models.py` | `IbkrAccountSummary`, `IbkrPositionsSnapshot`, `IbkrPosition`, `IbkrOptionQuote`, `IbkrChainSnapshot`, `IbkrStrikeList`, `IbkrMinuteBar`, `IbkrOrderSpec`, `IbkrOrderAck`, `IbkrOpenOrder`, `IbkrOrderEvent`, `IbkrOrderWhatIfPreview`, `IbkrPnLTick`, `IbkrConnectionHealth`, `DiagnosticCheck`, `DiagnosticReport` | Pydantic v2 wire models. **Every** boundary timestamp is `int64` ms UTC. NaN / `-1` IBKR sentinels become `None` via `_coerce_optional_float` / `_coerce_iv`. `IbkrMinuteBar` carries source/provenance/venue/session metadata and those fields round-trip through JSONL/Parquet bar persistence. `IbkrOrderSpec.manual_order=true` means the router server-mints a reserved manual namespace; callers may not provide their own manual `order_ref`. |
| `__init__.py` | (re-exports) | Curated entry points only. |

---

## 5. REST + SSE endpoints (`/api/broker/*`)

Routes live in `app/routers/broker.py` and `app/routers/broker_account_truth.py`, both under prefix `/api/broker`, tag `broker`.

| Method | Path | Type | Response model | Purpose |
|---|---|---|---|---|
| GET | `/health` | one-shot | `IbkrConnectionHealth` | Connection diagnostic. **Never raises** on disconnect; returns `connected=false` so the UI can render the disconnected state. |
| GET | `/diagnose` | one-shot | `DiagnosticReport` | 8-check self-test (see §9). |
| POST | `/capability/probe?symbols=SPY,QQQ` | one-shot | `BrokerCapabilityProbeResponse` | Read-only IBKR capability probe per symbol. Persists `SessionDataCapability` snapshots under the broker artifact root. Uses only read calls and non-submitting `whatIf=True` previews. |
| GET | `/capability` | one-shot | `BrokerCapabilityReadResponse` | Reads latest persisted session/data capability snapshots; does not touch IBKR. |
| GET | `/account` | one-shot | `IbkrAccountSummary` | Cash, NLV, margin, account-level P&L. Optionally persisted via `persist_account`. |
| GET | `/positions` | one-shot | `IbkrPositionsSnapshot` | Open positions across all symbols. |
| GET | `/account-truth` | one-shot | `AccountTruthResponse` | Account-wide ownership and invariant projection for Account Monitor, Reconciliation, and Orders. Ownership comes from `accounts/<account_id>/instance_registry.jsonl`; retired terminal facts stay attributed. Unknown live orders, unassigned executions, and unexplained current positions fail closed as `not_proven`; retired-owner live exposure emits the distinct critical blocker `retired_owner_live_exposure`. A successful read stores the latest projection in the process-local Account Truth snapshot cache for Bot Control submit-readiness reads and the `account.account_truth` live submit gate; status/submit gate reads consume only the cache and do not sweep IBKR. |
| GET | `/expirations/{symbol}` | one-shot | `dict` | All listed option expiries for a symbol, `int64 ms UTC`. |
| GET | `/strikes/{symbol}?expiry_ms=...` | one-shot | `IbkrStrikeList` | Strikes IBKR can actually qualify (call ∩ put). |
| GET | `/option-chain/{symbol}` | SSE | `IbkrChainSnapshot` (per event) | Streaming option chain — debounced (default 250 ms). |
| GET | `/pnl/stream` | SSE | `IbkrPnLTick` | Account-level P&L. Debounced (default 1 s). |
| GET | `/pnl/positions/stream?con_ids=...` | SSE | `IbkrPnLTick` | Per-position P&L for the requested contract IDs. |
| POST | `/orders` | one-shot (201) | `IbkrOrderAck` | Place a paper order. Layer 0+4 enforced; idempotent via `client_order_id`. If `manual_order=true`, the router stamps `manual/operator/v1:{intent_id}` before submit; if `manual_order=false`, missing `order_ref` is still refused by `place_paper_order`. |
| POST | `/orders/what-if` | one-shot | `IbkrOrderWhatIfPreview` | Non-submitting paper-order preview using IBKR what-if state. The Orders confirmation dialog requires a successful preview before submit. |
| GET | `/orders/open` | one-shot | `list[IbkrOpenOrder]` | Currently-open orders the broker still tracks. |
| GET | `/orders/completed` | one-shot | `list[IbkrOpenOrder]` | Recent completed/cancelled/rejected TWS orders from `reqCompletedOrdersAsync(apiOnly=false)`. Used by account truth and exposed for diagnostics. |
| DELETE | `/orders/{order_id}` | one-shot | `IbkrOpenOrder` | Cancel a paper order. Refuses if mode is not paper or account is not DU. |
| GET | `/orders/stream` | SSE | `IbkrOrderEvent` | Order lifecycle events. Polling-based (0.5 s default); status transitions can collapse — see §6. |

**SSE format**: every event line is JSON-encoded payload prefixed with `data: `. The frontend uses `EventSource` via `Frontend/src/app/services/broker-sse.ts`. The TDD §3.5 explains why SSE rather than WebSocket.

**Timestamp policy**: every payload field literally named `timestamp`, `ts`, `time`, `*_ms`, or `*_at_ms` is Unix epoch milliseconds UTC (`int64 ms UTC`). `string` / `DateTime` typing on these fields is banned at the model layer. Frontend presentation renders those timestamps in the viewer/user's local timezone by default; any ET/exchange-time view must say so explicitly. See [`numerical-rigor.md`](../.claude/rules/numerical-rigor.md) "Timestamp rigor."

---

## 6. Live runtime (`app/engine/live/`)

The runtime that turns a `Strategy` into paper orders against IBKR.

### Surface

| File | What ships |
|---|---|
| `config.py` | `LiveConfig` dataclass — `symbol`, `force_flat_at: time \| None = time(15, 55)`, `consolidator_period_min`, `run_dir`, `max_submit_latency_ms`, `allowed_sessions` (canonical PRE/RTH/POST/OVERNIGHT allow-list, default `["RTH"]`). |
| `indicator_state.py` | Envelope/payload Pydantic models, HydratePolicy tri-state, IndicatorStateRepo (atomic write + advisory lock), the six-row validation ladder, top-level hydrate() and maybe_write() entry points. |
| `live_portfolio.py` | `LivePortfolio` — Portfolio-shaped surface with broker-backed account snapshots; `set_holdings`, `liquidate`, `submit_pending_orders`, `record_broker_fill`, `cancel_open_orders`. `BrokerAdapter` Protocol + `IbkrBrokerAdapter` (production) implementation. `submit_pending_orders` refuses pending orders before any broker call when the current session is outside `allowed_sessions` or outside the active order mechanism's supported sessions, writing `INTENT_DROPPED_BEFORE_SUBMIT/drop_reason=session_policy_block` for WAL-identified intents. In PRE/POST/OVERNIGHT that pass policy, the strategy path emits `LMT` orders at the current reference price with `outside_rth=True`; missing reference price fails closed before any broker call. |
| `live_context.py` | `LiveContext` — drop-in for `StrategyContext`. Reuses `TradeBarConsolidator` verbatim. Plumbs consolidated-bar close through to `LivePortfolio` reference price. |
| `live_engine.py` | `LiveEngine`, `LiveRunResult`, `ReplayBrokerAdapter` Protocol. Per-minute bar loop driven by `_next_bar_or_shutdown` (PR #231) — races `source.__anext__()` against `shutdown_event.wait()` so SIGINT unwedges within bounded time even when the bar source is silent (Gateway stall, market halt, IP-binding rejection). Emits a `[BAR]` heartbeat per bar (PR #229) for operator-visible aliveness during the strategy's indicator warmup window. When `output_dir` is configured, atomically persists each native `IbkrMinuteBar` before conversion/strategy use as `input_bars.parquet`, and persists exact decimal-text post-bar `equity_curve.parquet` snapshots alongside decisions, executions, and trades. Eager four-layer paper-safety validation when an `IbkrClient` is supplied. |
| `nyse_calendar.py` | `previous_completed_nyse_session_close_ms` — pandas_market_calendars NYSE schedule wrapper; consumed only by indicator-state validation (ladder check #3). |
| `run.py` | Operator CLI. Four subcommands: `init-ledger` (writes the run identity at `artifacts/live_runs/<run_id>/run_ledger.json`), `pre-flight` (runs the 7 morning halt checks in `pre_flight.py`), `start` (wires `shutdown_event` + SIGINT/SIGTERM handlers + rotating file logger + unhandled-exception recovery flatten + `IbkrClient.connect()/disconnect()`), `emergency-flatten`. |
| `reconcile.py` | Three-way daily reconciler — Python live ↔ QC Cloud ↔ IBKR fills. Per-bar `CrossEngineClass` (none/data/engine) and `FillClass` (none/within_tol/breach) classifications; writes `day-N.{md,json,parquet,hashes.json}`; emits `halt.flag` consumed by next morning's `check_no_halt_flag` pre-flight; SHA-256 manifest in the committed Markdown receipt includes `input_bars.parquet` and `equity_curve.parquet` when present. The original design is archived at `docs/archive/plans/2026-05-08-ibkr-paper-shadow-deployment-design.md` § 6. |
| `run_logging.py` | Rotating file logger (`<run-dir>/live.log`, 10MB × 5 backups) plus console handler with `[STEP X]` formatting when callers pass `extra={"step": ...}`. Idempotent for repeat init on the same run-dir. |
| `services/daily_session_schedule.py` | Start-boundary stop policy. RTH-only bots retain the default `force_flat_at=15:55 ET` stop clamped to the NYSE close. Bots declaring any extended session in `allowed_sessions` default to no daily stop so the process can continue through post/overnight; an explicit `force_flat_at` still blocks at that exchange-local time and is not clamped back to the RTH close. |
| `pre_flight.py` | Seven morning halt checks: `clean_tree`, `run_state_intact`, `no_halt_flag` (reads `halt.flag` written by `reconcile.write_day_report`), `ntp_offset`, `no_unexpected_position`, `yesterday_artifacts_intact` (walks the SHA-256 sidecar), plus skip-gates. |
| `run_ledger.py` | Run-identity ledger: strategy spec hash, QC audit copy hash, account_id, start-of-session ms, live-config JSON. Written by `cmd_init_ledger`. |
| `README.md` | Operator runbook. |

### What `LiveEngine.run()` does, in order, per minute bar

The bar loop iterates via `_next_bar_or_shutdown(source_iter, shutdown_event)` rather than `async for` (PR #231) so that SIGINT can win the race even when `source` is wedged on its own `__anext__`. When shutdown wins the race the loop body is skipped and the post-loop graceful-flatten path runs; otherwise the per-bar steps below execute:

1. `await broker.advance_bar(bar)` — replay-only hook; for `FakeBroker` this fills any pending orders at this bar's open. For real IBKR, fills land via `stream_order_events` and this is a no-op.
2. Drain order events that fired since the last bar; for each: `portfolio.record_broker_fill(event)`, append to result, call `strategy.on_order_event(event)`.
3. **Force-flat barrier** (PR #78): at most once per session date, when `bar.time.time() >= force_flat_at`, clear in-memory pending orders, call `broker.cancel_open_orders()`, queue a `liquidate` for every open position, submit, and call `strategy.on_force_flat()`. `force_flat_at=None` disables the barrier (used by the replay parity gate).
4. `portfolio.update_reference_price(symbol, bar.close)` — every minute, matching `BacktestEngine.run`.
5. Update consolidators with the bar — fires the strategy's bar handler if the consolidator boundary is crossed.
6. **`[BAR]` heartbeat log** (PR #229): one `INFO` line per minute — `[BAR] <iso-time> consolidator_emitted=<n> snapshot=<set|None>`. Operator's primary signal that the engine is alive during the strategy's indicator warmup window (≥ 3 h 45 m for `SpyEmaCrossoverAlgorithm` due to RSI(14)'s `samples >= period + 1` predicate); without it, warmup is silent and looks indistinguishable from a hang (the issue #227 misdiagnosis).
7. `await portfolio.submit_pending_orders()` — drain anything the strategy queued.
8. `ctx.insight_manager.step(bar.end_time, current_prices)` — score expired insights.
9. Append an `EquitySnapshot` for the bar.

After the bar loop ends: `strategy.on_end_of_algorithm()`, then finalize any insights still active.

### Replay parity gate

`tests/engine/live/test_live_engine_replay.py` runs `BacktestEngine` and `LiveEngine` against the same `LeanMinuteDataReader(LEAN_CACHE_ROOT)` and asserts **exact** match (`Decimal("0")` tolerance) on:

- Order count and per-order fields (symbol, direction, fill_quantity, fill_price, fee, tag, fill time within 1 ms).
- Order ID monotonicity and uniqueness.
- Final cash, NLV, total fees.
- Equity-curve per-snapshot timestamp / equity / cash / holdings_value.
- Trade-log per-trade entry/exit time, prices, pnl_pts, pnl_pct, result, indicators.
- Insight count and per-insight 16-tuple signature (incl. final score).
- Force-flat parity (per-event `tag == "ForceFlat"`).

The gate skips on CI when `lean-cache/` is absent (gitignored runtime data — populated locally by the engine on first backtest).

### Lifecycle-collapse coverage

`test_live_engine_collapse.py` covers the polling-based `stream_order_events` collapse case (`PendingSubmit → Submitted → Filled` yielding only `Filled`):

- **Entry-side**: `OneFillStrategy` submits one entry; broker collapses; `strategy.on_order_event` fires once.
- **Exit-side** (PR #78): `EntryThenExitStrategy` submits entry then exit over two 1-minute consolidator emissions; both lifecycles collapse correctly; final position is flat.

### Known parity-non-equivalent behavior

`LiveEngine` force-flat submits a market liquidation that fills on the next print after submission (under `FakeBroker` that's the next bar's open). `BacktestEngine` synthesizes a fill at the current bar's close, bypassing the fill model. The price residual is what `reconcile.py`'s `classify_fill` measures and classifies (`FillClass.within_tol` vs `breach` against `FillTolerances.price_atol=0.05`). Documented in `LiveEngine.run` docstring.

### Graceful shutdown (SIGINT/SIGTERM, 2026-05-12; wedged-source fix 2026-05-13)

`LiveEngine.run()` accepts an optional `shutdown_event: asyncio.Event`. `run.py`'s `cmd_start` creates the event, registers SIGINT and SIGTERM handlers on the asyncio loop via `loop.add_signal_handler` that set it, and passes it through to `engine.run`. The bar loop iterates via `_next_bar_or_shutdown` (PR #231) which races `source.__anext__()` against `shutdown_event.wait()` — when shutdown wins, the loop returns `(None, True)`, `_shutdown_flatten` cancels open broker orders, liquidates open positions, submits the liquidations, and the existing `finally` block flushes artifact writers + stops the broker event stream.

**Responsiveness:** SIGINT now fires within bounded time (sub-second in practice) regardless of bar arrival. The original 2026-05-12 design checked `shutdown_event` inside the `async for` loop body, which meant SIGINT was honored only when the next bar arrived — fine when bars are flowing, but indefinitely deferred if the bar source was wedged (Gateway daily restart, market halt, IBKR error 420 from same-IP-binding rejection). The 2026-05-13 race-based helper closes this gap.

**Source-exception propagation:** if `source_iter.__anext__()` raises a non-cancellation, non-`StopAsyncIteration` exception (broker stream failure, IBKR connection drop, malformed bar) around the same time as shutdown, `_next_bar_or_shutdown` re-raises the source exception rather than returning `(None, True)` — operators see broker errors instead of having them masked by the graceful-exit path.

**Platform constraint:** `add_signal_handler` raises `NotImplementedError` on Windows's default event loop and the helper falls through with a warning. Windows operators stop the run with **Ctrl+C** in the terminal; `asyncio.run` translates it to `CancelledError`, which propagates through `engine.run`'s `finally` block — writers flush and the IbkrClient disconnects cleanly, but the structured `_shutdown_flatten` path is not invoked. The dry-run runbook calls this out.

### Unhandled-exception recovery (2026-05-12)

`cmd_start` wraps `engine.run` in an exception handler that, on an unhandled `Exception`, attempts a best-effort flatten via `_recovery_flatten`: re-fetches positions from the broker, cancels open orders (failures logged, not blocking), and submits a market liquidation per open position. Failure to recover-flatten logs the cause and tells the operator to run `emergency-flatten --confirm` manually. Exit code 3 either way.

### `IbkrClient` lifecycle in `cmd_start`

`cmd_start` now calls `await client.connect()` before driving the engine and `await client.disconnect()` in the surrounding `finally`. This closes a latent bug — the prior CLI created an `IbkrClient` but never connected it, so `_validate_paper_client` would raise "requires a DU paper account, got None" on the first run against a real Gateway. The injected-broker test path (`args.broker` set by tests) bypasses this — `FakeBroker` is always "connected."

### File logging with rotation

`app/engine/live/run_logging.py:configure_run_logging` attaches a `RotatingFileHandler` at `<run-dir>/live.log` (10 MB × 5 backups) plus a console handler to the root logger. Format inlines a `[STEP X]` prefix when callers pass `extra={"step": "N"}`; absent step attributes are defaulted by a custom filter so existing log calls don't break. Invoked in `cmd_start` after the ledger loads.

---

## 7. Frontend pages (`/broker/*`)

Standalone Angular 21 components, signal-driven, OnPush, gated by `BrokerHealthService.bannerState`.

| Route | Component | Purpose | Gates |
|---|---|---|---|
| `/broker` | `BrokerStatusComponent` | Connection card (mode, account, sentinel), account snapshot, positions table, **Diagnose** button (PR #77) with per-check pills + fix hints. | Always visible. Account/positions cards hide when disconnected. |
| `/broker/options-chain` | `BrokerOptionsChainComponent` | SSE-driven chain table; multi-strike select, NBBO + greeks, debounce-coalesced. | Locked unless `isPaperConnected()`. |
| `/broker/accounts/:accountId` | `AccountDeskPageComponent` | Account Truth, broker snapshot, ownership, account recovery proof, reconciliation status, and backend-authored recovery actions. | Account-scoped evidence is rendered through trader and operator lenses. |
| `/broker/account-monitor` | `AccountMonitorRedirectComponent` | Legacy bookmark redirect to the selected Account Desk. | Redirects to `/broker/accounts/:accountId` when exactly one account is available; otherwise opens the account roster. |
| `/broker/orders` | `BrokerOrdersComponent` | Manual paper-order form with what-if preview, server-minted manual namespace, account-truth order ledger, cancel affordance for live working orders, and order-event SSE. | Locked unless paper-connected (defense-in-depth on the four-layer safety). |

**Shared services**:

- `BrokerHealthService` — singleton 5-second poll of `/api/broker/health`. Exposes `health`, `bannerState`, `isPaperConnected` signals. The shell paper/live/disconnected banner reads from this.
- `BrokerService` — `firstValueFrom`-wrapped REST client for the non-SSE endpoints.
- `broker-sse.ts` — `EventSource` helper that each SSE-consuming page owns explicitly (no global SSE manager).

**Type generation**: REST-shaped models in `Frontend/src/app/api/broker.types.ts` are regenerated from the Python service's OpenAPI spec. SSE-only payloads (`IbkrChainSnapshot`, `IbkrPnLTick`, `IbkrOrderEvent`) and recently-added broker/account-truth types (`DiagnosticReport`, `AccountTruthResponse`, `IbkrOrderWhatIfPreview`) are hand-mirrored in `broker-models.ts` until the next regeneration. See `Frontend/AGENTS.md` for the regenerate command.

---

## 8. Persistence

Optional Parquet archive of three streams. **Off by default** — flip individual flags only when forensic queries are needed.

| Stream | Setting | Path | Schema |
|---|---|---|---|
| Option ticks | `IBKR_PERSIST_TICKS=true` | `{persist_dir}/{date}/ticks.parquet` | `IbkrOptionQuote` columns + `as_of_ms`. |
| Account snapshots | `IBKR_PERSIST_ACCOUNT=true` | `{persist_dir}/{date}/account.parquet` | `IbkrAccountSummary` columns. |
| P&L ticks | `IBKR_PERSIST_PNL=true` | `{persist_dir}/{date}/pnl.parquet` | `IbkrPnLTick` columns; account-level rows have `con_id=NULL`, per-position rows carry the contract id. |

Writers are factories — `make_writer` / `make_account_writer` / `make_pnl_writer` return either a Noop or a real Parquet writer based on the flag. Endpoints offer every snapshot to the configured writer; the writer flushes on close.

Live-run evidence is separate from these optional browser/API archives. A
`LiveEngine` run with an `output_dir` always uses the run directory's atomic
Parquet artifact bundle: native `input_bars.parquet` is published before a
broker bar becomes a `TradeBar`; decisions, executions, trades, and exact
decimal-text `equity_curve.parquet` snapshots flush on the completed bar checkpoint.
The daily reconciliation hash manifest records those additional receipts, but
does not yet perform an equity-series comparison.

---

## 9. Diagnostics

`GET /api/broker/diagnose` (PR #77) runs an 8-check self-test and returns a `DiagnosticReport` with `overall_status` of `pass | warn | fail`. Each `DiagnosticCheck` has `name`, `label`, `status` (`pass | warn | fail | skip`), `detail`, and an optional `fix` hint.

| # | Check | What it verifies |
|---|---|---|
| 1 | `settings_mode` | `IBKR_MODE` is `paper` or `live`. |
| 2 | `settings_port` | Port matches mode. Paper ports: `{4002, 7497}`; live ports: `{4001, 7496}`. |
| 3 | `host_resolution` | `IBKR_HOST` resolves. `auto` reports the detected gateway IP; failure to resolve auto is a `warn`. |
| 4 | `tcp_reachable` | 2-second `asyncio.open_connection` to `host:port`. Surfaces refused / timeout / DNS failure with a fix hint. |
| 5 | `client_initialized` | The FastAPI lifespan `IbkrClient` is constructed. |
| 6 | `client_connected` | `client.is_connected()` reports the `ib_async` session is open. |
| 7 | `account_sentinel` | Connected account ID matches mode (paper IDs begin with `DU`). |
| 8 | `account_fetch` | `fetch_account_summary` round-trips against the live session. |

Read-only: never calls `connect()` and never places orders. The frontend exposes this as the **Diagnose** button on `/broker`.

---

## 10. Test coverage

As of 2026-07-01 (post Account Truth MVP; historical rows retained from the prior reviewed snapshot):

| Area | File | Tests |
|---|---|---|
| **Broker module — reviewed tests** | | |
| | `tests/broker/ibkr/test_account.py` | 9 |
| | `tests/broker/ibkr/test_bars.py` | 12 (incl. `open_` regression from PR #78 and 5 `live_idempotent`/policy tests) |
| | `tests/broker/ibkr/test_client.py` | 14 |
| | `tests/broker/ibkr/test_config.py` | 8 |
| | `tests/broker/ibkr/test_contracts.py` | 8 |
| | `tests/broker/ibkr/test_market_data.py` | 10 |
| | `tests/broker/ibkr/test_models.py` | 5 |
| | `tests/broker/ibkr/test_orders.py` | 18 |
| | `tests/broker/ibkr/test_order_history_previews.py` | 3 |
| | `tests/broker/ibkr/test_persistence.py` | 5 |
| | `tests/broker/ibkr/test_pnl.py` | 7 |
| | `tests/broker/ibkr/test_account_truth.py` | 4 |
| | `tests/broker/ibkr/test_router.py` | 25 |
| **Live runtime — 15 tests** | | |
| | `tests/engine/live/test_live_context.py` | 5 |
| | `tests/engine/live/test_live_engine.py` | 3 (incl. force-flat fire + no-fire from PR #78) |
| | `tests/engine/live/test_live_engine_collapse.py` | 2 (entry- + exit-side from PR #78) |
| | `tests/engine/live/test_live_engine_replay.py` | 1 (HARD GATE; skipped on CI when `lean-cache/` absent) |
| | `tests/engine/live/test_live_portfolio.py` | 4 |
| | `tests/engine/live/test_order_identity.py` | 15 |

Project-scope: `pytest tests/ -k "not slow"` reports **1797 passed, 3 skipped, 5 xpassed** on the post-PR-#78 tree. CI runs the same scope on every PR.

---

## 11. What does NOT ship today

Tracked deliberately. None of these are accidental gaps; each is documented and gated.

| Area | Status | Why deferred |
|---|---|---|
| Live (real-money) trading | NOT SUPPORTED | Phase 4 in `architecture/ibkr-integration-tdd.md` §7. Will require a separate `run_live.py` runner with its own config profile, not a flag on the paper runner. |
| Multi-symbol live | NOT SUPPORTED | `LiveEngine` raises `NotImplementedError` on `len(ctx.symbols) != 1`. Mirrors `BacktestEngine` v1 scope. |
| Options paper trading via `LiveEngine` | NOT SUPPORTED | `LiveEngine` is equity-only in v1. Options option-chain *streaming* is supported; placing options orders via the runtime is not. |
| Phase 8 — paper config + CLI + signal handling + log rotation | **SHIPPED** (2026-05-12, hardened 2026-05-13) | `run.py` has four subcommands (`init-ledger`, `pre-flight`, `start`, `emergency-flatten`); `start` wires `shutdown_event`, signal handlers, rotating file logger, unhandled-exception recovery flatten, and `IbkrClient.connect()/disconnect()`. PR #229 added per-minute `[BAR]` heartbeat. PR #231 unwedged the SIGINT path when bar source is silent (`_next_bar_or_shutdown` helper) and propagates source exceptions during shutdown. Deferred to follow-ups: YAML config input, `LiveConfig` → `BaseSettings` conversion, `[STEP X]` log-call sweep inside `LiveEngine.run` (helper supports it; existing calls still need migration). |
| Phase 9 — daily reconciliation tooling | **SHIPPED** (2026-05-08, registered 2026-05-13) | `app/engine/live/reconcile.py` implements the three-way design from the shadow-deployment spec — Python live ↔ QC Cloud ↔ IBKR fills. Per-bar `CrossEngineClass`/`FillClass` classification, `day-N.{md,json,parquet,hashes.json}` artifacts, halt.flag wired into pre-flight, week rollup. 25 unit tests in `tests/engine/live/test_reconcile.py`. Note: the deployment plan § 11 described an older paper-vs-backtest framing; the three-way design supersedes it. |
| Phase 10 — actual paper week + reconciliation report | NOT STARTED | Operational; the local durability and producer/consumer prerequisites are implemented. A full RTH, host-run, read-only observation and post-close reconciliation receipt remain required before any paper-week claim. |
| Phase 10 prereq — full-RTH end-to-end dry-run pass | NOT YET RUN | The longest live-Gateway session attempted on 2026-05-13 was 30 min (container, then 20 min host-side). Neither reached strategy readiness (the current strategy needs ≥3 h 45 m of RTH minute bars). We have not yet observed `init-ledger → pre-flight → host-run start --readonly → reconcile` against one real session. [Issue #1211](https://github.com/tim1016/learn-ai/issues/1211) is the operative checklist; do not use historical root-level dry-run PDFs or smoke scripts. |
| Phase 10 prereq — `commissionReport` callback wiring | IMPLEMENTED; LIVE OBSERVATION PENDING | Poll-based fill conversion records `Fill.commissionReport.commission` when IBKR has reported it and retains `None` when it has not; the test suite covers both. A full RTH observation must still establish whether reports arrive reliably enough for a commission comparison to become gating. |
| Account Truth post-hoc manual adoption | NOT SHIPPED | App-minted manual orders are supported through `manual/operator/v1`, but TWS hand-clicks remain `foreign_or_unclaimed`. The adoption workflow must be explicit, one fact at a time, append-only, and keyed by `permId` or `execId`; it is not a heuristic based on `clientId=0`. |
| Account Truth operator-specific manual namespace | PARTIAL | The server mints a reserved manual namespace, currently `manual/operator/v1`, because this broker route has no authenticated operator/session principal. Before person-level audit claims, wire a real operator or session slug into the server mint. |
| Flex delayed audit import | NOT SHIPPED | `flex_audit_match` is reported as `not_applicable`. Flex remains the delayed official statement source for settled executions, commissions, cash, and positions. |
| Client Portal account-truth cross-check | NOT SHIPPED | No `/iserver` calls are made in the live validation path. Any Client Portal use requires a separately documented session-safety decision and experimental/disabled labelling. |
| Phase 10 prereq — `equity_curve.parquet` writer | **SHIPPED** (2026-07-23) | `LiveEngine` persists exact decimal-text post-bar `timestamp_ms`, `equity`, `cash`, and `holdings_value` rows through the atomic artifact bundle; the reconciliation receipt hashes it. QC equity-series comparison is still a future extension. |
| Phase 10 prereq — indicator-state-persistence across restarts | **SHIPPED** (2026-05-15) | Generic envelope + SpyEma-specific payload at `PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json`. Three policies: `require` (default — paper-week gate), `optional` (seed-day cold-start), `disabled` (operator escape hatch). NYSE previous-completed-session staleness check. Per-run hydration receipt rolled into reconcile hash manifest. Historical design: `docs/archive/plans/2026-05-15-spy-ema-paper-dry-run-design.md`. |
| Phase 10 prereq — end-to-end producer test (LiveEngine → reconcile) | **SHIPPED** (2026-07-23) | CI runs a minimal `LiveEngine` session against `FakeBroker` that produces its own non-empty decisions, executions, equity, and hydration receipt; `write_day_report` consumes those artifacts and records their hashes. The QC input remains an explicitly scoped fixture, not a claim of independent parity. |
| `IbkrMinuteBar` → `TradeBar` conversion in `stream_minute_bars` consumer | **SHIPPED** (2026-07-23) | `trade_bars_from_ibkr` converts the native stream before engine use. The real-broker-boundary test verifies converted `TradeBar` delivery and durable native `input_bars.parquet` plus `equity_curve.parquet` receipts. |
| `client_order_id` per-session uniqueness | TRACKED | Counter resets per `LivePortfolio`; `place_paper_order` idempotency cache is process-scoped. PR #76 review C2 — Phase 10 prereq. |
| `IbkrMinuteBar` `model_validator` for `end_ms == start_ms + 60_000` and `volume >= 0` | TRACKED | Defensive; unenforced today. PR #76 review R5. |
| `LiveEngine.run()` guard for `bars=None` and `client=None` | TRACKED | Currently passes `None` to `stream_minute_bars` if both are absent; should fail fast. PR #76 review R7. |
| `[STEP X]` structured logging in `LiveEngine` | PARTIAL | `run_logging.configure_run_logging` supports `[STEP X]` via `extra={"step": ...}`; `cmd_start` and the new shutdown/recovery paths use it. Pre-existing log calls inside `LiveEngine.run` still need migration; tracked as a sweep. |
| Single-account FA support | NOT SUPPORTED | `client.py:201-208` refuses to connect on >1 managed account. |
| 2FA mid-session | OUT OF SCOPE | TDD §6 risk register. Operator handles via Gateway settings. |
| Order ID persistence across restarts | NOT SHIPPED | `.live_state.json` is in the plan §10 hygiene tasks but unimplemented. Postgres-based persistence is a separate ticket because there is no migrations workflow yet. |

---

## 12. Platform-owner integration preflight (not a Bot Control procedure)

This is infrastructure context for the platform owner. Operators use the UI-first
procedure in `docs/bot-control-operator-manual.md`; it is the only current Bot Control
and Clerk operating manual.

Run these checks before turning the runner loose:

1. **Platform configuration** must select the paper account, paper port, and the
   approved host allowlist. The deployed daemon policy is the authority; an operator
   does not change it through the Bot Control surface.
2. **NYSE/ARCA real-time market-data subscription** active on the linked live account. Paper inherits — see TDD §2.4.
3. **IB Gateway** running, logged into the paper account, "Read-Only API" is OFF, and the API tab's "Trusted IPs" includes:
   - **`127.0.0.1`** (host loopback) — required by the host daemon and its bot children.
   - The configured data-plane bridge address — required only when the platform's
     infrastructure health/diagnostic routes use it.

   The platform owner maintains these settings. Bot operators use the diagnostics and
   launch evidence surfaced by the application rather than a host command.
4. **`GET /api/broker/health`** returns `connected: true, is_paper: true` and the account ID begins with `DU`.
5. **`GET /api/broker/diagnose`** returns `overall_status: pass` (or click the **Diagnose** button on `/broker`).
6. **Project-scope tests** green: `pytest PythonDataService/tests/ -k "not slow"`. 1797+ pass; the replay parity test must skip with the `lean-cache` message on a clean CI runner or pass locally where the cache is materialized.
7. **Operator path** uses Bot Control and Account Desk, never a one-off CLI start.
   The authenticated host daemon owns process launch and allocates distinct client ids;
   the UI/manual surface displays its evidence and directs recovery. See
   `docs/bot-control-operator-manual.md`.

If any of these fails, fix it before running. The diagnostic endpoint will tell you which layer is the blocker.

---

## 13. Code cross-reference

| Concept | Files | Notes |
|---|---|---|
| Paper safety boundary | `app/broker/ibkr/{config,client,orders}.py` | Layers 0-4 in §3. |
| Boundary timestamp policy | `app/broker/ibkr/models.py` (`int64 ms UTC` everywhere) | See `.claude/rules/numerical-rigor.md` "Timestamp rigor." |
| Account Truth projection | `app/broker/ibkr/account_truth.py`, `app/schemas/account_truth.py` | Backend-authored ownership, invariant, blocker, caveat, and ledger rows. |
| Account Truth endpoints | `app/routers/broker_account_truth.py` | `/account-truth`, `/orders/what-if`, and `/orders/completed` under `/api/broker`. |
| Manual order namespace | `app/engine/live/order_identity.py`, `app/routers/broker.py` | `manual/{operator_or_session}/v1:{intent_id}` is server-minted for `manual_order=true`. |
| Completed order sweep | `app/broker/ibkr/order_history.py` | `reqCompletedOrdersAsync(apiOnly=false)` mapped to `IbkrOpenOrder` evidence rows. |
| What-if preview | `app/broker/ibkr/order_previews.py` | Non-submitting `whatIfOrderAsync` preview for manual paper order confirmation. |
| Strategy contract | `app/engine/strategy/base.py` (unchanged for live) | `LiveContext` mirrors `StrategyContext`. |
| Backtest engine | `app/engine/engine.py` | The replay parity gate runs both engines from the same data source. |
| Live engine | `app/engine/live/live_engine.py` | Per-bar loop driven by `_next_bar_or_shutdown` (race-helper from PR #231); per-minute `[BAR]` heartbeat (PR #229); force-flat from PR #78. |
| Replay parity gate | `tests/engine/live/test_live_engine_replay.py` | `Decimal("0")` tolerance; CI skips when `lean-cache/` absent. |
| Frontend gate | `Frontend/src/app/services/broker-health.service.ts` (`isPaperConnected`) | Defense-in-depth at form level. |

---

## Change log

| Date | Reviewer | Notes |
|---|---|---|
| 2026-07-23 | Codex GPT-5 | Added durable per-run market-input and equity receipts: `input_bars.parquet` is atomically published before native IBKR bars are converted for strategy use; exact decimal-text `equity_curve.parquet` snapshots flush with the per-bar artifact checkpoint. The text representation avoids Arrow's variable-Decimal-scale dataset merge failure without a float conversion. The reconcile receipt now hashes both when present. Replaced the synthetic producer fixture with a FakeBroker run whose own decision, execution, equity, and hydration artifacts feed `write_day_report`; retained the full-RTH host-run observation as an explicit operational prerequisite. |
| 2026-07-17 | Codex GPT-5 | Removed the unfinished `/broker/reconciliation` comparison route and component: both engine-side comparison columns were deliberately empty, while the useful Account Truth and account-recovery information is owned by `/broker/accounts/:accountId`. Removed the sidebar entry and redirected the bot-instance runbook action to the current bot control page, preserving run-scoped reconciliation without a dead route. |
| 2026-07-15 | Codex GPT-5 | Corrected IBKR capacity scopes from current primary documentation: 32 is a simultaneous-connection count rather than a `clientId` range; the default 100 market-data lines are shared across TWS and API clients; per-second request pacing is per connection; 50 simultaneous open requests belongs to historical data; and `reqRealTimeBars` uses the shared line allocation plus 60-new-requests/600-second pacing. Added same-process real-time-bar multiplexing, reference-counted cancellation, configurable local active-line admission, and an explicit 45 requests/second transport pin. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 0 capability probe landed: `capability.py`, `/api/broker/capability/probe`, `/api/broker/capability`, persisted `SessionDataCapability` JSON snapshots, schedule parsing through instrument timezone, market-data entitlement classification, non-submitting `whatIf=True` outside-RTH preview, and Broker Status capability panel. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 1 session authority landed: `services/session_authority.py`, operator-surface trading session now consumes capability snapshots when present and falls back to NYSE calendar otherwise, `OVERNIGHT` was added to the trading-session phase contract, and ADR 0029 records the calendar-vs-IBKR authority split. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 2 session-gated execution landed: `LiveConfig.allowed_sessions` defaults to RTH-only and round-trips through deploy/ledger validation; real-broker `LiveEngine` submit paths consult centralized session authority using persisted capability snapshots when available; `LivePortfolio.submit_pending_orders` drops and WAL-marks pending intents with `session_policy_block` before any broker call when the phase is closed, not strategy-permitted, or not yet supported by the order mechanism. PRE/POST/OVERNIGHT placement remains blocked until Slice 3. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 3 extended-hours order mechanism landed: `IbkrOrderSpec.outside_rth` stamps IBKR `Order.outsideRth`; live extended-session submits that pass `allowed_sessions` use `LMT` plus the current reference price and `outside_rth=True`; missing reference prices fail closed with `session_policy_block`. RTH submits remain `MKT/outside_rth=False`. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 4 bar provenance landed: `IbkrMinuteBar` now carries `provenance`, `venue`, `session_phase`, and `use_rth`; IBKR real-time bars stamp qualified venue and session metadata, `fetch_historical_minute_bars` adds timeout-bounded read-only IBKR historical bars with `ibkr_historical` provenance, Polygon historical chart overlays stamp `polygon_historical/POLYGON/RTH`, chart buckets combine mixed provenance/venues explicitly, and bar persistence round-trips the fields through JSONL and Parquet. |
| 2026-07-12 | Codex GPT-5 | Issue #1005 Slice 5 continuous lifecycle landed: `_live_config_from_ledger` disables the old 15:55 ET force-flat default when `allowed_sessions` declares PRE/POST/OVERNIGHT and no explicit `force_flat_at` was supplied; `daily_session_schedule` mirrors that rule so start-boundary checks allow extended-session bots after the old RTH stop while preserving explicit operator stops. |
| 2026-07-01 | Codex GPT-5 | Account Truth MVP landed: new account-wide projection (`account_truth.py` + schema), completed-order sweep (`order_history.py`), non-submitting what-if preview (`order_previews.py`), manual namespace builder + router-side manual order stamping, `/api/broker/account-truth`, `/orders/completed`, and `/orders/what-if`. Account Monitor now shows Account Truth status/owners/exposure, Reconciliation shows invariant verdicts/blockers/caveats, and Orders is a ledger over account-truth open+completed orders with manual submit requiring what-if preview. Follow-ups remain: adoption ledger/workflow, Flex import, Client Portal evaluation, and operator-specific manual namespace attribution. |
| 2026-07-01 | Codex GPT-5 | Account Truth registry attribution update: `/account-truth` now reads the durable account instance registry instead of the in-process publisher list, exposes `owner_binding_state`, uses retired bindings for terminal attribution, and emits `retired_owner_live_exposure` when a retired bot still owns a live working order or current position. |
| 2026-07-03 | Codex GPT-5 | Account Truth readiness-cache update: `/account-truth` stores the latest projection in `services/account_truth_snapshot.py`; `/api/live-instances/{id}/status` reads that cache by account id and folds missing, stale, or `not_proven` Account Truth into `operator_surface.submit_readiness` as `broker_state_unproven` with `ACCOUNT_TRUTH_*` reason codes. |
| 2026-07-03 | Codex GPT-5 | Account Truth submit-gate update: `LivePortfolio.submit_pending_orders` now consumes the cached projection through the `account.account_truth` `GateResult` and blocks missing, stale, or not-clean Account Truth before any broker call or AccountOwner handoff. The gate performs no IBKR sweep and does not mutate freeze/registry artifacts. |
| 2026-05-04 | Claude Opus 4.7 | Initial authority doc post PR #76, #77, #78. Captures Phase 1-7 live runtime, Diagnose endpoint + button, Phase 10 prereqs (`open_` fallback, force-flat, exit-side collapse). |
| 2026-05-12 | Claude Opus 4.7 | Phase 8 hardening landed on `feat/ibkr-paper-runner-hardening`: `LiveEngine.shutdown_event` graceful exit, SIGINT/SIGTERM handlers in `cmd_start`, rotating file logger (`run_logging.py`, 10MB × 5 backups), unhandled-exception recovery flatten, `IbkrClient.connect()/disconnect()` wired. Phase 8 row in § 11 flipped from STUB → SHIPPED. Latent CLI bug (CLI never connected the client) fixed in the same commit. Deferred to follow-ups: equity-curve parquet writer, YAML config input, `LiveConfig` → `BaseSettings`. |
| 2026-05-13 | Claude Opus 4.7 | Doc-rot refresh — three-way Phase 9 reconciliation pipeline (`reconcile.py`, per the 2026-05-08 shadow-deployment spec § 6) had shipped but this page still listed it as a stub. Updated § 6 surface table (`run.py`/`reconcile.py`/`run_logging.py`/`pre_flight.py`/`run_ledger.py` rows), § 6 force-flat residual paragraph (replaced "(when shipped)" with the actual classifier), § 11 status row (STUB → SHIPPED with note on deployment-plan's older paper-vs-backtest framing being superseded), and § 12 operational checklist item 7 (reconcile CLI command). Also surfaced the smoke-discovered `IbkrClient.disconnect()` latent bug (`ib_async.IB.disconnect` is synchronous; the code awaited a non-existent `disconnectAsync`) — fix landed in PR #225 commit `34ea0a1` and is regression-tested. |
| 2026-05-28 | Claude Opus 4.7 | **Historical design-lock round** for the persistent paper-trading bot architecture and shadow VWAP onboarding ([archived plan](archive/plans/ibkr-paper-deployment-plan.md) § 16). Its former direct-broker emergency and command-channel statements are superseded for current operation by ADR-0030, ADR-0026, and the Bot Control manual. |
| 2026-05-13 | Claude Opus 4.7 | Post-PR #229/#230/#231 refresh after a focused operator-path session. **PR #229** added the per-minute `[BAR]` heartbeat to `LiveEngine.run` so operators can distinguish "engine alive, strategy in indicator warmup" from "engine hung" — closes the issue #227 misdiagnosis. **PR #230** corrected three runbook bugs: dropped `client_id` from the `--live-config-json` example (rejected by `_live_config_from_ledger`), flipped Step 3 from container-side to host-venv (IBKR error 420 same-IP-binding makes container-side `start` impossible), added a Windows asyncio note for `loop.add_signal_handler` no-op fallback, and preserved the spec § 5 single-client invariant by stopping the container before Step 3. **PR #231** introduced `_next_bar_or_shutdown` so SIGINT unwedges the engine within bounded time even when the bar source is silent (Gateway stall, market halt, IP-binding rejection); follow-up commit ensures source-side exceptions propagate even when shutdown is concurrent (broker errors are no longer masked by graceful exit). § 6 surface table, bar-loop step list, and graceful-shutdown subsection updated. § 11 Phase 8 row notes the new heartbeat / wedge-fix / exception-propagation; Phase 10 row now points at four explicit prereq rows (full-RTH dry-run pass, `commissionReport` callback, `equity_curve.parquet` writer, indicator-state-persistence) plus an end-to-end producer-test gap. § 12 item 7 rewritten for the host-venv operator path. § 13 cross-reference updated. |
