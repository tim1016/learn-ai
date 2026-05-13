# IBKR Integration — Authority

> **Canonical reference** for what the Interactive Brokers integration
> ships today. Source-of-truth implementation snapshot, not a design
> document — when this page disagrees with code, the code is right and
> this page must be updated in the same PR.
>
> **Sibling docs** (different jobs, do not duplicate):
> - [`architecture/ibkr-integration-tdd.md`](architecture/ibkr-integration-tdd.md) — design rationale (why we chose `ib_async`, four-layer paper safety, SSE everywhere). Read first to understand "why."
> - [`architecture/ibkr-integration-phase{1,2,3}.md`](architecture/) — frozen snapshots of what each integration phase shipped.
> - [`ibkr-paper-deployment-plan.md`](ibkr-paper-deployment-plan.md) — Phase 6/7 replay-parity plan and Phase 8/9/10 paper-week roadmap.
> - [`codex-phase-1-4-audit.md`](codex-phase-1-4-audit.md) — most recent code audit; tracks Phase 10 prereqs.
>
> **Owner:** the engineer editing `PythonDataService/app/broker/ibkr/*` or `PythonDataService/app/engine/live/*`. Same-PR rule: if you touch those files, update the matching section here and bump **Last reviewed**.
>
> **Last reviewed:** 2026-05-13 (post PR #229 — `[BAR]` per-minute heartbeat + runbook warmup expectations; PR #230 — three runbook operator-path corrections (host-venv flip, single-client invariant, Windows signal note); PR #231 — `_next_bar_or_shutdown` helper unwedges SIGINT when bar source is silent + propagates source exceptions during shutdown. See change log for the full session.).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Architecture map](#2-architecture-map)
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

**Authority precedence** when this doc, the TDD, and the code disagree: code wins, then this doc, then the TDD. The TDD captures design intent which can be older than the implementation; this doc is updated on every PR that touches the integration.

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

---

## 3. Configuration and four-layer paper safety

Settings live in `app/broker/ibkr/config.py:IbkrSettings`, env-prefixed `IBKR_`. Loaded from a `.env` file at repo root. Singleton — instantiated once via `get_settings()`.

| Field | Default | Notes |
|---|---|---|
| `mode` | `paper` | `paper` or `live`. Default refuses to drift to live. |
| `host` | `auto` | `auto` resolves the container default gateway via `/proc/net/route`; literal IP or hostname accepted. |
| `port` | `4002` | Paper Gateway. Validated against `mode` — see Layer 2 below. |
| `client_id` | `1` | Reserved for the FastAPI lifespan client; later phases (recorder, etc.) get higher IDs. |
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

Twelve files. Public surface only — private helpers (prefix `_`) and the `models.py` Pydantic shapes are not in this table.

| Module | Public surface | Purpose |
|---|---|---|
| `client.py` | `IbkrClient`, `get_client`, `set_client`, `BrokerError`, `ConnectionRefusedDueToSentinelError`, `NotConnectedError` | `ib_async.IB` lifecycle wrapper. Owns the singleton client. Layers 1+3 of paper safety. |
| `config.py` | `IbkrSettings`, `get_settings`, `reset_settings_for_testing`, `PAPER_PORTS`, `LIVE_PORTS` | Env-var-backed settings + Layer 2. |
| `contracts.py` | `qualify_underlying`, `list_expirations`, `list_strikes`, `build_option_contract`, `list_qualified_strikes`, `build_chain_contracts`, `expiry_ms_to_yyyymmdd`, `yyyymmdd_to_expiry_ms` | Stock + Option contract resolution. SMART/USD only. |
| `bars.py` | `aggregate_realtime_bar`, `stream_minute_bars`, `IBKRBarStreamError`, `IbkrMinuteBar` (model) | 5-second TRADES → closed 1-minute bar aggregation. Fail-fast on duplicate or non-monotonic timestamps. |
| `market_data.py` | `stream_option_chain` | `reqMktData` with generic ticks `100,101,106` → debounced `IbkrChainSnapshot` SSE. Greeks selection: `model > bid > ask > last > none`. |
| `account.py` | `fetch_account_summary`, `fetch_positions` | One-shot reads of NLV / cash / margin / per-position state. |
| `orders.py` | `place_paper_order`, `list_open_orders`, `cancel_paper_order`, `stream_order_events`, `OrderRefusedError`, `OrderNotFoundError` | Layers 0+4 of paper safety. Idempotency cache keyed on `client_order_id` (process-local, not durable). Polling-based event stream (default 0.5s). |
| `pnl.py` | `stream_account_pnl`, `stream_position_pnl`, `DEFAULT_PNL_DEBOUNCE_S` | `reqPnL` / `reqPnLSingle` → debounced `IbkrPnLTick` SSE. |
| `persistence.py` | `TickWriter`, `make_writer`, `AccountSnapshotWriter`, `make_account_writer`, `PnLTickWriter`, `make_pnl_writer` (+ Noop / Parquet implementations) | Optional Parquet archive of ticks / snapshots / P&L. Off by default. |
| `diagnostics.py` | `run_diagnostics` | Self-test for the connection chain (8 checks). See §9. |
| `models.py` | `IbkrAccountSummary`, `IbkrPositionsSnapshot`, `IbkrPosition`, `IbkrOptionQuote`, `IbkrChainSnapshot`, `IbkrStrikeList`, `IbkrMinuteBar`, `IbkrOrderSpec`, `IbkrOrderAck`, `IbkrOpenOrder`, `IbkrOrderEvent`, `IbkrPnLTick`, `IbkrConnectionHealth`, `DiagnosticCheck`, `DiagnosticReport` | Pydantic v2 wire models. **Every** boundary timestamp is `int64` ms UTC. NaN / `-1` IBKR sentinels become `None` via `_coerce_optional_float` / `_coerce_iv`. |
| `__init__.py` | (re-exports) | Curated entry points only. |

---

## 5. REST + SSE endpoints (`/api/broker/*`)

All routes live in `app/routers/broker.py`, prefix `/api/broker`, tag `broker`.

| Method | Path | Type | Response model | Purpose |
|---|---|---|---|---|
| GET | `/health` | one-shot | `IbkrConnectionHealth` | Connection diagnostic. **Never raises** on disconnect; returns `connected=false` so the UI can render the disconnected state. |
| GET | `/diagnose` | one-shot | `DiagnosticReport` | 8-check self-test (see §9). |
| GET | `/account` | one-shot | `IbkrAccountSummary` | Cash, NLV, margin, account-level P&L. Optionally persisted via `persist_account`. |
| GET | `/positions` | one-shot | `IbkrPositionsSnapshot` | Open positions across all symbols. |
| GET | `/expirations/{symbol}` | one-shot | `dict` | All listed option expiries for a symbol, `int64 ms UTC`. |
| GET | `/strikes/{symbol}?expiry_ms=...` | one-shot | `IbkrStrikeList` | Strikes IBKR can actually qualify (call ∩ put). |
| GET | `/option-chain/{symbol}` | SSE | `IbkrChainSnapshot` (per event) | Streaming option chain — debounced (default 250 ms). |
| GET | `/pnl/stream` | SSE | `IbkrPnLTick` | Account-level P&L. Debounced (default 1 s). |
| GET | `/pnl/positions/stream?con_ids=...` | SSE | `IbkrPnLTick` | Per-position P&L for the requested contract IDs. |
| POST | `/orders` | one-shot (201) | `IbkrOrderAck` | Place a paper order. Layer 0+4 enforced; idempotent via `client_order_id`. |
| GET | `/orders/open` | one-shot | `list[IbkrOpenOrder]` | Currently-open orders the broker still tracks. |
| DELETE | `/orders/{order_id}` | one-shot | `IbkrOpenOrder` | Cancel a paper order. Refuses if mode is not paper or account is not DU. |
| GET | `/orders/stream` | SSE | `IbkrOrderEvent` | Order lifecycle events. Polling-based (0.5 s default); status transitions can collapse — see §6. |

**SSE format**: every event line is JSON-encoded payload prefixed with `data: `. The frontend uses `EventSource` via `Frontend/src/app/services/broker-sse.ts`. The TDD §3.5 explains why SSE rather than WebSocket.

**Timestamp policy**: every payload field literally named `timestamp`, `ts`, `time`, `*_ms`, or `*_at_ms` is `int64 ms UTC`. `string` / `DateTime` typing on these fields is banned at the model layer. See [`numerical-rigor.md`](../.claude/rules/numerical-rigor.md) "Timestamp rigor."

---

## 6. Live runtime (`app/engine/live/`)

The runtime that turns a `Strategy` into paper orders against IBKR.

### Surface

| File | What ships |
|---|---|
| `config.py` | `LiveConfig` dataclass — `symbol`, `force_flat_at: time \| None = time(15, 55)`, `consolidator_period_min`, `run_dir`, `max_submit_latency_ms`. |
| `live_portfolio.py` | `LivePortfolio` — Portfolio-shaped surface with broker-backed account snapshots; `set_holdings`, `liquidate`, `submit_pending_orders`, `record_broker_fill`, `cancel_open_orders`. `BrokerAdapter` Protocol + `IbkrBrokerAdapter` (production) implementation. |
| `live_context.py` | `LiveContext` — drop-in for `StrategyContext`. Reuses `TradeBarConsolidator` verbatim. Plumbs consolidated-bar close through to `LivePortfolio` reference price. |
| `live_engine.py` | `LiveEngine`, `LiveRunResult`, `ReplayBrokerAdapter` Protocol. Per-minute bar loop driven by `_next_bar_or_shutdown` (PR #231) — races `source.__anext__()` against `shutdown_event.wait()` so SIGINT unwedges within bounded time even when the bar source is silent (Gateway stall, market halt, IP-binding rejection). Emits a `[BAR]` heartbeat per bar (PR #229) for operator-visible aliveness during the strategy's indicator warmup window. Eager four-layer paper-safety validation when an `IbkrClient` is supplied. |
| `run.py` | Operator CLI. Four subcommands: `init-ledger` (writes the run identity at `artifacts/live_runs/<run_id>/run_ledger.json`), `pre-flight` (runs the 7 morning halt checks in `pre_flight.py`), `start` (wires `shutdown_event` + SIGINT/SIGTERM handlers + rotating file logger + unhandled-exception recovery flatten + `IbkrClient.connect()/disconnect()`), `emergency-flatten`. |
| `reconcile.py` | Three-way daily reconciler — Python live ↔ QC Cloud ↔ IBKR fills. Per-bar `CrossEngineClass` (none/data/engine) and `FillClass` (none/within_tol/breach) classifications; writes `day-N.{md,json,parquet,hashes.json}`; emits `halt.flag` consumed by next morning's `check_no_halt_flag` pre-flight; SHA-256 manifest in the committed Markdown receipt. Implemented per `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` § 6. |
| `run_logging.py` | Rotating file logger (`<run-dir>/live.log`, 10MB × 5 backups) plus console handler with `[STEP X]` formatting when callers pass `extra={"step": ...}`. Idempotent for repeat init on the same run-dir. |
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
| `/broker/account-monitor` | `BrokerAccountMonitorComponent` | Account summary + per-position P&L SSE. | Locked unless connected. |
| `/broker/orders` | `BrokerOrdersComponent` | Place / cancel paper orders, open-orders table, order-event SSE. Native confirmation dialog before submit. | Locked unless paper-connected (defense-in-depth on the four-layer safety). |
| `/broker/reconciliation` | `BrokerReconciliationComponent` | Reconciliation table — broker truth vs engine view. | Locked unless connected. |

**Shared services**:

- `BrokerHealthService` — singleton 5-second poll of `/api/broker/health`. Exposes `health`, `bannerState`, `isPaperConnected` signals. The shell paper/live/disconnected banner reads from this.
- `BrokerService` — `firstValueFrom`-wrapped REST client for the non-SSE endpoints.
- `broker-sse.ts` — `EventSource` helper that each SSE-consuming page owns explicitly (no global SSE manager).

**Type generation**: REST-shaped models in `Frontend/src/app/api/broker.types.ts` are regenerated from the Python service's OpenAPI spec. SSE-only payloads (`IbkrChainSnapshot`, `IbkrPnLTick`, `IbkrOrderEvent`) and recently-added types (`DiagnosticReport`) are hand-mirrored in `broker-models.ts` until the next regeneration. See `Frontend/AGENTS.md` for the regenerate command.

---

## 8. Persistence

Optional Parquet archive of three streams. **Off by default** — flip individual flags only when forensic queries are needed.

| Stream | Setting | Path | Schema |
|---|---|---|---|
| Option ticks | `IBKR_PERSIST_TICKS=true` | `{persist_dir}/{date}/{symbol}.parquet` | `IbkrOptionQuote` columns + `as_of_ms`. One file per underlying symbol. |
| Account snapshots | `IBKR_PERSIST_ACCOUNT=true` | `{persist_dir}/{date}/account.parquet` | `IbkrAccountSummary` columns. |
| P&L ticks | `IBKR_PERSIST_PNL=true` | `{persist_dir}/{date}/pnl_{account_id}.parquet` | `IbkrPnLTick` columns; account-level rows have `con_id=NULL`, per-position rows carry the contract id. One file per IBKR account. |

Writers are factories — `make_writer` / `make_account_writer` / `make_pnl_writer` return either a Noop or a real Parquet writer based on the flag. Endpoints offer every snapshot to the configured writer; the writer flushes on close.

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

As of 2026-05-04 (post PR #78):

| Area | File | Tests |
|---|---|---|
| **Broker module — 107 tests** | | |
| | `tests/broker/ibkr/test_account.py` | 9 |
| | `tests/broker/ibkr/test_bars.py` | 7 (incl. `open_` regression from PR #78) |
| | `tests/broker/ibkr/test_client.py` | 14 |
| | `tests/broker/ibkr/test_config.py` | 8 |
| | `tests/broker/ibkr/test_contracts.py` | 8 |
| | `tests/broker/ibkr/test_market_data.py` | 10 |
| | `tests/broker/ibkr/test_models.py` | 5 |
| | `tests/broker/ibkr/test_orders.py` | 18 |
| | `tests/broker/ibkr/test_persistence.py` | 5 |
| | `tests/broker/ibkr/test_pnl.py` | 7 |
| | `tests/broker/ibkr/test_router.py` | 16 |
| **Live runtime — 15 tests** | | |
| | `tests/engine/live/test_live_context.py` | 5 |
| | `tests/engine/live/test_live_engine.py` | 3 (incl. force-flat fire + no-fire from PR #78) |
| | `tests/engine/live/test_live_engine_collapse.py` | 2 (entry- + exit-side from PR #78) |
| | `tests/engine/live/test_live_engine_replay.py` | 1 (HARD GATE; skipped on CI when `lean-cache/` absent) |
| | `tests/engine/live/test_live_portfolio.py` | 4 |

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
| Phase 10 — actual paper week + reconciliation report | NOT STARTED | Operational; gated on the **Phase 10 prereqs** rows below. Earliest start is gated on (a) one full-RTH end-to-end dry-run pass that produces a populated `decisions.parquet` against a real IBKR Gateway, plus (b) the three small writer/state PRs called out below. |
| Phase 10 prereq — full-RTH end-to-end dry-run pass | NOT YET RUN | The longest live-Gateway session attempted on 2026-05-13 was 30 min (container, then 20 min host-side). Neither produced `decisions.parquet` because indicator warmup didn't complete (≥ 3 h 45 m). We have not yet observed the full pipeline `init-ledger → pre-flight → start --readonly → reconcile` end-to-end on a single trading day, so the writer-schema + reconcile-loader contract is unverified against real artifacts. Operator action: start by 05:45 ET, run through 16:00 ET force-flat, run reconcile Step 4 with synthetic QC. ~10.5 h wall clock. |
| Phase 10 prereq — `commissionReport` callback wiring | NOT SHIPPED | Real IBKR fills currently record `fee=0` because the `commissionReport` event isn't subscribed in `IbkrBrokerAdapter`. Reconcile receipts will look misleadingly clean on commissions until this lands. Without it, the Phase 10 receipt cannot honestly compare commission against the QC backtest. |
| Phase 10 prereq — `equity_curve.parquet` writer | NOT SHIPPED | `equity_curve` lives in `LiveRunResult` in memory; `artifacts.py` has writers for decisions / executions / trades but no `EquityWriter`. Reconcile cannot compare equity-over-time against QC's equity series. |
| Phase 10 prereq — indicator-state-persistence across restarts | NOT SHIPPED | Every fresh-state run burns ≥ 3 h 45 m on warmup. Without persistence, day 2+ of paper week starts from scratch unless the operator re-starts pre-warmup. Persists EMA/RSI internals to a JSON sidecar on graceful shutdown; `LiveContext` restores from it on init. |
| Phase 10 prereq — end-to-end producer test (LiveEngine → reconcile) | NOT SHIPPED | All 25 reconcile unit tests use synthetic parquets. No CI test proves `LiveEngine`'s artifact writers (`DecisionRow`/`ExecutionRow`/`TradeRow`) match what `reconcile.load_python_decisions` / `load_python_executions` expect. A small producer-consumer integration test (run a minimal LiveEngine session against `FakeBroker`, assert reconcile's loaders parse the resulting parquets) would close the contract. |
| Phase 10 prereq — broker-truth identity in `executions.parquet` | NOT SHIPPED | `_write_execution` (`live_engine.py:678-690`) currently synthesizes `exec_id=f"engine-{order_id}"`, `perm_id=int(order_id)`, `client_order_id=f"live-{order_id}"` from the engine's own order_id rather than IBKR's `execDetailsEvent` primary keys. The docstring acknowledges this ("Until Phase C-2c surfaces those through a dedicated execution channel, we synthesize stable identifiers"). Three-way reconciliation against IBKR's actual exec records cannot match against synthesized identifiers, and foreign fills are dropped at `_convert_ibkr_fill` (`live_engine.py:1001-1009`) instead of being persisted as broker-truth-without-runner-ownership. Required for Phase 9's stated Python ↔ QC ↔ IBKR exec reconciliation to hold against real broker primary keys. |
| `IbkrMinuteBar` → `TradeBar` conversion in `stream_minute_bars` consumer | **SHIPPED** | `live_engine.py:259` wraps `stream_minute_bars(client, symbol)` in `trade_bars_from_ibkr` (`app/engine/live/bar_adapter.py`) which yields `TradeBar`. The async-generator adapter is exercised on every real-IBKR run. (PR #76 review C1 row, originally tracked; the conversion adapter shipped with the bar-adapter module — flagged as stale in code-review pass 2026-05-13.) |
| `client_order_id` per-session uniqueness | TRACKED | Counter resets per `LivePortfolio`; `place_paper_order` idempotency cache is process-scoped. PR #76 review C2 — Phase 10 prereq. |
| `IbkrMinuteBar` `model_validator` for `end_ms == start_ms + 60_000` and `volume >= 0` | TRACKED | Defensive; unenforced today. PR #76 review R5. |
| `LiveEngine.run()` guard for `bars=None` and `client=None` | TRACKED | Currently passes `None` to `stream_minute_bars` if both are absent; should fail fast. PR #76 review R7. |
| `[STEP X]` structured logging in `LiveEngine` | PARTIAL | `run_logging.configure_run_logging` supports `[STEP X]` via `extra={"step": ...}`; `cmd_start` and the new shutdown/recovery paths use it. Pre-existing log calls inside `LiveEngine.run` still need migration; tracked as a sweep. |
| Single-account FA support | NOT SUPPORTED | `client.py:201-208` refuses to connect on >1 managed account. |
| 2FA mid-session | OUT OF SCOPE | TDD §6 risk register. Operator handles via Gateway settings. |
| Order ID persistence across restarts | NOT SHIPPED | `.live_state.json` is in the plan §10 hygiene tasks but unimplemented. Postgres-based persistence is a separate ticket because there is no migrations workflow yet. |

---

## 12. Operational checklist (paper week pre-flight)

Run these in order before turning the runner loose:

1. **`.env`** has `IBKR_MODE=paper`, `IBKR_PORT=4002` (or `7497` for TWS), `IBKR_READONLY=false`, paper account `DU…` ID. Anything else and the four layers will refuse.
2. **NYSE/ARCA real-time market-data subscription** active on the linked live account. Paper inherits — see TDD §2.4.
3. **IB Gateway** running, logged into the paper account, API tab has the container IP under "Trusted IPs," and "Read-Only API" is OFF.
4. **`GET /api/broker/health`** returns `connected: true, is_paper: true` and the account ID begins with `DU`.
5. **`GET /api/broker/diagnose`** returns `overall_status: pass` (or click the **Diagnose** button on `/broker`).
6. **Project-scope tests** green: `pytest PythonDataService/tests/ -k "not slow"`. 1797+ pass; the replay parity test must skip with the `lean-cache` message on a clean CI runner or pass locally where the cache is materialized.
7. **Operator path** runs entirely from the **host venv** (PR #230). The `start` subcommand cannot be run inside the `polygon-data-service` container — IBKR error 420 ("Trading TWS session is connected from a different IP address") rejects `reqRealTimeBars` whenever the API client's source IP differs from the Gateway's login IP, and the container always fails this check from the WSL bridge subnet. Stop the container before Step 3 to free `client_id=42` for the dry-run client. End-to-end operator steps are in [`docs/runbooks/ibkr-paper-dry-run.md`](runbooks/ibkr-paper-dry-run.md). Tail `live.log` for the per-minute `[BAR]` heartbeat to confirm bars are flowing — `decisions.parquet` stays empty during the ≥ 3 h 45 m indicator warmup window, but `[BAR]` lines appearing every minute prove the engine is alive (PR #229; see issue #228 / #227 for the misdiagnosis this prevents).

If any of these fails, fix it before running. The diagnostic endpoint will tell you which layer is the blocker.

---

## 13. Code cross-reference

| Concept | Files | Notes |
|---|---|---|
| Paper safety boundary | `app/broker/ibkr/{config,client,orders}.py` | Layers 0-4 in §3. |
| Boundary timestamp policy | `app/broker/ibkr/models.py` (`int64 ms UTC` everywhere) | See `.claude/rules/numerical-rigor.md` "Timestamp rigor." |
| Strategy contract | `app/engine/strategy/base.py` (unchanged for live) | `LiveContext` mirrors `StrategyContext`. |
| Backtest engine | `app/engine/engine.py` | The replay parity gate runs both engines from the same data source. |
| Live engine | `app/engine/live/live_engine.py` | Per-bar loop driven by `_next_bar_or_shutdown` (race-helper from PR #231); per-minute `[BAR]` heartbeat (PR #229); force-flat from PR #78. |
| Replay parity gate | `tests/engine/live/test_live_engine_replay.py` | `Decimal("0")` tolerance; CI skips when `lean-cache/` absent. |
| Frontend gate | `Frontend/src/app/services/broker-health.service.ts` (`isPaperConnected`) | Defense-in-depth at form level. |

---

## Change log

| Date | Reviewer | Notes |
|---|---|---|
| 2026-05-04 | Claude Opus 4.7 | Initial authority doc post PR #76, #77, #78. Captures Phase 1-7 live runtime, Diagnose endpoint + button, Phase 10 prereqs (`open_` fallback, force-flat, exit-side collapse). |
| 2026-05-12 | Claude Opus 4.7 | Phase 8 hardening landed on `feat/ibkr-paper-runner-hardening`: `LiveEngine.shutdown_event` graceful exit, SIGINT/SIGTERM handlers in `cmd_start`, rotating file logger (`run_logging.py`, 10MB × 5 backups), unhandled-exception recovery flatten, `IbkrClient.connect()/disconnect()` wired. Phase 8 row in § 11 flipped from STUB → SHIPPED. Latent CLI bug (CLI never connected the client) fixed in the same commit. Deferred to follow-ups: equity-curve parquet writer, YAML config input, `LiveConfig` → `BaseSettings`. |
| 2026-05-13 | Claude Opus 4.7 | Doc-rot refresh — three-way Phase 9 reconciliation pipeline (`reconcile.py`, per the 2026-05-08 shadow-deployment spec § 6) had shipped but this page still listed it as a stub. Updated § 6 surface table (`run.py`/`reconcile.py`/`run_logging.py`/`pre_flight.py`/`run_ledger.py` rows), § 6 force-flat residual paragraph (replaced "(when shipped)" with the actual classifier), § 11 status row (STUB → SHIPPED with note on deployment-plan's older paper-vs-backtest framing being superseded), and § 12 operational checklist item 7 (reconcile CLI command). Also surfaced the smoke-discovered `IbkrClient.disconnect()` latent bug (`ib_async.IB.disconnect` is synchronous; the code awaited a non-existent `disconnectAsync`) — fix landed in PR #225 commit `34ea0a1` and is regression-tested. |
| 2026-05-13 | Claude Opus 4.7 | Post-PR #229/#230/#231 refresh after a focused operator-path session. **PR #229** added the per-minute `[BAR]` heartbeat to `LiveEngine.run` so operators can distinguish "engine alive, strategy in indicator warmup" from "engine hung" — closes the issue #227 misdiagnosis. **PR #230** corrected three runbook bugs: dropped `client_id` from the `--live-config-json` example (rejected by `_live_config_from_ledger`), flipped Step 3 from container-side to host-venv (IBKR error 420 same-IP-binding makes container-side `start` impossible), added a Windows asyncio note for `loop.add_signal_handler` no-op fallback, and preserved the spec § 5 single-client invariant by stopping the container before Step 3. **PR #231** introduced `_next_bar_or_shutdown` so SIGINT unwedges the engine within bounded time even when the bar source is silent (Gateway stall, market halt, IP-binding rejection); follow-up commit ensures source-side exceptions propagate even when shutdown is concurrent (broker errors are no longer masked by graceful exit). § 6 surface table, bar-loop step list, and graceful-shutdown subsection updated. § 11 Phase 8 row notes the new heartbeat / wedge-fix / exception-propagation; Phase 10 row now points at four explicit prereq rows (full-RTH dry-run pass, `commissionReport` callback, `equity_curve.parquet` writer, indicator-state-persistence) plus an end-to-end producer-test gap. § 12 item 7 rewritten for the host-venv operator path. § 13 cross-reference updated. |
| 2026-05-13 | Claude Opus 4.7 | Code-review-pass corrections (Tim review on this PR). **§ 8 persistence paths corrected**: option ticks write per-symbol `{symbol}.parquet` (not a single `ticks.parquet`); P&L ticks write per-account `pnl_{account_id}.parquet` (not a single `pnl.parquet`). Account-snapshot path was correct. **§ 11 `IbkrMinuteBar` → `TradeBar` row flipped TRACKED → SHIPPED**: `live_engine.py:259` wraps `stream_minute_bars` in `trade_bars_from_ibkr` and the adapter has been live since the bar-adapter module shipped. **New § 11 Phase 10 prereq row added — broker-truth identity in `executions.parquet`**: `_write_execution` synthesizes `exec_id`/`perm_id`/`client_order_id` from the engine's own order_id rather than IBKR's `execDetailsEvent` primary keys (docstring 670-676 acknowledges as Phase C-2c gap). Three-way reconciliation against IBKR truth cannot resolve against synthesized identifiers, so this is a hard prereq for Phase 9's stated reconcile claim. |
