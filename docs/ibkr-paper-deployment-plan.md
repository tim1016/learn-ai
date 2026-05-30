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

**Status (2026-05-12): SHIPPED on `feat/ibkr-paper-runner-hardening`.** The CLI grew richer than originally spec'd — four subcommands (`init-ledger`, `pre-flight`, `start`, `emergency-flatten`) with run-ledger identity, halt/poisoned-flag refusal, max-orders-per-day cap, and dirty-tree refusal. The hardening commit added on top: `LiveEngine.shutdown_event` for graceful SIGINT/SIGTERM exit, `run_logging.configure_run_logging` (10MB × 5 backups), unhandled-exception recovery flatten via `_recovery_flatten`, and `IbkrClient.connect()/disconnect()` wiring (the latter closed a latent bug — the prior CLI never connected the client). See the IBKR integration authority doc § 6 for the runtime semantics and § 11 for the test inventory.

**Deferred to follow-ups (intentional scope choices, not blockers):**
- Equity-curve parquet writer — `equity_curve` lives in `LiveRunResult` in memory; `artifacts.py` has writers for decisions/executions/trades but no `EquityWriter`. Standalone follow-up.
- YAML config input to `init-ledger` — `--live-config-json` already round-trips through the ledger; YAML is a sugar layer.
- `LiveConfig` → `BaseSettings` conversion — current `dataclass(frozen=True)` works fine for the ledger-mediated flow; env-var loading at init-ledger would require this conversion.

**Goal (original):** A `python -m app.engine.live.run` command that paper-trades SPY end-to-end with sane defaults. Most safety is delegated to existing `IbkrSettings` (`app/broker/ibkr/config.py`).

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

**Status (2026-05-13):** SHIPPED, but **the implemented design is three-way** (Python live ↔ QC Cloud ↔ IBKR fills), not the paper-vs-backtest design originally outlined in this section. The three-way design is specified in [`docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md`](superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md) § 6 and supersedes what follows here. Per-bar classifications use `CrossEngineClass` (`none`/`data`/`engine`) and `FillClass` (`none`/`within_tol`/`breach`); the divergence taxonomy below is preserved as historical framing. End-to-end operator steps live in [`docs/runbooks/ibkr-paper-dry-run.md`](runbooks/ibkr-paper-dry-run.md). Deferred: IBKR `commissionReport` callback (real fills carry `fee=0` today; reconciliation receipts will look misleadingly clean on commissions until this lands).

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

---

## 16. Design-Lock Round (2026-05-28)

This section captures the design decisions taken in the 2026-05-28 grilling session for the persistent paper-trading bot architecture, sim ↔ paper divergence harness, and the shadow VWAP strategy onboarding. It supersedes any conflicting framing in earlier sections of this document (in particular, the pre-implementation design draft's proposal of a Postgres control plane and a `gnzsnz/ib-gateway-docker` container topology).

The full session record lives in `docs/architecture/adrs/000{1,2,3}-*.md` for the three load-bearing decisions; this section is the digest, queue, and term lock.

### 16.1. The 8 resolutions

| # | Decision | Authority |
|---|---|---|
| **1** | **Substrate** — JSON + Parquet + hash-sidecar artifacts remain canonical for the live-runtime control plane. Postgres is reserved as a future *projection layer* (read-replica derived from artifacts), never the source of truth. Triggers for the projection layer are written into the ADR. | [ADR 0001](architecture/adrs/0001-control-plane-substrate-json-parquet.md) |
| **2** | **Cold-restart source of truth** — broker truth via namespaced ownership (`orderRef` / `client_order_id`), not `reqAllOpenOrders`. Sidecar is a cross-check ("what must be checked"), not a source of state. Mismatch → `poisoned.flag`, refuse to submit new orders. Rule: **no broker connection, no verified resume; mismatch, no trading.** | This § 16.4 |
| **3** | **Sidecar field list** — explicit. `run_id, strategy_instance_id, bot_order_namespace, ib_client_id, last_processed_bar_ms, last_artifact_flush, pending_intents, submitted_orders, known_perm_ids, known_exec_ids, expected_position_by_symbol, poisoned_reason?`. | This § 16.4 |
| **4** | **Shadow mode** — same engine, broker-adapter-level `submit_mode` switch (`"live_paper"` \| `"shadow"`). Separate OS processes per strategy. Five invariants govern shadow (cold-start namespace empty at broker; explicit `execution_source` tag; explicit `fill_model` + `source_bar_close_ms`; bounded blast radius; graduation = new run ledger). | [ADR 0002](architecture/adrs/0002-shadow-mode-adapter-level-no-submit.md) |
| **5** | **Schema growth** — per-strategy parquet, schema generated from `StrategySpec` at run init. Universal core prefix (`run_id, strategy_key, strategy_instance_id, bar_close_ms, bar_source, bar_open, bar_high, bar_low, bar_close, bar_volume, signal, intended_action, intended_price, intended_fill_model, decision_latency_ms, mode`) plus strategy-specific typed indicator columns. `DECISION_COLUMNS` resolution rule: `CORE_DECISION_COLUMNS + strategy_spec.decision_columns`. Spec declares types + nullability + semantics, not just names. Cross-strategy joins live in the report layer, not the artifact writer. `ExecutionRow` grows by `execution_source ∈ {"broker_fill","shadow_sim"}`, `fill_model`, `source_bar_close_ms`. | This § 16.4 |
| **6** | **Divergence taxonomy** — two new layer-scoped enums; existing `DivergenceCategory` + `CrossEngineClass` + `FillClass` from `qc_reconciler.py` / `reconcile.py` are **untouched** (they describe backtest-vs-backtest agreement, which is a different question). New enums: <br>**Layer A — `ExecutionDivergence`:** `SLIPPAGE, LATENCY_SUBMIT, LATENCY_FILL, MISSED, EXTRA, PARTIAL, REJECTED, COMMISSION_MISSING, COMMISSION_DRIFT`. `COMMISSION_OBSERVED` is a metric/status field, not a category. <br>**Layer B — `ReplayDivergence`:** `DATA_DRIFT_{O,H,L,C,V}, INDICATOR_STATE_DRIFT, DECISION_DRIFT, COVERAGE_GAP, TRADE_GRAPH_DRIFT`. <br>`fill_latency_atol_ms` (default 2000 for SPY paper market orders) is configurable per-report and reported with every divergence record. Report bundles are separate: `day-N.exec.{md,json,parquet}` (Layer A) and `day-N.replay.{md,json,parquet}` (Layer B), each with own tolerances, category counts, manifest hash, and pass/fail gate. The three-way reconciler's existing `day-N.{md,json,parquet,hashes.json}` continues to write unchanged as a third bundle. | This § 16.4 |
| **7** | **Operator control surface** — file-based command channel matching the artifact substrate. **Durable desired state** at `artifacts/live_state/<strategy_instance_id>/desired_state.json` with `{desired_state ∈ {RUNNING,PAUSED,STOPPED}, updated_at_ms, updated_by, reason, version}` — survives crash + reboot (PAUSED stays PAUSED across restart). **Per-run commands** at `artifacts/live_runs/<run_id>/commands/command.<seq>.<verb>.pending.json` with verbs `{PAUSE, RESUME, STOP, FLATTEN, RECONCILE, MARK_POISONED}`. Atomic create/rename for partial-write safety. Ack as `command.<seq>.<verb>.ack.json` with outcome payload. Bot polls command dir at 1s, independent of bar loop. **Panic path** is `run.py emergency-flatten` direct-broker CLI — does not depend on bot being alive. **Final truth on restart** = broker cross-check per Resolution 2 before any trading. `MARK_POISONED` is the operator-declared halt verb (e.g. "I saw a manual trade hit this account"). | This § 16.4 |
| **8** | **Operational topology** — (T3) Hybrid. Windows host-venv preserved (Gateway + IBC native; bots in host venv; `host_daemon.py` extended to N processes and NSSM-wrapped for boot survival). Podman compose stays observability-only. One Gateway, multiple `clientId`s pinned per strategy spec. IBKR paper data remains delayed (no subscription) and is logged as `bar_source = "ibkr_paper_delayed"` in every `DecisionRow`; subscribe only if Layer B becomes uninformative. Explicit triggers for (T4) Linux VPS migration are written. | [ADR 0003](architecture/adrs/0003-operational-topology-host-venv.md) |

§ 8 (open questions in the pre-implementation draft) is superseded: items #1 (always-on machine), #4 (paper market data), #5 (one Gateway vs two) are all resolved above. Items #2 (reconcile existing repo), #3 (long-only vs long/short for VWAP) — the first is done by the design-lock reconnaissance; the second is left to the VWAP `port-indicator` session along with bar size, band formula, k, session filter, and position sizing.

### 16.2. PR queue

Suggested batching. Each PR ends with documentation per "documentation is part of done". The Phase 10 prereq RTH dry-run is an operational event, not a PR — it sits between PR-D and PR-E in the queue as a gating activity.

| PR | Title | Scope | Depends on |
|---|---|---|---|
| **PR-A** | `host_daemon` registry refactor | `_current: ManagedProcess \| None` → `_managed: dict[strategy_instance_id, ManagedProcess]`. Existing single-bot lifecycle preserved end-to-end. | none |
| **PR-B** | `StrategySpec` growth + clientId/submit_mode pinning | Typed `decision_columns`, `clientId`, `submit_mode` (default `"live_paper"`), `bar_source_descriptor`. Update `spy_ema_crossover.spec.json`. No behavior change for EMA. | PR-A |
| **PR-C** | `DecisionRow` / `ExecutionRow` schema growth | Core columns from § 16.1 Resolution 5 added; `artifacts.py` refactored to resolve columns from spec; `ExecutionRow` gains `execution_source` / `fill_model` / `source_bar_close_ms`. Parity test, replay parity gate must still pass. | PR-B |
| **PR-D** | Command channel + durable desired-state | Stable `desired_state.json` per strategy_instance_id; per-run command files with atomic writes; 1s poll loop in bot independent of bar loop; CLI verbs wired. Tests: PAUSED across crash; STOPPED → no restart loop. **✅ Implemented 2026-05-29**: the per-run command channel + 1s poll loop + engine verb dispatch shipped earlier (#367 / #373); this round closes the row by adding the durable `desired_state.py` sidecar (`<artifacts_root>/live_state/<strategy_instance_id>/desired_state.json`, atomic write, default-RUNNING), `start`-time gating (PAUSED → boots paused, STOPPED → refuses restart, corrupt → refuse), engine-side persistence of PAUSE/RESUME/STOP→durable intent, and `run.py pause`/`resume`/`stop` operator verbs. | PR-A, PR-C |
| **Phase 10 prereq — Full RTH dry-run pass** | First end-to-end populated `decisions.parquet` against real Gateway | Operator-driven session (~10.5h wall clock per integration authority § 11). Closes "writer-schema + reconcile-loader contract unverified" gap. | PR-D |
| **PR-E** | Order-idempotency `.live_state.json` sidecar + cold-start cross-check | Sidecar fields from § 16.1 Resolution 3; cold-start procedure from § 16.1 Resolution 2; mismatch writes `poisoned.flag` and refuses to submit. Tests: mock IBKR with mismatched open orders → poison fires. | PR-D + Phase 10 prereq |
| **PR-F** | NSSM / Windows Service wrap for `host_daemon` | Auto-start on boot. Test: reboot machine, host_daemon up before login. | PR-A (any time after) |
| **PR-G** | `commissionReport` callback + `ExecutionRow.fee` populated + `COMMISSION_OBSERVED` metric | Wires the IBKR callback path; required before Layer A divergence is meaningful. (Phase 10 prereq row in integration authority § 11.) | PR-C |
| **PR-H** | Layer A `ExecutionDivergence` harness + `day-N.exec.{md,json,parquet}` | New enum, matched-ledger code over `decisions.parquet` + `executions.parquet`; per-category default tolerances declared; per-report tolerance override and report-time logging; pass/fail gate. Synthetic fixture exercises each category. | PR-C, PR-G |
| **PR-I** | Layer B `ReplayDivergence` harness + `day-N.replay.{md,json,parquet}` | Replays strategy spec against archived Polygon bars for same session window; joins to live decisions; emits divergence categories; pass/fail gate. Synthetic fixture covers each category. | PR-C |
| **PR-J** | `NoSubmitBrokerAdapter` + adapter-level submit-mode switch | Implements ADR 0002. Existing `IbkrBrokerAdapter` unchanged. Engine no longer branches on submit-mode internally. Tests: shadow run produces `execution_source = "shadow_sim"` rows; never calls `ib.placeOrder`. | PR-B, PR-C |
| **PR-K** | VWAP-band reversion strategy port | `port-indicator` session: math, golden fixture from LEAN / Polygon reference, parity test at `atol=1e-9`. Strategy spec `spy_vwap_reversion.spec.json` with clientId, typed decision_columns, `submit_mode = "shadow"`. Indicator-state sidecar at `artifacts/live_state/spy_vwap_reversion_1min/SPY_1m.json`. | PR-J |
| **PR-L** | Shadow VWAP onboarding | Second managed process under PR-A's registry. Smoke run: shadow boots, registers, emits decisions, never submits orders, cold-start namespace check yields zero broker orders. | PR-A, PR-J, PR-K |

Deferred per the revised direction (Step 7+):
- PDF/matplotlib presentation layer over Layer A/B markdown+parquet outputs.
- Angular polish: see §16.5 for the now-required UI accuracy and operator UX plan.
- Authentication / authorization around command issuance and audit identity.

### 16.3. Term Lock (deployment-specific glossary)

These terms are introduced or pinned by this design-lock round. They are deployment-runtime-specific; trading-domain terms remain owned by `.claude/skills/trading-domain/`. A repo-wide `CONTEXT.md` is not created at this time — if/when a third use site for these terms emerges, promote them.

| Term | Definition |
|---|---|
| **strategy_instance_id** | Stable identifier for a configured strategy instance across runs. Used as the key for the per-strategy stable sidecar directory (`artifacts/live_state/<strategy_instance_id>/`), the indicator-state sidecar, and the `host_daemon` managed-process registry. Distinct from `strategy_key` (the algorithm family, e.g. `spy_ema_crossover`) and from `run_id` (a single execution). One `strategy_key` can have many `strategy_instance_id`s; one `strategy_instance_id` has many `run_id`s over time. |
| **bot_order_namespace** | A unique prefix the bot stamps on every order's `orderRef` / `client_order_id` so it can claim ownership of orders at the broker without using `reqAllOpenOrders`. Per strategy_instance_id. Cold-start broker cross-check (Resolution 2) queries the broker for orders within this namespace and only those. |
| **ib_client_id** | The `clientId` the bot uses on the IBKR Gateway connection. Pinned per strategy_instance_id in the strategy spec so executing and shadow processes never collide. One Gateway, multiple clientIds. |
| **submit_mode** | The broker-adapter-level switch: `"live_paper"` (route through `IbkrBrokerAdapter`, `ib.placeOrder` called) or `"shadow"` (route through `NoSubmitBrokerAdapter`, synthetic fill via declared fill model). Part of the hashed `live_config`, so changing it produces a new `run_id` (graduation requires new ledger). |
| **execution_source** | Required `ExecutionRow` column: `"broker_fill"` (came from IBKR) or `"shadow_sim"` (synthesized by `NoSubmitBrokerAdapter`). Layer A divergence applies only to `broker_fill` rows; Layer B replay applies to both. |
| **Layer A divergence** | `ExecutionDivergence` regime: did broker execution diverge from what this live run intended, on the same data source? Slippage, latency, missed/extra/partial/rejected fills, commission drift. Meaningful only for executing strategies. |
| **Layer B divergence** | `ReplayDivergence` regime: did the live run's observed world diverge from the canonical research world (Polygon / LEAN baseline) when the strategy spec is replayed against archived canonical bars for the same session? Data drift, indicator-state drift, decision drift, coverage gaps, trade-graph drift. Meaningful for both executing and shadow strategies. |
| **shadow_sim** | The `execution_source` discriminator value for synthetic fills produced by `NoSubmitBrokerAdapter`. Always carries `fill_model` + `source_bar_close_ms` in the same `ExecutionRow`. Never written by the executing path. |
| **NoSubmitBrokerAdapter** | The `IBrokerAdapter` implementation used when `submit_mode = "shadow"`. Same interface as `IbkrBrokerAdapter`; `place_order` no-ops on broker submission and produces a `shadow_sim` `ExecutionRow` instead. |
| **command channel** | The file-based control protocol of Resolution 7. Distinct from the durable desired-state sidecar (also Resolution 7); commands are one-shot per-run events, desired-state is persistent operator intent across runs. |
| **MARK_POISONED** | An operator command verb. Writes `poisoned.flag` into the run_dir externally — used when the operator observes a poison condition the bot itself has not detected (e.g. a manual trade hit the account from outside the bot's clientId). |
| **(T3) topology** | The current operational layout: Windows host-venv for Gateway/IBC/bots/host_daemon; Podman compose for observability only. Distinct from (T2) all-containerized and (T4) Linux VPS. Migration to (T4) is gated on the written triggers in ADR 0003. |

### 16.4. Cross-references

- ADRs for the three load-bearing decisions: [0001](architecture/adrs/0001-control-plane-substrate-json-parquet.md), [0002](architecture/adrs/0002-shadow-mode-adapter-level-no-submit.md), [0003](architecture/adrs/0003-operational-topology-host-venv.md).
- Code surfaces affected by the PR queue:
  - `PythonDataService/app/engine/live/host_daemon.py` — registry refactor (PR-A), NSSM wrapping (PR-F).
  - `PythonDataService/app/engine/strategy/spec/` — `StrategySpec` growth (PR-B); new `spy_vwap_reversion.spec.json` (PR-K).
  - `PythonDataService/app/engine/live/artifacts.py` — `DECISION_COLUMNS` resolution from spec; `ExecutionRow` growth (PR-C).
  - `PythonDataService/app/engine/live/live_engine.py` — adapter polymorphism at the order boundary (PR-J).
  - `PythonDataService/app/broker/ibkr/orders.py` — `commissionReport` wiring (PR-G); orderRef namespacing (PR-E).
  - `PythonDataService/app/engine/live/` — new `command_channel.py`, `desired_state.py` (PR-D); `live_state_sidecar.py` (PR-E).
  - `PythonDataService/app/engine/live/reconcile.py` — sibling layer-A / layer-B modules (PR-H, PR-I); existing three-way reconciler untouched.
- Status: Phase 10 prereq RTH dry-run remains the gating operational activity per `docs/ibkr-integration-authority.md` § 11; this design-lock round does not change that status, only the scope of work that follows it.

### 16.5. UI Accuracy + Operator UX Plan (added 2026-05-30)

This section converts the PRD-A-through-D control-plane work into a UI plan. "Accurate" means the browser shows only state that exists in code or artifacts, labels each value by its source, and never implies that a command succeeded before the durable artifact or ack says it did. "User friendly" means the operator can answer the trading-critical questions in one glance: is the bot alive, is it allowed to trade, what did it last see, what did it last decide, what orders/fills exist, and what action is safe next?

#### Current UI truth table

As of PR #387 / PR-D, the shipped Angular surface is `Frontend/src/app/components/broker/broker-paper-run/broker-paper-run.component.*` backed by `Frontend/src/app/services/live-runs.service.ts`.

| UI area | What it currently shows | Source of truth | Accuracy constraint |
|---|---|---|---|
| Run picker + top strip | Run id, `PAPER`, account id, inferred run state, last bar age, decision count | `GET /api/live-runs` and `GET /api/live-runs/{run_id}/status`, derived from `run_ledger.json`, `run_status.json`, `live.log`, parquet row counts, `halt.flag`, `poisoned.flag` | Label as artifact-derived observer state, not broker state. |
| Host Runner card | Daemon health, process state, pid, start time, exit code, host log path; Start / Stop process buttons | Host daemon `GET /health`, `POST /runs/{run_id}/start`, `POST /runs/{run_id}/stop` | "Stop" here is process stop only. It is not PR-D durable `STOPPED` intent and must not be labeled as strategy stop after PR-D controls land. |
| Heartbeat | Parsed `[BAR]` log tail and stale marker when `last_bar_age_s > 90` | `GET /api/live-runs/{run_id}/log-tail` parsed by `app.services.live_log_parser` | If parsing is degraded, show degraded; do not synthesize a heartbeat from file mtimes. |
| Strategy State | Latest decision row fields (`signal`, `ema5`, `ema10`, `rsi14`) and warmup count | `decisions.parquet` tail via `app.routers.live_runs` | Fields are strategy-specific. Missing columns render as absent/unknown, not zero. |
| Position & Exposure | Recent fills and open position derived from run artifacts | `executions.parquet` and `trades.parquet` | Current copy says "readonly run"; after executing-paper mode this must branch on `execution_source` / `submit_mode` so broker fills are not mislabeled as simulated. |
| Safety Flags | `halt.flag`, `poisoned.flag`, latest reconcile receipt link | Files under `artifacts/live_runs/<run_id>/` | Flags are run-scoped safety artifacts. They are distinct from durable desired state under `artifacts/live_state/<strategy_instance_id>/desired_state.json`. |

#### Required additions after PR-D

0. **Persist the run → strategy-instance identity binding before UI-1.**
   - Current gap: `run_id` is derived from `run_ledger.json` identity inputs, while `strategy_instance_id` is supplied later to `run.py start` as `--strategy` and is not written to `run_ledger.json` or `run_status.json`. A fresh run with no decisions has no durable `run_id -> strategy_instance_id` mapping, so `/api/live-runs/{run_id}/status` cannot locate `artifacts/live_state/<strategy_instance_id>/desired_state.json`.
   - Decision: write `strategy_instance_id` into `run_ledger.json` at `init-ledger` time. The ledger is the run's identity record and exists before the engine starts, before warmup, and before the first decision row. This is the only place that gives the UI an O(1), pre-decision mapping.
   - Plumbing: add an explicit `--strategy-instance-id` argument to `init-ledger` and `LiveRunLedger`. Include it in the run-id identity payload unless the migration deliberately chooses `schema_version="1.1"` with documented hash semantics. Then make `start` prefer `ledger.strategy_instance_id`; `--strategy` remains the algorithm module / strategy key and must match or be derived consistently from the spec/ledger.
   - Back-compat: existing ledgers without the field render `strategy_instance_id: null`, `desired_state.path_status: "unknown_no_ledger_binding"`, and no desired-state controls. Do not fall back to the first decision row except as a clearly labeled diagnostic because it is absent in the operator-critical pre-warmup window.
   - Tests: run-ledger schema/hash tests, `init-ledger` CLI parser/writer tests, `start` mismatch/refusal tests if `--strategy` conflicts with ledger identity, and live-runs router tests for missing legacy binding.

1. **Expose durable desired state as first-class status.**
   - Backend: extend the live-runs status response with `strategy_instance_id` and `desired_state: { state, updated_at_ms, updated_by, reason, version, path_status }`.
   - Source: `DesiredStateRepo` at `artifacts/live_state/<strategy_instance_id>/desired_state.json`.
   - Absence must render as `RUNNING (default; no operator intent file)`. Corruption must render as a blocking red state and match `run.py start` refusal semantics.
   - Tests: router fixtures for absent/RUNNING, PAUSED, STOPPED, and corrupt desired-state file; Angular render tests for each state.

2. **Separate process lifecycle from trading intent.**
   - UI has two rows of controls:
     - **Host process:** Start process, stop process. These call the host daemon and own only subprocess lifecycle.
     - **Strategy intent:** Pause, Resume, Stop strategy. These write durable desired state and/or command-channel files and own trading permission.
   - Copy rule: never use one "Stop" button for both meanings. Process stop and strategy stop have different blast radii and different persistence behavior.

3. **Add command-channel controls with ack visibility.**
   - Backend: add an API surface that writes `CommandChannel.write_from_operator` into the selected run's `commands/` directory and reads recent pending/ack files.
   - UI controls: Pause, Resume, Stop, Flatten, Mark Poisoned, Reconcile.
   - User feedback states: `queued` (pending file exists), `acknowledged` (ack file exists), `failed` (ack outcome status error), `stale` (pending older than 3 poll intervals).
   - Flatten must be visually separated as the highest-risk action. Its copy must state current behavior: PR-D aliases FLATTEN to graceful shutdown + flatten and persists durable `STOPPED`; flatten-without-stop is a future primitive.

4. **Make the top strip a quant/operator risk dashboard.**
   - Left to right: mode/account, process state, desired state, run state, heartbeat age, last decision timestamp, latest execution timestamp, flags.
   - Color semantics: green only when process is running, desired state is RUNNING, run state is running/warming/waiting, heartbeat is fresh or no bars yet pre-open, and no flags are set. Yellow for PAUSED, stale heartbeat, waiting for bars, warmup, or degraded parser. Red for STOPPED, halted, poisoned, corrupt desired state, daemon offline for an active run, or command ack failure.
   - Every timestamp remains `int64 ms UTC` over the wire and converts to America/New_York only at render.

5. **Show numbers with provenance and no frontend math authority.**
   - Decision values (`ema5`, `ema10`, `rsi14`, signal, intended action/price/fill model) come from `decisions.parquet`.
   - Execution values (`execution_source`, `fill_model`, `source_bar_close_ms`, fee once PR-G lands) come from `executions.parquet`.
   - UI may format and round for display; it must not recompute indicators, signals, P&L, divergence categories, or fees.
   - Add a compact "source" marker per panel: `decision artifact`, `execution artifact`, `desired-state sidecar`, `command ack`, `host daemon`, `log parser`.

6. **Design the operator flow around safe next actions.**
   - `PAUSED`: show Resume as primary; Start Process is allowed only if the process is down and must boot paused.
   - `STOPPED`: show Resume as the only way to clear durable stop before Start; Start Process should explain the engine will refuse until desired state becomes RUNNING.
   - `poisoned.flag`: disable all start/resume controls and route to the documented cold-start inspection.
   - `halt.flag`: disable start/resume unless the backend proves the halt condition has been cleared.
   - Daemon offline: show the launch command as today, but keep strategy intent visible if artifacts are readable.

#### Implementation slices

| Slice | Scope | Acceptance |
|---|---|---|
| **UI-0 — Identity binding** | Persist `strategy_instance_id` in `run_ledger.json` at `init-ledger`; update `LiveRunLedger`, run-id hash/version semantics, CLI args, host-daemon start assumptions, and legacy-read behavior. | A fresh pre-decision run has an O(1) `run_id -> strategy_instance_id` binding; legacy runs are explicit `unknown`, not guessed from parquet. **✅ Implemented 2026-05-29** (decision: `schema_version` 1.1, `strategy_instance_id` **NOT** in the `run_id` hash → existing run_ids stay valid): `LiveRunLedger.strategy_instance_id` + `build_ledger`/`init-ledger --strategy-instance-id` (optional, empty = legacy/unknown) + `start` prefers `ledger.strategy_instance_id`, falling back to `--strategy` with a warning on legacy ledgers, and keys the desired-state path off the resolved id. `host_daemon` unaffected (it only passes `--strategy` + `run_dir`). Router/UI surfacing remains UI-1+. |
| **UI-1 — Status contract** | Add desired-state fields and command summary to `app.schemas.live_runs`, `app.routers.live_runs`, TS types, and service tests. | API returns accurate absent/PAUSED/STOPPED/corrupt states; no timestamp strings; existing observer UI still renders. |
| **UI-2 — Read-only clarity pass** | Update Paper Run Observer top strip and cards to distinguish process state, run state, desired state, flags, and data provenance. | Playwright/Vitest assertions cover each operator state; no control writes yet. |
| **UI-3 — Durable intent controls** | Add Pause / Resume / Stop strategy controls backed by durable desired-state write API. | Button click writes the sidecar, UI reloads to the new state, corrupt sidecar blocks with clear error. |
| **UI-4 — Per-run command controls** | Add Pause / Resume / Stop / Flatten / Mark Poisoned / Reconcile command-channel writes plus pending/ack timeline. | UI shows queued then acked outcomes using real command files; stale pending commands are obvious. |
| **UI-5 — Execution-aware exposure panel** | Branch exposure copy and tables by `execution_source`, `submit_mode`, and PR-G commission fields. | Executing broker fills, shadow fills, and readonly/simulated rows are labeled differently; no frontend fee/P&L computation. |
| **UI-6 — Divergence/report viewer** | Render Layer A/B reports and latest receipt bundle with pass/fail, counts, tolerances, and artifact hashes. | UI displays report fields produced by Python/reconcile artifacts; no category recomputation in Angular. |

UI-1 and UI-2 should land before any new trading-control buttons. Otherwise the UI would expose powerful actions without first making the current state and source-of-truth boundaries legible.
