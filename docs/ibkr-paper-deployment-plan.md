# IBKR Paper-Trading Deployment Plan — `SpyEmaCrossoverAlgorithm`

**Status:** Draft v2, post-Codex review. Codex-executable.

> **Implementation note (2026-05-04):** the live runtime now ingests
> `IbkrMinuteBar` through `app/engine/live/bar_adapter.py`, drains real
> IBKR fills via `IbkrBrokerAdapter.start_event_stream` (translated to
> engine `OrderEvent` in `LiveEngine._convert_ibkr_fill`), and scopes
> `cancel_open_orders` to the runner's own `_owned_order_ids` so we
> never touch unrelated open orders on the paper account. Commission
> reconciliation (IBKR's `commissionReport` callback) and the Phase 8
> CLI / Phase 9 reconcile script are still pending — `fee=0` on
> real-broker fills until Phase 9 wires the commission callback.

**Owner:** to be assigned.

**Goal:** Run the existing `SpyEmaCrossoverAlgorithm` against an Interactive Brokers **paper** account through a new live runtime that shares the strategy class with the backtest, and produce a reconciliation report (paper trades vs same-window backtest trades) as the stage-2 parity receipt.

**Out of scope:** real-money trading (Phase 4 in `docs/architecture/ibkr-integration-tdd.md` §7), multi-symbol live, options live (`spy_ema_crossover_options.py`), Angular Engine Lab GUI integration.

**Companion documents:**
- [`architecture/ibkr-integration-tdd.md`](architecture/ibkr-integration-tdd.md) — **read this first.** The broker boundary already exists; this plan layers on top.
- [`docs/archive/plans/ibkr-integration-phase3.md`](archive/plans/ibkr-integration-phase3.md) — order placement / cancel / event-stream details (archived phase snapshot)
- [`docs/archive/plans/engine-tv-alignment-roadmap.md`](archive/plans/engine-tv-alignment-roadmap.md) — RTH filter / warmup fixes that must be in place before paper (archived)
- [`audits/computational-fidelity-2026-04-22.md`](audits/computational-fidelity-2026-04-22.md) — timestamp / fill-model gotchas
- [`math-sources-of-truth.md`](math-sources-of-truth.md) — registry to receive a `live-runtime` row
- [`ibkr-paper-deployment-feedback.md`](ibkr-paper-deployment-feedback.md) — Codex's review of v1, used to produce this v2

---

## Revision history

**v2** (this revision) incorporates Codex's review:

- **Removed all duplicate broker-client work.** v1 specified a parallel `app/engine/live/ibkr_client.py`. The repo already has `app/broker/ibkr/{client,config,orders,account,...}` with a four-layer paper-safety boundary, idempotency cache, polling-based event stream, and `int64 ms UTC` enforced. This plan now reuses that boundary verbatim.
- **Updated `ib_async` version** from the stale `>=1.0,<2.0` to the actual pinned `>=2.0,<3.0` already in `requirements-light.txt`.
- **Dropped the 0.10 sizing cap.** The receipt runs at the strategy's native `set_holdings(SPY, 1.0)`. Codex's argument: changing target fraction changes the integer `target_quantity = int(target_value / price)` path, so capping doesn't preserve parity, it breaks it differently. If a real paper week needs a cap for risk reasons, regenerate a separate capped-baseline backtest and call out the cap in the reconciliation report.
- **Tightened replay parity tolerance to exact.** v1 allowed `atol=Decimal("0.01")` on fill price. Codex was right that this hides bugs in the fake-client gate, where both engines consume identical Decimal OHLCV. Cent tolerance is reserved for real-broker reconciliation only. Replay assertions also expanded — see Phase 6.
- **Added a new "broker-lifecycle-collapse" test.** Codex flagged a failure mode the original replay test misses: the polling-based `stream_order_events` (`orders.py:433`) can collapse rapid status transitions. A replay-only gate cannot exercise this. New Phase 7 covers it.
- **Locked the open questions:**
  - Insight scoring cadence: per-minute, matches backtest exactly.
  - Reference price for `set_holdings`: consolidated bar close, matches backtest exactly.
- **Added cross-cutting hygiene phase:** `.gitignore` for `live_runs/` and `.live_state.json`, `int64 ms UTC` enforcement on every run artifact, drop the `mypy --strict` gate (aspirational — no service-root `pyproject.toml`).

**v1** (initial draft) is preserved in git history.

---

## 0. Assumptions to refine before execution

The following are the v2 defaults, all reviewed by Codex. Override before handing to Codex if any are wrong.

| # | Assumption | Why this default |
|---|---|---|
| 1 | **Build a Python `LiveEngine` adapter inside `app/engine/live/`**, not a LEAN-via-IBKR deployment | Repo philosophy (`CLAUDE.md`, `math-sources-of-truth.md`): references are studied and eliminated as runtime dependencies. Python engine is canonical. |
| 2 | **Reuse `app/broker/ibkr/IbkrClient`** from `requirements-light.txt`'s `ib_async>=2.0,<3.0`. Do not wrap, duplicate, or shadow it. | The four-layer paper safety, account sentinel, idempotency, and SSE patterns are already shipped (Phase 1+2+3 of the IBKR integration). A second wrapper would split the safety boundary. |
| 3 | **SPY only, single symbol, paper port only** in v1 | Matches `BacktestEngine.run`'s explicit `Phase 1 single symbol only` (`engine.py:182-184`). Closes the surface. |
| 4 | **`FillMode.NEXT_BAR_OPEN` is the replay parity target.** Real IBKR paper fills are reconciled against that baseline, not held to strict fill-price parity. | Closest backtest fill model to broker behavior (`fill_model.py:77-81`). Live fills come on next-print-after-submission; the residual gap is exactly what stage 2 measures. |
| 5 | **Hard guard against live port** + four-layer safety from `app/broker/ibkr/orders.py`. If loosened later, a separate `run_live.py` runner with its own config profile, not a flag on the paper runner. | The existing `_enforce_paper_safety` (`orders.py:76-138`) is already paranoid: env-var mode, port validator, `DU` account sentinel, `confirm_paper=true` per request. Reuse it. |
| 6 | **Run the receipt at strategy-native `set_holdings(SPY, 1.0)`**. If a real paper week needs a sizing cap for account-risk reasons, the cap goes into a parallel capped-baseline backtest, *not* the receipt run. | Codex feedback §6: changing the target fraction changes integer share count (`portfolio.py:155-168`); capping doesn't preserve parity, it just breaks it differently. |

---

## 1. Architecture

```
                              ┌──────────────────────┐
                              │  Strategy            │  ← UNCHANGED
                              │  (SpyEmaCrossover)   │
                              └──────┬───────────────┘
                                     │ uses
                      ┌──────────────┴──────────────┐
                      │                              │
           ┌──────────▼──────────┐         ┌────────▼───────────┐
           │ StrategyContext      │         │ LiveContext         │  ← NEW
           │ + Portfolio          │         │ + LivePortfolio     │  ← NEW
           │ (existing, sim)      │         │ (in app/engine/live)│
           └──────────┬───────────┘         └────────┬────────────┘
                      │                              │
           ┌──────────▼──────────┐         ┌────────▼────────────┐
           │  BacktestEngine     │         │  LiveEngine         │  ← NEW
           │  (existing)         │         │  asyncio driver     │
           │  for-loop driver    │         │                     │
           └──────────┬──────────┘         └────────┬────────────┘
                      │                              │
           ┌──────────▼──────────┐         ┌────────▼────────────┐
           │ LeanMinuteDataReader│         │  app.broker.ibkr.*  │  ← EXISTING
           │ FillModel           │         │  ┌────────────────┐ │
           └─────────────────────┘         │  │ IbkrClient     │ │
                                           │  │ orders.*       │ │
                                           │  │ account.*      │ │
                                           │  │ config.*       │ │
                                           │  │ market_data.*  │ │
                                           │  │ + bars.py      │ │  ← NEW (one file)
                                           │  └────────────────┘ │
                                           └─────────────────────┘
```

**Invariants:**
1. `Strategy` and the `StrategyContext` interface contract are unchanged.
2. `app/broker/ibkr/` is the only place that imports `ib_async`. The new `app/engine/live/` modules talk to the broker through its public Python surface, never `ib_async` directly. This preserves the curated boundary documented in `ibkr-integration-tdd.md` §3.2.
3. The one new file inside `app/broker/ibkr/` is a real-time underlying-bar streamer (`bars.py`) — see Phase 2.

---

## 2. Pre-flight: what must already be true

Codex should verify these and stop with a flag if any is false:

- [ ] `ruff check PythonDataService/app/ PythonDataService/tests/` is clean on `main`.
- [ ] `dotnet format podman.sln --verify-no-changes` is clean on `main`.
- [ ] The Tier-1 fixes in [`docs/archive/plans/engine-tv-alignment-roadmap.md`](archive/plans/engine-tv-alignment-roadmap.md) §2-3 (RTH filter, warmup buffer) are merged. Without them, paper would diverge from the backtest because of known data-pipeline bugs, polluting the receipt.
- [ ] `test_spy_validation.py` and `test_spy_next_bar_open_validation.py` both pass.
- [ ] All 86 broker tests under `tests/broker/ibkr/` pass.
- [ ] `IBKR_MODE=paper`, `IBKR_PORT=4002` (or 7497), `IBKR_READONLY=false`, paper-account `DU…` ID present in `.env`.
- [ ] NYSE/ARCA real-time market-data subscription active on the IBKR live account (paper inherits — see `ibkr-integration-tdd.md` §2.4).
- [ ] IB Gateway running, connectable, `GET /api/broker/health` returns `connected: true, is_paper: true`.

---

## 3. Phase 1 — Adapter scaffolding (no broker changes yet)

**Goal:** `app/engine/live/` exists with module skeletons. No new dependency.

**Files to create:**

```
PythonDataService/app/engine/live/
  __init__.py
  live_context.py            # to be filled in Phase 4
  live_portfolio.py          # to be filled in Phase 3
  live_engine.py             # to be filled in Phase 5
  config.py                  # thin LiveConfig (paper run settings)
  run.py                     # CLI entrypoint (Phase 8)
  reconcile.py               # paper-vs-backtest diff (Phase 9)
  README.md                  # paper runbook

PythonDataService/tests/engine/live/
  __init__.py
  fixtures/__init__.py
  fixtures/fake_broker.py    # in-memory broker stand-in
  test_live_portfolio.py
  test_live_context.py
  test_live_engine_replay.py # the parity gate (Phase 6)
  test_live_engine_collapse.py # broker-event-collapse test (Phase 7)
```

**Acceptance criteria:**
- `import app.engine.live` succeeds.
- `python -m app.engine.live.run --help` prints usage and exits 0.
- All new modules pass `ruff check`.
- No new entries in `requirements-light.txt` or `requirements-heavy.txt`.

**Risk:** None — pure scaffolding, no broker contact.

---

## 4. Phase 2 — Real-time minute-bar source (`app/broker/ibkr/bars.py`)

**Goal:** Add the one piece of broker functionality that doesn't exist yet — streaming closed 1-minute bars for an equity symbol — inside `app/broker/ibkr/`, following the patterns of the existing module.

**Why a new file rather than extending `market_data.py`:** `market_data.py` is option-chain-specific (`reqMktData` with generic ticks 100/101/106). Bar streaming uses `reqRealTimeBars` with a TRADES `whatToShow` and a different cancellation contract. Mixing them muddies the curated boundary.

**Public surface** (`app/broker/ibkr/bars.py`):

```python
async def stream_minute_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
) -> AsyncIterator[IbkrMinuteBar]: ...
```

**Implementation notes:**

1. **Bar source.** `client.ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=use_rth)`. IBKR delivers 5-second TRADES bars; aggregate internally to closed 1-minute bars and yield one per minute boundary. Do **not** use `reqHistoricalData(..., keepUpToDate=True)` — its update cadence is documented as approximate and bar finalization is not guaranteed (this is the same reasoning Codex cited).
2. **Closed bars only.** Yield only after the minute boundary has passed. Never yield in-flight aggregates.
3. **Timestamp normalization.** `int64 ms UTC` at the boundary, per `.claude/rules/numerical-rigor.md`. Convert IB's `datetime` (which is tz-aware UTC in `ib_async`) immediately on receipt. The yielded `IbkrMinuteBar` model carries `start_ms: int64`, `end_ms: int64`.
4. **Fail-fast on duplicates and non-monotonic timestamps.** Reject with a descriptive `IBKRBarStreamError`. Do not silently dedupe — duplicates signal upstream corruption.
5. **Cancellation.** `try/finally` calls `client.ib.cancelRealTimeBars(bars_obj)` exactly like `market_data.stream_option_chain` cancels its `reqMktData`. No streaming-line leaks.
6. **Pacing.** A single 5-second-bar subscription is well under the 50 msg/s and 100-line quotas. Document this; do not add per-call pacing.

**New Pydantic model** (`app/broker/ibkr/models.py`):

```python
class IbkrMinuteBar(BaseModel):
    symbol: str
    start_ms: int   # int64 ms UTC, inclusive
    end_ms: int     # int64 ms UTC, exclusive
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int
```

**Tests** (`tests/broker/ibkr/test_bars.py`, ~10 tests):
- Five 5-sec bars within the same minute are aggregated into one yielded `IbkrMinuteBar` with correct OHLCV.
- A new minute-aligned 5-sec bar triggers the previous minute to fire.
- A duplicate 5-sec timestamp raises `IBKRBarStreamError`.
- A non-monotonic 5-sec timestamp raises `IBKRBarStreamError`.
- Subscriber cancellation calls `cancelRealTimeBars` exactly once.
- `useRTH=True` is honored (mock returns RTH-only bars).

**Acceptance:** All bar tests pass with `ib_async` mocked. Total broker test count rises to ~96.

---

## 5. Phase 3 — `LivePortfolio`

**Goal:** Implement the `Portfolio`-shaped surface that the strategy depends on, but route every read to existing broker primitives and every order through `place_paper_order`.

**File:** `app/engine/live/live_portfolio.py`

**Surface mapping** (existing → new wrapper):

| `Portfolio` method | `LivePortfolio` implementation |
|---|---|
| `cash` (property) | `account.fetch_account_summary(client).cash_balance`, cached for current bar |
| `positions[sym].quantity` | `account.fetch_positions(client).positions`, cached |
| `total_value()` | `account.fetch_account_summary(client).net_liquidation` |
| `update_reference_price(sym, price)` | Pure local cache — no IBKR call. Updated by the engine on every minute bar. |
| `submit_market_order(sym, qty, time, …)` | Build `IbkrOrderSpec` (with `confirm_paper=true`, `client_order_id` from monotonic counter), call `place_paper_order(client, spec)`. Return a synthetic `Order` keyed by the broker `order_id` for engine bookkeeping. |
| `set_holdings(sym, fraction, time, tag)` | **Use the same math as `Portfolio.set_holdings`** (`portfolio.py:142-168`): `target_qty = int(total_value * fraction / reference_price)`. Reference price is the consolidated bar's close (passed in by `LiveContext` — see Phase 4). Submits via the wrapper above. |
| `liquidate(sym, time)` | Position-opposite market order via the wrapper. |
| `apply_fill(event)` | **Removed** — IBKR is the source of truth for fills. The engine routes `OrderEvent` to `strategy.on_order_event` only. |
| `_next_id()` | File-backed monotonic counter (see Phase 8 hygiene). |

**Critical correctness item:** `set_holdings` math must use the consolidated bar's close as `reference_price`, not a freshly-fetched IB tick. Otherwise integer `target_quantity` drifts from the backtest and the receipt is meaningless. This is plumbed through `LiveContext.set_holdings(symbol, fraction)` calling `LivePortfolio.set_holdings(symbol, fraction, ref_price=consolidated_bar.close)` — the engine passes the consolidated bar through to the context on every emit.

**Tests** mirror `test_portfolio.py` semantically — recorded calls against a `FakeBroker` that implements the same async surface as `IbkrClient` + `place_paper_order` + `fetch_account_summary` + `fetch_positions` + `stream_order_events`.

---

## 6. Phase 4 — `LiveContext`

**Goal:** A drop-in for `StrategyContext` that the strategy class compiles against unchanged.

**File:** `app/engine/live/live_context.py`

**Differences from `StrategyContext`:**

1. Wraps `LivePortfolio` instead of the simulated `Portfolio`.
2. `register_consolidator` reuses the existing `app/engine/consolidators/TradeBarConsolidator` verbatim — the consolidator is data-source-agnostic.
3. `set_holdings(sym, fraction)` plumbs the current consolidated bar's close as the reference price (see Phase 3 critical item).
4. `liquidate`, `log`, `emit_insight`, `current_time`, `_pre_handler_hook` behave identically. `_pre_handler_hook` is unused in v1 (no live brackets); preserve the field for shape symmetry.
5. `consolidated_bars` retained — same role as backtest, used by the reconciliation tooling for the final report.

**Tests:** for a recorded sequence of bars + orders, assert `LiveContext` invokes the same calls on `LivePortfolio` that `StrategyContext` invokes on `Portfolio`, using a recording double.

---

## 7. Phase 5 — `LiveEngine` driver

**Goal:** Asyncio event loop replacing `BacktestEngine.run`'s `for minute_bar in iter_bars(...)`.

**File:** `app/engine/live/live_engine.py`

**Lifecycle:**

```python
class LiveEngine:
    def __init__(self, client: IbkrClient, config: LiveConfig) -> None: ...

    async def run(self, strategy: Strategy) -> LiveRunHandle:
        # 1. Validate the four-layer paper safety eagerly: read client.settings,
        #    confirm IBKR_MODE=paper, port in PAPER_PORTS, account is DU.
        #    The order placement gate runs again per-order; this is for fail-fast.
        # 2. Build LivePortfolio(client) and LiveContext(portfolio).
        # 3. strategy.ctx = ctx; strategy.initialize().
        # 4. Validate single-symbol constraint (mirror BacktestEngine).
        # 5. Concurrently, three tasks:
        #    a. minute_bar_consumer:  async for bar in stream_minute_bars(client, sym):
        #                               feed consolidator → handler → drain orders → submit
        #                               update equity_curve, score insights
        #    b. order_event_consumer: async for ev in stream_order_events(client):
        #                               translate to engine OrderEvent → strategy.on_order_event
        #                               update LivePortfolio cached state
        #    c. force_flat_scheduler: schedule asyncio.call_at(force_flat_at) →
        #                              cancel open orders, market-flat, strategy.on_force_flat
        # 6. Run forever (or until handle.stop()).
```

**Critical correctness items:**

1. **Single-task strategy execution.** Bar handler and fill handler must run on the same task to prevent concurrent state mutation in the strategy. Use one `asyncio.Queue` that both consumers write to, and a single consumer coroutine that calls strategy methods.
2. **Order submission completes before the next minute bar.** Measure end-to-end latency (`submit_ts - bar_close_ts`) on every order; log a warning if > 500 ms. The replay test enforces it; the live runtime needs a watchdog.
3. **Force-flat barrier.** Equivalent to `BacktestEngine`'s session-close force-flat. On fire: cancel any open IBKR orders via `cancel_paper_order`, market-out positions via `place_paper_order`, call `strategy.on_force_flat()`.
4. **No deferred-fill list.** Backtest's `NEXT_BAR_OPEN` deferred fills do not exist in live. The broker is the source of fill timing. Document this in module docstring.
5. **Equity curve.** Snapshot on every consolidated 15-min bar boundary (not on every minute, to keep file size reasonable). Persist to `live_runs/<run_id>/equity_curve.parquet` with `int64 ms UTC` timestamps.
6. **Insight scoring cadence.** `ctx.insight_manager.step(minute_bar.end_time, current_prices)` runs on every minute bar — same cadence as `BacktestEngine.run` (`engine.py:371-374`). Codex feedback §8: stepping per-15-min would change scoring and finalization semantics.
7. **Logging.** Structured logger per module. `[STEP X]` markers for the lifecycle: `[STEP 1] CONNECT`, `[STEP 2] SUBSCRIBE`, `[STEP 3] BAR`, `[STEP 4] ORDER`, `[STEP 5] FILL`, `[STEP 6] FORCE_FLAT`. No `print`.

---

## 8. Phase 6 — Replay parity test (the gate)

**Goal:** Prove `LiveEngine` produces the same trades the `BacktestEngine` produces, when fed the same bars through the fake broker.

**File:** `tests/engine/live/test_live_engine_replay.py`

**Procedure:**

1. Load the existing SPY validation fixture (the same minute bars the LEAN parity test uses).
2. Run `BacktestEngine.run(SpyEmaCrossoverAlgorithm())` with `FillMode.NEXT_BAR_OPEN`. Capture `order_events`, `equity_curve`, `log_lines`, `strategy.trade_log`, `insight_summary`.
3. Configure `FakeBroker(bars=fixture_bars, fill_at=lambda bar: bar.next_minute.open)` to fill market orders at the next 1-min bar's open — matching `FillMode.NEXT_BAR_OPEN`.
4. Run `LiveEngine.run(SpyEmaCrossoverAlgorithm())` against the fake. Capture the same outputs.
5. Assert (revised per Codex feedback §7):
   - **Order count exact.** `len(live_orders) == len(backtest_orders)`.
   - **Per-order exact match** on: `symbol`, `direction`, `fill_quantity`, `fill_price`, `tag`. Tolerance: `Decimal("0")` — both engines consume the same Decimal OHLCV with the same fill rule. Any non-zero diff is a bug.
   - **Order ID monotonicity** within the live run.
   - **Submit and fill timestamps** match within 1 ms (timestamps are `int64 ms`; the 1 ms allowance covers integer arithmetic ordering, not real time).
   - **Final cash, positions, total fees** exact match.
   - **Equity curve** exact match per consolidated-bar snapshot.
   - **`strategy.trade_log` (`LoggedTrade` list)** exact match on entry_time, entry_price, exit_time, exit_price, pnl_pts, pnl_pct, result, indicators dict.
   - **Insight count and per-insight score** exact match.
   - **No open positions, no pending orders** at end-of-fixture.
   - **Force-flat fired iff backtest fired it.**

**Why exact, not cent-tolerant:** in the fake-client gate both engines see identical `Decimal` OHLCV inputs, identical fill rules, identical reference-price plumbing. The cent tolerance from v1 was hiding bugs in this gate. Cent tolerance is reserved for real-broker reconciliation in Phase 9, where it correctly absorbs broker-side slippage.

**Failure modes this test does NOT cover** (covered in Phase 7):
- Polling-based event-stream collapse (`orders.py:443-451` documents this trade-off).
- Reconnect-mid-bar behavior.
- IBKR-specific oddities like `OrderStatus.PendingSubmit` → `Filled` skipping `Submitted`.

---

## 9. Phase 7 — Broker-lifecycle-event-collapse test (NEW)

**Goal:** Cover the failure mode Codex identified in feedback §7. The synchronous fake broker in Phase 6 produces clean submit→fill ordering. The real `stream_order_events` polls `IB.trades()` on a 0.5-second interval and may collapse two transitions into one yielded event. Assert the engine handles collapsed sequences without dropping fills or double-applying status.

**File:** `tests/engine/live/test_live_engine_collapse.py`

**Procedure:**

1. Configure a `FakeBroker` variant that, on order submission, internally transitions `PendingSubmit → Submitted → Filled` between two polling ticks — emitting only the final `Filled` status event (the collapsed case).
2. Run `LiveEngine` for one trade through this collapsed sequence.
3. Assert:
   - The strategy's `on_order_event` is called exactly once with the final fill.
   - `LivePortfolio` cached state matches the broker's reported cash/positions after the collapse.
   - The trade is logged in `strategy.trade_log` exactly once.
   - No unhandled exceptions in `live.log`.

**Repeat for** the symmetric collapse on the exit order. Both directions matter.

**Why this matters:** if the engine assumes a `Submitted` event will arrive before `Filled` (e.g., for state-machine bookkeeping), real paper trading will silently desynchronize from the receipt, and the divergence will be classified as `unknown` in the reconciliation report. Catching it in test rather than discovery during the paper week saves the receipt.

---

## 10. Phase 8 — Paper config, CLI, hygiene

**Goal:** A `python -m app.engine.live.run` command that paper-trades SPY end-to-end with sane defaults. Most safety is delegated to existing `IbkrSettings` (`app/broker/ibkr/config.py`).

**Files:**

- `app/engine/live/config.py` — `LiveConfig` (engine-level, not broker-level)
- `app/engine/live/run.py` — CLI
- `app/engine/live/README.md` — runbook

**`LiveConfig` schema** (engine knobs only — broker safety is already in `IbkrSettings`):

```python
class LiveConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVE_", env_file=".env")

    symbol: str = "SPY"
    force_flat_at: time = time(15, 55)  # ET, 5 min before close
    consolidator_period_min: int = 15
    run_dir: Path = Path("live_runs")  # equity, log, state

    # Watchdog: warn if order submission latency exceeds this.
    max_submit_latency_ms: int = 500
```

The four-layer paper safety is **not** duplicated here. Codex feedback §5: don't put safety logic in two places. The CLI eagerly calls `IbkrSettings()` (raises if port/mode disagree) and `IbkrClient.connect()` (raises if account isn't `DU`). `place_paper_order` re-checks per-request with `confirm_paper=true`.

**CLI (`run.py`):**

- Parses `--config paper.yaml` or env vars.
- Logs to `live_runs/<timestamp>/live.log` with rotation.
- Catches SIGINT/SIGTERM → graceful shutdown: cancel open orders, flatten positions, disconnect, write final equity snapshot.
- On unhandled exception: log full traceback, attempt to flatten, exit non-zero. Do **not** silently restart.

**Hygiene tasks (incorporated, per Codex cross-cutting):**

- Add to `.gitignore`:
  ```
  # Live-runtime artifacts
  PythonDataService/live_runs/
  PythonDataService/.live_state.json
  ```
- All run-artifact files (`equity_curve.parquet`, `orders.parquet`, `live_state.json`) carry `int64 ms UTC` timestamps. No ISO strings, no naive datetimes. Per `.claude/rules/numerical-rigor.md` "Timestamp rigor".
- `.live_state.json` writes are atomic (write to `.tmp`, fsync, rename). Path is run-scoped: `live_runs/<run_id>/state.json`. The monotonic order-ID counter persists here.
- **Drop the `mypy --strict` gate.** The service has `ruff.toml` but no `pyproject.toml` and no mypy config. Make it an explicit follow-up in §13 rather than an aspirational acceptance criterion.

**Tests:** `LiveConfig` validator unit tests; CLI smoke test (`pytest -k test_cli`).

---

## 11. Phase 9 — Reconciliation tooling

**Goal:** Diff a paper run against a same-window backtest run and emit a reconciliation report.

**File:** `app/engine/live/reconcile.py`

**Inputs:**
- `--paper-run live_runs/<run_id>/`
- `--backtest-window YYYY-MM-DD,YYYY-MM-DD` (re-runs the backtest internally for the same window with `FillMode.NEXT_BAR_OPEN` and `set_holdings(SPY, 1.0)` — matching what paper actually ran)

**Output:** `docs/references/reconciliations/spy-ema-crossover-paper-<run_id>.md` containing:
- Window, symbol, run config, `IbkrConnectionHealth` snapshot at run start
- Trade-by-trade table: paper time/price, backtest time/price, delta, classification
- Divergence taxonomy from `reconcile-backtest` skill: `precision` / `timestamp` / `fill_model` / `slippage` / `commission` / `data_gap` / `unknown`
- Cumulative PnL delta, total fees delta
- Pass/fail vs explicit tolerance: PnL within ±0.5% of backtest, zero `unknown` divergences

**Tolerances** (these are the *real-broker* tolerances, not the replay-gate tolerances):
- Fill price: `atol=Decimal("0.05"), rtol=0` — broker slippage envelope.
- Fill time: ±5 seconds vs next-bar open.
- Fill quantity: exact match required.

**Why this is the deliverable:** `.claude/rules/numerical-rigor.md` "Reconciliation reports" — a port (and the live-runtime port is exactly that) isn't done until it has one.

---

## 12. Phase 10 — Run paper for one trading week, write the receipt

**Goal:** Generate the actual reconciliation report from a real paper-trading week.

**Procedure:**

1. Start the runner Monday pre-open. Let it run 5 RTH sessions.
2. Each evening, run `python -m app.engine.live.reconcile --paper-run <today> --backtest-window <today>,<today>` and review.
3. End of week: aggregate into `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX.md`.
4. Update `docs/math-sources-of-truth.md` with a `live-runtime` row pointing at the report.
5. Open a PR titled "stage-2 parity receipt: SpyEmaCrossover paper week of 2026-XX".

**Acceptance for "stage 2 done":**
- All 5 days reconciled, sized at native `1.0`.
- Cumulative PnL delta within ±0.5% of the backtest, OR every divergence classified non-`unknown` with documented explanation.
- Zero unhandled exceptions in `live.log`.
- Zero forced restarts of the runner.
- `IBKR_READONLY=false` was on for the duration — recorded in run metadata.

---

## 13. Risks & open questions for refinement

1. **Single-account FA.** `IbkrClient.connect` (`client.py:201-208`) fails closed on >1 managed account. If the paper account is part of an FA structure, this blocks Phase 7. Resolve before Pre-flight.
2. **Gateway daily restart.** Documented in `ibkr-integration-tdd.md` §6 risk register. The runner's reconnect loop must survive this. Worth a manual test mid-week.
3. **2FA mid-session.** TDD §6 says the user requested IBKR relax 2FA for API. Confirm before paper week 1 — mid-week 2FA reauth invalidates the run.
4. **CI for live runtime.** Phase 6 (replay) and Phase 7 (collapse) run without IB and go in CI. Phase 4 (`bars.py`) tests stub `ib_async` and also go in CI. No live-IB tests in CI.
5. **`mypy --strict` follow-up.** The service has no `pyproject.toml`. Adding one is a separate ticket — flagging here rather than blocking on it.
6. **Insights vs no-trade minutes.** The strategy emits insights only on entries (15-min cadence) but the InsightManager is stepped per-minute. If `stream_minute_bars` skips a minute due to a 5-second-bar gap (rare, but possible at thin liquidity), insight `close_time` evaluation may be late by ≤1 minute. Acceptable for v1; document.
7. **Polling rate for `stream_order_events`.** Default `poll_seconds=0.5` (`orders.py:436`). For paper at 1 trade per ~75 minutes this is plenty. Don't tune.
8. **Order ID persistence: file vs Postgres.** v2 uses `.live_state.json`. Codex feedback §10: Postgres is a real persistence feature, not a small refactor (no migrations workflow yet, `EnsureCreated` only). If we need it, that's a separate ticket.

---

## 14. Files Codex will create or modify

**Create (engine):**
- `PythonDataService/app/engine/live/__init__.py`
- `PythonDataService/app/engine/live/live_context.py`
- `PythonDataService/app/engine/live/live_portfolio.py`
- `PythonDataService/app/engine/live/live_engine.py`
- `PythonDataService/app/engine/live/config.py`
- `PythonDataService/app/engine/live/run.py`
- `PythonDataService/app/engine/live/reconcile.py`
- `PythonDataService/app/engine/live/README.md`

**Create (broker — one new file, in the existing module):**
- `PythonDataService/app/broker/ibkr/bars.py`

**Create (tests):**
- `PythonDataService/tests/engine/live/__init__.py`
- `PythonDataService/tests/engine/live/fixtures/__init__.py`
- `PythonDataService/tests/engine/live/fixtures/fake_broker.py`
- `PythonDataService/tests/engine/live/test_live_portfolio.py`
- `PythonDataService/tests/engine/live/test_live_context.py`
- `PythonDataService/tests/engine/live/test_live_engine_replay.py`
- `PythonDataService/tests/engine/live/test_live_engine_collapse.py`
- `PythonDataService/tests/broker/ibkr/test_bars.py`

**Create (config / docs):**
- `PythonDataService/paper.example.yaml`
- `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX.md` (Phase 10 deliverable)

**Modify:**
- `PythonDataService/app/broker/ibkr/models.py` — add `IbkrMinuteBar`
- `PythonDataService/app/broker/ibkr/__init__.py` — export new module if patterns require
- `.gitignore` — add `live_runs/` and `.live_state.json`
- `docs/math-sources-of-truth.md` — add `live-runtime` row referencing the reconciliation report (Phase 10 deliverable)

**Do NOT touch:**
- `app/engine/strategy/algorithms/spy_ema_crossover.py` — strategy class is unchanged by design
- `app/engine/engine.py` — `BacktestEngine` is unchanged
- `app/engine/strategy/base.py` — `StrategyContext` interface is the contract
- `requirements-light.txt`, `requirements-heavy.txt` — no new deps
- `app/broker/ibkr/client.py`, `config.py`, `orders.py`, `account.py`, `market_data.py`, `pnl.py`, `contracts.py`, `persistence.py` — broker boundary already correct; do not extend

---

## 15. How to hand this to Codex

Run phases sequentially. After each phase Codex should:
1. Show the diff.
2. Run `ruff check PythonDataService/app/ PythonDataService/tests/`.
3. Run the tests for that phase.
4. Stop and report. The user reviews before authorizing the next phase.

**Hard gates:**
- **Phase 6 (replay parity)** — must pass exactly. If it fails, do not proceed; report and propose fixes.
- **Phase 7 (collapse test)** — must pass. If it fails, the live runtime has a real concurrency bug that would silently corrupt the receipt.

**Manual phases (not Codex-driven):**
- **Phase 10 (paper week)** — requires real IBKR connection and human supervision. Codex's job ends at "the runner is verified end-to-end against the fake broker, and the reconciliation script works on a recorded sample run."
