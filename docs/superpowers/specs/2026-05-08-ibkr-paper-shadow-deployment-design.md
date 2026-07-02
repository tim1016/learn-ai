# IBKR Paper-Trading Shadow Deployment — Design Spec

**Date:** 2026-05-08
**Status:** Awaiting user approval
**Path:** Path C — Python primary on IBKR paper, QuantConnect Cloud as parallel observer
**Scope:** 15 RTH trading days, SPY only, single strategy (`SpyEmaCrossoverAlgorithm`)
**Companion docs:** [`docs/ibkr-paper-deployment-plan.md`](../../ibkr-paper-deployment-plan.md), [`docs/ibkr-paper-deployment-feedback.md`](../../ibkr-paper-deployment-feedback.md)

---

## 1. Purpose

Deploy the canonical `SpyEmaCrossoverAlgorithm` to Interactive Brokers paper for 15 RTH days, while running the same strategy independently on QuantConnect Cloud, and produce a daily three-way reconciliation report that classifies any divergence into a fixed taxonomy.

The deployment earns three things:

1. **A live IBKR paper receipt.** The Python `LiveEngine` is the production path, per repo philosophy (`CLAUDE.md` guiding principle #3: references are studied and eliminated as runtime dependencies).
2. **A cross-engine signal.** The QC Cloud algorithm is a parallel observer that does NOT touch the IBKR paper account, does NOT receive Python's fills, and does NOT publish numbers a user compares against another number on the production path. It consumes QC's own data feed, runs its own paper simulation, and emits a trade log per day.
3. **A reproducibility artifact.** New `live-runtime` row in [`docs/math-sources-of-truth.md`](../../math-sources-of-truth.md) referencing the week-end reconciliation rollup.

## 2. Strategy spec (frozen for the run)

These values are **frozen on the run-start commit**. Any change is a new run with a new run identity (§ 10).

| Dimension | Value | Source |
|---|---|---|
| Symbol | SPY | [spy_ema_crossover.py:70](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:70) |
| Resolution | 15-min bars consolidated from 1-min | [spy_ema_crossover.py:127](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:127) |
| EMAs | EMA(5), EMA(10) | [engine/indicators/ema.py](../../../PythonDataService/app/engine/indicators/ema.py) |
| RSI | RSI(14), Wilder smoothing | [engine/indicators/rsi.py](../../../PythonDataService/app/engine/indicators/rsi.py) |
| Entry | fresh `EMA5 > EMA10` cross AND `EMA5 − EMA10 ≥ 0.20` AND `50 ≤ RSI ≤ 70` | [spy_ema_crossover.py:180-184](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:180) |
| Exit | exactly 5 consolidated bars (~75 min) after entry, market | [spy_ema_crossover.py:191](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:191) |
| Direction | long-only | — |
| Sizing | `SetHoldings(SPY, 1.0)` → integer share count from consolidated bar close | [spy_ema_crossover.py:189](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py:189), [portfolio.py:155-168](../../../PythonDataService/app/engine/execution/portfolio.py:155) |
| Force-flat | 15:55 ET | `LiveConfig.force_flat_at` |
| Commission (backtest baseline) | $1.00 per order | [test_spy_validation.py:71](../../../PythonDataService/app/engine/tests/test_spy_validation.py:71) |
| Fill model (live parity target) | `FillMode.NEXT_BAR_OPEN` | [fill_model.py:77-81](../../../PythonDataService/app/engine/execution/fill_model.py:77) |

## 3. Single source of truth for the strategy logic

Per the user's prior selection (option b): **ported twice with strict equivalence testing**, with a shared declarative contract.

**Audit source of truth:** the checked-in [`StrategySpec` JSON](../../../PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json) **plus** the checked-in QC audit copy at `references/qc-shadow/SpyEmaCrossoverAlgorithm.py`. Together these two files define what's running. Reviewable in PR.

**Execution copy (QC Cloud):** the QC Cloud workspace runs an instance of the QC algorithm. It is **not** the source of truth — it is an execution copy that must report a matching identifier (file SHA-256 if QC exposes it, or a backtest ID whose output matches the audit copy run on the same fixture) demonstrating it is in sync with the audit copy. Verification at run-start (§ 10): both the audit copy's SHA-256 and the QC Cloud's backtest ID are recorded in the run ledger. Mismatch halts the pre-paper gate.

| Implementation | Role | Equivalence pinned by |
|---|---|---|
| [`app/engine/strategy/algorithms/spy_ema_crossover.py`](../../../PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py) | Canonical Python (production path) | [test_spec_spy_ema_parity.py](../../../PythonDataService/app/engine/strategy/spec/tests/test_spec_spy_ema_parity.py) (Python ↔ Spec); [test_spy_validation.py](../../../PythonDataService/app/engine/tests/test_spy_validation.py) (Python ↔ LEAN CSV, **bit-exact over 2024-03-28 → 2026-03-27**) |
| `references/qc-shadow/SpyEmaCrossoverAlgorithm.py` (audit copy; QC Cloud runs an identical execution copy) | Independent QC port (observer) | (1) `test_qc_python_parity_fixture.py` — strict same-bar parity over a fixture (§ 8.1 Test 1); (2) `test_qc_python_native_feed.py` — operational divergence over a 90-day native-feed window (§ 8.2 Test 2) |

**Decision-time call to a FastAPI strategy service from QC's `OnData` is rejected** (matches user's recommendation): it would create a network dependency in the live decision loop and contradicts math-sovereignty.

## 4. Architecture

```
                                 BEFORE PAPER WEEK
       ┌─────────────────────────────────────────────────────────┐
       │ StrategySpec JSON (frozen contract)                     │
       └────────────────────┬────────────────────────────────────┘
                            │ ports to
                ┌───────────┴───────────────┐
                ▼                           ▼
       ┌────────────────┐          ┌──────────────────┐
       │ Python algo    │          │ QC algo          │
       │ (canonical)    │          │ (port, in QC)    │
       └────────┬───────┘          └────────┬─────────┘
                │                           │
       ┌────────▼──────────┐       ┌────────▼──────────┐
       │ Python backtest   │       │ QC backtest       │
       │ over 90-day window│       │ same window       │
       └────────┬──────────┘       └────────┬──────────┘
                │                           │
                └────────────┬──────────────┘
                             ▼
                tolerance-pinned cross-engine parity
                          GO / NO-GO

                                 PAPER WEEK
                  Python primary                          QC observer
       ┌────────────────────────────────┐       ┌────────────────────────────┐
       │ this repo, runs on host        │       │ QuantConnect Cloud         │
       │ ┌──────────────────────────┐   │       │ ┌──────────────────────┐  │
       │ │ LiveEngine               │   │       │ │ QC SpyEmaCrossover   │  │
       │ │ + LivePortfolio          │   │       │ │ Algorithm            │  │
       │ │ + LiveContext            │   │       │ │ (paper sim only)     │  │
       │ └──────┬───────────────────┘   │       │ └──────────┬───────────┘  │
       │        │ ib_async via          │       │            │              │
       │        │ app/broker/ibkr/      │       │            │ daily export │
       │        ▼                       │       │            │ trades+bars+ │
       │ ┌─────────────────┐            │       │            │ indicators   │
       │ │ IB Gateway      │            │       └────────────┼──────────────┘
       │ │ DU paper acct   │            │                    │
       │ │ client_id=42    │            │                    │
       │ └─────────────────┘            │                    │
       │        │                       │                    │
       │        ▼                       │                    │
       │ artifacts/live_runs/<run_id>/  │                    │
       │   trades.parquet               │                    │
       │   bars.parquet                 │                    │
       │   indicators.parquet           │                    │
       │   executions.parquet           │                    │
       └────────┬───────────────────────┘                    │
                │                                            │
                │                                            ▼
                │                                  artifacts/qc/<date>/
                │                                    trades.csv
                │                                    indicators.csv
                │                                            │
                └─────────┬──────────────────────────────────┘
                          ▼ both consumed by
                  ┌──────────────────────────┐
                  │ reconcile.py             │
                  │ three-way divergence     │
                  │ classification           │
                  └──────────────┬───────────┘
                                 ▼
              docs/references/reconciliations/
              spy-ema-crossover-paper-2026-XX/
                day-N.md (×15)
                week.md (rollup)
```

## 5. Roles and isolation

**Python (this repo) — production path:**
- Sends orders to IBKR paper account
- Owns `IBKR_CLIENT_ID = 42` for the run; configurable but pinned across the 15 days
- Consumes IBKR real-time 5-second TRADES bars via [`app/broker/ibkr/bars.py`](../../../PythonDataService/app/broker/ibkr/bars.py), aggregated to closed 1-min, consolidated to 15-min
- Persists artifacts to `PythonDataService/artifacts/live_runs/<run_id>/`

**QuantConnect (QC Cloud) — observer:**
- Runs `SpyEmaCrossover` algorithm on QC's paper trading simulator, end-of-day mode is acceptable; live mode preferred for timestamp alignment
- **Does NOT connect to IBKR** under any configuration. Even if QC supports IBKR live deploy, that flag is OFF for this run.
- Uses QC's data feed (Algoseek / QC curated equities) — this is the point: data divergence is a measured dimension, not a bug to suppress
- Daily export to `PythonDataService/artifacts/qc/<date>/` (manual via QC Cloud UI is acceptable; QC API automation is a Phase B sub-task)

**Key isolation invariant:** the IBKR paper account has exactly one client connected during the paper window — Python's `IbkrClient(client_id=42)`. No QC Cloud connection, no manual TWS user, no second Python process. This eliminates split-brain on the same IBKR account.

## 6. Reconciliation report — three-way

After force-flat each day, `app/engine/live/reconcile.py` produces `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX/day-N.md`.

### 6.1 Decision-time table (per consolidated 15-min bar)

| `bar_close_ms` (int64 UTC) | `python_signal` | `python_ema5` | `python_ema10` | `python_rsi` | `qc_signal` | `qc_ema5` | `qc_ema10` | `qc_rsi` | `cross_engine_class` | `python_fill_price` | `python_intended_price` | `fill_class` |

Schema details:
- `python_signal` and `qc_signal` ∈ `{ENTER, EXIT, HOLD}`
- `python_intended_price` = consolidated bar close at signal time (the reference price `set_holdings` used for share-count math)
- `python_fill_price` = actual IBKR fill price for the bar's order (NaN if no signal)
- `cross_engine_class` ∈ `{none, data, engine}` (see § 6.2)
- `fill_class` ∈ `{none, within_tol, breach}` (see § 6.3)

### 6.2 Cross-engine divergence classification (per user's contribution)

| Class | Definition | Identification rule |
|---|---|---|
| **none** | Indicators and signals agree | indicators within tolerance AND signals identical |
| **data divergence** | Indicators disagree beyond tolerance | EMA5 \| EMA10 \| RSI delta exceeds tolerance (signals may agree or disagree — both indicate the underlying data feeds differ materially; signals merely reveal whether the difference matters at the threshold) |
| **engine divergence** | Indicators agree, signals differ | indicators within tolerance AND signals differ — **this is a real bug**: same inputs, different decisions |

Engine-class divergences trigger the halt rule (§ 6.4).

### 6.3 Tolerances

Two distinct contexts, two distinct tolerance sets. Loose tolerances are fine for cross-data operational shadowing but mask engine bugs in same-bar parity — they MUST NOT be used for Test 1.

**Same-bar engine parity (§ 8.1 Test 1, pre-paper fixture-driven gate):**

Both engines consume the exact same bar fixture. Indicator math is deterministic. Any non-trivial divergence is a real bug.

| Quantity | Tolerance | Rationale |
|---|---|---|
| EMA5, EMA10 | `atol = 1e-9` (Decimal-exact preferred) | Same input bars; both ports of the same SMA-seeded LEAN formula. Beyond floating-point representation noise, no source of divergence is acceptable. |
| RSI Wilder | `atol = 1e-9` | Same input bars; deterministic Wilder smoothing. |
| Trade timestamps | exact | int64 ms UTC; deterministic given input bars |
| Trade prices | exact (Decimal) | both engines fill at identical fixture-driven prices |
| Total trade count | exact | — |
| Per-trade entry/exit time, price, direction, size | exact | — |

If Test 1 fails at this strictness, the QC port is not equivalent to the Python algorithm. The pre-paper gate does not pass.

**Cross-feed shadowing (§ 8.2 Test 2 + paper week, day-by-day):**

[DECISION — confirm before plan generation]

Each engine consumes its native data feed (QC's data feed in QC Cloud; Polygon historical for the Test 2 backtest, IBKR live during paper week). Divergence is expected and is the measured signal.

| Indicator | Proposed `atol` | Rationale |
|---|---|---|
| EMA5, EMA10 | `0.10` (price units, USD) | Polygon vs TradingView already shows ~$1.50–$4.00 divergence on SPY. QC vs IBKR feeds are both major; ~10 cents is the conservative cross-feed envelope. Tighter triggers data-class divergence flags; looser would let real engine bugs leak through during paper week. |
| RSI | `2.0` (RSI units) | RSI is bounded 0–100; the entry band 50–70 is 20 units wide. 2.0 ≈ 10% of band. |

**Strategy-level tolerances (pinned in code, reuse verbatim):**

From [`test_spy_next_bar_open_validation.py:134-145`](../../../PythonDataService/app/engine/tests/test_spy_next_bar_open_validation.py:134), already canonical:

| Metric | Tolerance | Type |
|---|---|---|
| `final_equity` | 0.5% | relative |
| `net_profit` | 2% | relative |
| `total_fees` | exact | — |
| `total_trades` | exact | — |
| `winning_trades`, `losing_trades` | ±2 | absolute |
| `win_rate` | ±5 pp | absolute |
| `profit_factor` | 10% | relative |
| `max_drawdown_pct` | ±0.5 pp | absolute |
| `sharpe_ratio` | ±0.15 | absolute |

**Fill divergence tolerances (Python's IBKR fill vs Python's intended price):**

From [`ibkr-paper-deployment-plan.md:421-424`](../../ibkr-paper-deployment-plan.md):

| Dimension | Tolerance |
|---|---|
| Fill price | `atol = Decimal("0.05")` (±5¢, broker-slippage envelope) |
| Fill time | ±5 seconds vs next-bar open |
| Fill quantity | exact |

### 6.4 Next-session halt gate (next-morning, NOT intra-day)

[DECISION — confirm before plan generation]

This is the **morning gate**: at the start of each trading session, check whether the prior day's receipt is intact. If any condition below was true on the prior day, the runner does not place new orders today.

Distinguish from § 7:
- **§ 6.4 says:** "Prior-day receipt failed; today may not place new orders." (next-session gate)
- **§ 7 says:** "Runtime integrity broken; current run stops now and does not auto-resume." (intra-day fatal halt)

Halt the next morning, do **not** place new orders, if any of these occurred on the prior day:

1. Any **engine-class** cross-engine divergence (§ 6.2)
2. Two or more unclassified divergences within a single day
3. Strategy-level tolerance breach on `total_trades` (exact match required) or `final_equity` (>0.5% relative)
4. Any unhandled exception in `live.log`
5. Any IBKR connection drop > 60 seconds while a position is open
6. **Missing or invalid daily artifacts** — `day-(N-1).json`, `day-(N-1).parquet`, or `day-(N-1).md` is missing, fails schema validation, or fails its SHA-256 inclusion check (§ 6.5). The reconciliation script writes a halt sentinel that the runner checks at pre-flight.
7. **Missing or invalid QC export** — the prior day's QC export under `artifacts/qc/<date>/` (`trades.csv`, `indicators.csv`) is missing, fails schema validation, or its SHA-256 doesn't match what `day-(N-1).md` reports. There is no "we'll catch up tomorrow" path; the morning halt is mechanical.
8. Pre-flight halt-rule trip the next morning (NTP offset > 1 s, unexpected position on the IBKR paper account, missing run-state file, **dirty source tree per § 9**)

A halted run is **not deleted** — partial reconciliation through day N-1 is still a valuable receipt. Resuming requires a new `run_id` (§ 10).

### 6.5 Daily artifact format

[DECISION — confirm before plan generation]

Per day:
- `day-N.md` — human-reviewable Markdown report (canonical artifact, **committed to git**)
- `day-N.json` — machine-readable companion (NOT committed; persisted under `live_runs/<run_id>/reconcile/`)
- `day-N.parquet` — bar-level reconciliation table (NOT committed; persisted alongside the JSON)

**SHA-256 inclusion (audit-grade Markdown):** the committed `day-N.md` includes a hash manifest near the top with the SHA-256 of every uncommitted artifact it summarizes. This makes the Markdown receipt audit-grade without requiring large machine artifacts in git history.

```yaml
artifact_hashes:
  reconcile_json:           "<sha256 of day-N.json>"
  reconcile_parquet:        "<sha256 of day-N.parquet>"
  python_executions_parquet:"<sha256 of live_runs/<run_id>/executions.parquet file or dataset directory (cumulative through day N)>"
  python_trades_parquet:    "<sha256 of live_runs/<run_id>/trades.parquet file or dataset directory (cumulative)>"
  qc_export_trades:         "<sha256 of artifacts/qc/<date>/trades.csv>"
  qc_export_indicators:     "<sha256 of artifacts/qc/<date>/indicators.csv>"
  run_ledger:               "<sha256 of live_runs/<run_id>/run_ledger.json (immutable after run-start)>"
```

Hashes are computed by the reconciliation script on the bytes-as-written for files, or on the sorted relative path + bytes of every file in an artifact directory, and embedded in the Markdown front matter or a fenced YAML block. The reconciliation script also writes a sidecar `live_runs/<run_id>/reconcile/day-N.hashes.json` containing the same hashes for machine verification — downstream tooling (and the next-morning pre-flight halt rule § 6.4 #6, #7) can verify the committed Markdown's hashes match the artifacts on disk.

Week-end `week.md` rollup includes the day-by-day hash manifest plus its own SHA-256 manifest of any week-aggregate artifacts.

## 7. Fill ingestion via execId/permId — fatal intra-day halt (per user's contribution)

In Path C, Python is the order source — but auditing fills by broker primary keys still matters.

The existing `IbkrBrokerAdapter.start_event_stream` callback (per [`ibkr-paper-deployment-plan.md`](../../ibkr-paper-deployment-plan.md) implementation note 2026-05-04) is augmented to:

- Index every received fill by `(execId, permId, account_id)` not by `ib.trades()` cached state
- Persist all received executions to `live_runs/<run_id>/executions.parquet` whether or not Python originated them, **regardless of `clientId`**

### 7.1 Two intra-day fatal halt triggers

**Trigger A — Outside-mutation: any unowned execution.** If any execution under the DU account has an `(execId, permId)` not linked to a Python-owned `client_order_id`, fire the fatal halt **regardless of `clientId`**. A `clientId == 42` filter is insufficient — TWS itself can place orders under a different `clientId` (or `clientId=0`) when a human clicks a button, and those would slip past a same-client check. Linking by `(execId, permId)` against the Python-owned-orders table catches all of them.

**Trigger B — Lost fill: any unfilled Python order.** A Python order whose `(client_order_id)` has no matching execution within its expected fill window (next-bar-open + slack), or remains unfilled at end-of-day. Both indicate broker-state divergence from Python's ownership ledger.

### 7.2 Fatal-halt semantics (NOT a next-session gate)

When either trigger fires the runtime is treated as compromised. Once broker executions disagree with Python's ownership ledger the receipt is contaminated; **the run does not auto-recover**.

The fatal halt does, in this exact order:

1. **Stop the strategy loop immediately.** No further `on_data` calls; the consolidator drains its buffer but emits no decisions.
2. **Cancel only Python-owned open orders.** Use `client_order_id` to filter; do NOT cancel orders the runtime cannot identify as ours, since they may belong to a human operator or another process.
3. **Persist partial artifacts and a `halt_reason`.** Final `executions.parquet`, `trades.parquet`, `bars.parquet`, `indicators.parquet` are flushed; a sidecar `live_runs/<run_id>/halt.json` records `{trigger: "outside_mutation" | "lost_fill", details: {...}, halted_at_ms: int64, last_clean_bar_close_ms: int64}`.
4. **Disable automatic reconnect/resume for the current `run_id`.** The runner writes a `live_runs/<run_id>/poisoned.flag` sentinel; any subsequent `python -m app.engine.live.run --run-id <same>` invocation reads the flag and refuses to start.
5. **Require a new `run_id` after manual account reconciliation.** A new `run_id` (§ 10) implies a new `code_sha`, a new run ledger, and a fresh start. Resuming the same run_id is forbidden.
6. **Manual operator force-flat is the only allowed action on the contaminated account.** The runner exposes a clearly labeled `python -m app.engine.live.run --emergency-flatten --account DU... --confirm` path that places only liquidating orders, logs each one, and writes to a separate `live_runs/<run_id>/emergency_flatten.log`. This path is OFF by default and never auto-triggered.

The reasoning: once Python's ownership ledger disagrees with broker reality, every subsequent decision is suspect (position size unknown, risk unknown, P&L unknown). Continuing the run masks the discrepancy in the receipt. The cost of stopping is at most one trading day of missed signals; the cost of continuing is a corrupted 15-day artifact.

§ 5's isolation invariant says no other client should be present; § 7 is defense in depth in case the invariant is violated.

## 8. Pre-paper-week QC ↔ Python equivalence gates

Two separate tests prove different things. Both must pass before paper week starts. Conflating them lets a "looks close enough" pass disguise either a port bug or a data-feed difference, and the gate cannot tell which.

### 8.1 Test 1 — Same-bar engine parity (strict)

**Purpose:** prove that QC's algorithm and Python's algorithm, given identical input bars, produce identical decisions and trades. This isolates engine equivalence from data-source differences.

**Fixture:** the same SPY minute-bar fixture used by [`test_spy_validation.py`](../../../PythonDataService/app/engine/tests/test_spy_validation.py) (LEAN-formatted minute data, the bit-exact LEAN parity reference). The fixture is fed to **both** engines:
- Python: read by `LeanMinuteDataReader` → `BacktestEngine`
- QC: a custom data consolidator / replay harness in QC Cloud loads the same fixture (CSV / Lean format → QC bars). The QC algorithm runs the QC paper sim on this fixture, **NOT** QC's native data feed.

**Window:** the LEAN parity fixture's full window (2024-03-28 → 2026-03-27).

**Tolerances:** § 6.3 same-bar engine parity (`atol=1e-9` on indicators; exact on trades, timestamps, prices, count).

**Test file:** `app/engine/tests/test_qc_python_parity_fixture.py`. The QC side is a one-time export (trades + per-bar indicators) checked in to `references/qc-shadow/backtests/lean-parity-fixture/`. The Python side runs live each test invocation.

**⚠ Phase A critical path:** running QC Cloud against the LEAN parity fixture is **NOT a CSV export chore**; it is a real QC-side build that requires:
- Loading external CSV / Lean-format minute bars into QC's data pipeline (custom `IDataConsolidator` or `BaseData` subclass plus a `DataReader` for the fixture format)
- Disabling QC's native data feed for the duration of the Test 1 backtest
- Verifying the QC algorithm receives exactly the bar count and timestamps the Python fixture provides

This is the riskiest piece of Phase A by a wide margin and is on the critical path: without it, there is no clean engine-equivalence proof.

**Fallback decision point:** if the QC custom-data replay harness cannot be built within ~5 working days inside Phase A, **paper week is paused** rather than auto-substituting native QC data. Native QC data may serve as the § 8.2 Test 2 shadow comparison, but it does **NOT** substitute as the engine-equivalence gate. There is no auto-fallback. The user reviews and decides one of:

1. Extend Phase A to complete the harness (recommended)
2. Defer paper week indefinitely and ship Path A only (Python primary, no QC observer — falls back to the existing v2 plan with no dual-engine signal)
3. Accept native-QC-only shadow with the explicit caveat in the spec and reconciliation reports that engine-equivalence was not strictly proven (forfeits the dual-engine signal that motivated Path C)

The default position is option 1 unless the user explicitly downgrades.

**Gate:** must pass strictly. Engine-class divergence here is a real bug in the QC port and blocks paper week. No partial-pass allowed.

### 8.2 Test 2 — Native-feed shadowing (operational tolerances)

**Purpose:** prove that QC's algorithm running on QC's native data feed and Python's algorithm running on Polygon historical produce trade lists that agree within operational tolerance. Divergences here are classified per § 6.2 (data-class is informational; engine-class is blocking).

**Window:** [DECISION — confirm before plan generation]

Proposed evaluation window: **`[2025-08-01 00:00 America/New_York, 2025-11-01 00:00 America/New_York)`** — half-open interval, RTH-only (09:30–16:00 ET, NYSE calendar), no extended hours, ~63 trading days.

**Warmup pre-roll:** load bars from **`2025-07-15 09:30 America/New_York`** (~10 RTH days before the evaluation window) for indicator warmup.
- EMA(10) reaches its SMA-seeded steady state in 10 bars.
- RSI(14) Wilder needs 14 + buffer.
- 10 RTH days × ~26 fifteen-min bars/day ≈ 260 bars; comfortable for both indicators to fully stabilize.
- **Bars before 2025-08-01 are loaded and consumed for indicator state, but signals fired during pre-roll are not scored or reported.** Both engines drop the pre-roll trades from the comparison set; only trades whose `entry_time` is within the half-open evaluation window count.

**Procedure:**

1. **Python:** run `BacktestEngine.run(SpyEmaCrossoverAlgorithm())` with `FillMode.NEXT_BAR_OPEN`, SPY 15-min bars from Polygon, full pre-roll + evaluation window. Persist `python_backtest_<window>.{trades,bars,indicators}.parquet`. Trades during pre-roll are dropped from the reporting set.
2. **QC:** run the QC algorithm in QC Cloud with QC's native data feed, same evaluation + pre-roll dates, RTH-only. Export trade log + per-bar indicators to `references/qc-shadow/backtests/<window>/`.
3. **Test:** `app/engine/tests/test_qc_python_native_feed.py` loads both, classifies every divergence per § 6.2, asserts:
   - Zero engine-class divergences
   - Strategy-level tolerances from § 6.3 (cross-feed shadowing) hold
   - Data-class divergences are reported in the test output for review
4. **Gate:** engine-class divergences are blocking. Data-class divergences are documented and the user signs off (PR reviewer comment) before paper week starts.

**Why two tests, not one:** Test 1 proves the engines are equivalent. Test 2 proves the native-feed operational pipeline is stable enough for paper. A 90-day "looks close enough" pass can disguise either a port bug or a data-feed mismatch; with the split, Test 1 isolates the port and Test 2 reveals only the data-feed signal.

## 9. Operational safety (per user's contribution)

| Control | Value |
|---|---|
| IBKR client ID | `42` (Python only; no other client connects during the run) |
| `IBKR_READONLY` | `false` (Python is the order source) |
| `IBKR_MODE` | `paper` (sentinel; `orders.py:76-138` enforces) |
| Account ID prefix | `DU` (sentinel check in `IbkrClient.connect`) |
| Max orders per day | 4 (strategy emits ≤ 1 entry + 1 exit per day in 15-min mode; 4 leaves a margin for force-flat and one retry) |
| Max position size | 100% NLV in SPY (matches `set_holdings(SPY, 1.0)`) |
| Halt on unexpected position | yes — any non-SPY position or any short position halts the runner |
| Halt on missing bars | yes — if `stream_minute_bars` produces no bar in the trailing 5 minutes during RTH |
| Halt on stale clock | yes — NTP offset > 1 s from `pool.ntp.org` at run-start and every hour |
| Halt on duplicated bars | yes — already in [`app/broker/ibkr/bars.py`](../../../PythonDataService/app/broker/ibkr/bars.py) (raises `IBKRBarStreamError`) |
| Halt on divergence above tolerance | yes — see § 6.4 |
| **Halt on dirty source tree at run-start** | yes — `git status --porcelain` must be empty for tracked files in `PythonDataService/`, `references/qc-shadow/`, and any spec/config path referenced by the run ledger. The runner refuses to start with uncommitted changes; this is what makes `code_sha` (§ 10) meaningful. The runner does **not** persist a "dirty diff hash" — runs from a dirty tree do not start. Untracked files outside the run scope are tolerated. |
| TWS / Gateway daily restart | scheduled cron 03:00 ET; runner reconnects on `client.ib.disconnectedEvent` |
| Reconnect timeout | 60 s; on timeout, halt and write partial reconciliation |

## 10. Run identity

Reuse the existing canonical-JSON SHA-256 scheme at [`app/research/runs/hashing.py`](../../../PythonDataService/app/research/runs/hashing.py).

`run_id = sha256_canonical_json({...})` over:

- `code_sha` — `git rev-parse HEAD` on run-start commit. **Only meaningful because the runner refuses to start with a dirty tree** (§ 9 halt rule). The run ledger does NOT persist a "dirty diff hash"; runs from a dirty tree do not start.
- `strategy_spec_path` + content SHA-256 of the spec JSON
- `qc_audit_copy_sha256` — SHA-256 of `references/qc-shadow/SpyEmaCrossoverAlgorithm.py` at run-start (§ 3 audit source of truth)
- `qc_cloud_backtest_id` — QC Cloud's identifier for the most recent QC backtest used to verify the QC Cloud execution copy is in sync with the audit copy (§ 3)
- `live_config` — resolved values, not raw env vars
- `account_id` (DU…)
- `start_date_ms` — int64 ms UTC, the first bar's start

Persisted at `live_runs/<run_id>/run_ledger.json`. Same `run_id` is referenced in every reconciliation artifact, in both pre-paper gate test outputs (§ 8), and in the new `live-runtime` row added to [`docs/math-sources-of-truth.md`](../../math-sources-of-truth.md) at week-end.

## 11. Where the Python service runs

[DECISION — confirm before plan generation]

**Proposed:** same machine as the existing `polygon-data-service` container, but a **separate container** named `python-live-engine` started from the same image with `command=["python", "-m", "app.engine.live.run"]`.

Reasons:
- Isolates the long-running event loop from the FastAPI request handlers (a crash of one doesn't take the other)
- The existing podman compose stack already has the network plumbing for an IBKR Gateway sidecar
- Same code, same Python env — no second build target

**Alternative considered:** dedicated VPS. Better network isolation, higher operational cost. Worth it only if the dev machine is unreliable.

**Restart plan:** if the host reboots, the live-engine container restarts via podman's restart policy; on start it reads `live_runs/<run_id>/state.json`, verifies the run is still active for the current trading day, validates IBKR account state matches the persisted snapshot, and resumes. If state mismatches (positions don't agree), it halts.

## 12. Build sequence

### Status of Phases 1–7 (existing plan)

Per the [`ibkr-paper-deployment-plan.md`](../../ibkr-paper-deployment-plan.md) implementation note 2026-05-04, the bar adapter, fill draining, force-flat barrier, and live-engine modules are implemented:

- Phase 1 — Adapter scaffolding (`app/engine/live/`)
- Phase 2 — Real-time minute-bar source (`app/broker/ibkr/bars.py`)
- Phase 3 — `LivePortfolio`
- Phase 4 — `LiveContext`
- Phase 5 — `LiveEngine` driver
- Phase 6 — Replay parity test (`test_live_engine_replay.py`)
- Phase 7 — Broker-lifecycle-collapse test (`test_live_engine_collapse.py`)

[`reconcile.py`](../../../PythonDataService/app/engine/live/reconcile.py) and [`run.py`](../../../PythonDataService/app/engine/live/run.py) are stubs. Phases 1–7 should be re-verified passing on `main` as part of Phase D pre-paper dry run; if any regress, fix before continuing.

### New work (Path C extension)

**Phase A — QC algorithm port + pre-paper-week parity gates** (~1.5–2 weeks; the QC custom-data harness is the critical-path item)
- Write the QC `SpyEmaCrossoverAlgorithm` in QC Cloud
- Check in audit copy at `references/qc-shadow/SpyEmaCrossoverAlgorithm.py`
- **Critical path:** build a custom-data replay harness in QC Cloud that loads the LEAN parity fixture and disables QC's native feed for Test 1 (§ 8.1). Target: 5 working days. Hits the fallback decision point in § 8.1 if not buildable.
- Run Test 1 (§ 8.1) in QC Cloud, export to `references/qc-shadow/backtests/lean-parity-fixture/`
- Run Test 2 (§ 8.2) in QC Cloud over the native-feed window, export to `references/qc-shadow/backtests/<window>/`
- Run the same windows in Python, persist artifacts
- New tests `test_qc_python_parity_fixture.py` and `test_qc_python_native_feed.py` enforce tolerances from § 6.3
- **Gate:** Test 1 must pass strictly; Test 2 must pass with no engine-class divergences. Both before any further phase.

**Phase B — Reconciliation tooling** (~3–4 days)
- Implement `app/engine/live/reconcile.py` per § 6 (currently a stub)
  - Three-way taxonomy classifier
  - QC trade-log + indicator-log loader (CSV/JSON parser, schema documented in module docstring)
  - Daily Markdown report generator
  - Week-end rollup
- Tests against synthetic three-way fixtures (data-class, engine-class, fill-class triggers)
- Output schema: § 6.5

**Phase C — Run CLI + halt rules** (~3–4 days)
- Implement `app/engine/live/run.py` per § 9 (currently a stub)
  - Honor every halt rule from § 6.4 and § 9
  - NTP check at start + hourly
  - Max-orders-per-day enforcement
  - Unexpected-position guard
  - Daily reconcile invocation as a post-force-flat step
- Tests for each halt rule (synthetic triggers; no live IB)

**Phase D — Pre-paper dry run** (~1 day)
- Run the full stack against IB Gateway with `IBKR_READONLY=true` for one trading day, no orders placed
- Verify: bar stream OK, halt rules don't fire spuriously, NTP check passes, reconcile.py produces a sensible day-0 report against synthetic QC export
- Verify: Phases 1–7 tests still pass on the run-start commit

**Phase E — Paper week** (15 RTH days = 3 calendar weeks)
- 15 RTH days of live paper running
- Daily review of the `day-N.md` report (manual, by user)
- Week-end `week.md` rollup
- New row in [`docs/math-sources-of-truth.md`](../../math-sources-of-truth.md) under `live-runtime` referencing the rollup
- PR titled "stage-2 parity receipt: SpyEmaCrossover paper week of 2026-XX"

**Total remaining engineering time:** ~3.5–4 weeks (Phases A–D, with Phase A's QC custom-data harness as the critical path), then 3 calendar weeks of paper running (Phase E).

## 13. Open decisions (confirm before plan generation)

These are the explicit defaults this design proposes. Each has a `[DECISION — confirm before plan generation]` marker inline.

| # | Decision | Proposed default | Section |
|---|---|---|---|
| 1a | Same-bar engine-parity tolerance (Test 1) | `atol=1e-9` on indicators; exact on trades, timestamps, prices, count | § 6.3 |
| 1b | Cross-feed shadowing tolerance (Test 2 + paper week) | EMA `atol=0.10`, RSI `atol=2.0` | § 6.3 |
| 2 | Halt rule details | List in § 6.4 (now includes missing/invalid daily artifacts and QC export, plus dirty-tree refusal) | § 6.4, § 9 |
| 3 | Daily artifact format + SHA-256 inclusion in committed Markdown | Markdown (committed, with hash manifest) + JSON + Parquet (uncommitted) | § 6.5 |
| 4 | Test 2 evaluation window + pre-roll | `[2025-08-01 00:00, 2025-11-01 00:00)` America/New_York RTH-only; pre-roll from 2025-07-15 | § 8.2 |
| 5 | Service host | Same machine as existing podman stack, separate container | § 11 |
| 6 | Paper account starting balance | Accept whatever the IBKR DU account has, do not rebase | § 11 |
| 7 | QC trade-log export mechanism | Manual CSV/JSON via QC Cloud UI for v1; QC API automation deferred | § 5 |

(The earlier item 8 — "Where audit QC source lives" — is resolved by the rewritten § 3: `references/qc-shadow/SpyEmaCrossoverAlgorithm.py` plus the StrategySpec JSON form the audit source of truth; QC Cloud is an execution copy that must verify in sync.)

## 14. Out of scope

- **Real-money trading.** This is paper only; live mode is a separate runner per [`ibkr-paper-deployment-plan.md`](../../ibkr-paper-deployment-plan.md) assumption #5.
- **Multi-symbol live.** [`engine.py:178-184`](../../../PythonDataService/app/engine/engine.py:178) raises on > 1 symbol; explicitly Phase 1 single-symbol.
- **Options-strategy live.** `spy_ema_crossover_options.py` is a separate algorithm; not in this run.
- **Modifying the canonical Python algorithm.** It is frozen for this run; any change is a new `run_id`.
- **QC API automation for daily exports.** v1 uses manual export; a QC API integration is a follow-up if 15 days of manual export is annoying.
- **GraphQL passthrough or Angular UI for the live engine.** Out of scope; the receipt artifact is the Markdown rollup committed to git.

## 15. Why this design

- **Aligned with repo philosophy** (`CLAUDE.md` #3: references are studied and eliminated as runtime dependencies). QC observes; QC does not produce numbers users compare against another number on the production path.
- **Reuses ~70% of the Python live-runtime code** that already exists per the v2 plan's implementation note.
- **Gives the dual-engine signal the user wanted**: data vs engine vs fill divergence, classified daily, with engine-class divergence a hard stop.
- **Single source of truth is the StrategySpec JSON**, ported twice with strict equivalence proven before any paper order. No network dependency in QC's `OnData`.
- **Operational safety has explicit halt rules** rather than "best effort" wording.
- **Run identity uses the existing canonical-JSON SHA-256 scheme** rather than inventing a parallel reproducibility mechanism.

---

**Companion artifacts produced by this design:**

- [ ] (new) `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` — this spec
- [ ] (Phase A) `references/qc-shadow/SpyEmaCrossoverAlgorithm.py` (audit copy)
- [ ] (Phase A) `references/qc-shadow/backtests/lean-parity-fixture/{trades,indicators}.csv` (Test 1 input)
- [ ] (Phase A) `references/qc-shadow/backtests/<window>/{trades,bars,indicators}.csv` (Test 2 input)
- [ ] (Phase A) `PythonDataService/app/engine/tests/test_qc_python_parity_fixture.py` (Test 1, strict same-bar parity)
- [ ] (Phase A) `PythonDataService/app/engine/tests/test_qc_python_native_feed.py` (Test 2, operational native-feed shadowing)
- [ ] (Phase B) `PythonDataService/app/engine/live/reconcile.py` (replaces stub; emits SHA-256 manifest in `day-N.md`)
- [ ] (Phase C) `PythonDataService/app/engine/live/run.py` (replaces stub; honors halt rules including dirty-tree refusal)
- [ ] (Phase E) `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX/day-{1..15}.md`
- [ ] (Phase E) `docs/references/reconciliations/spy-ema-crossover-paper-2026-XX/week.md`
- [ ] (Phase E) new row in [`docs/math-sources-of-truth.md`](../../math-sources-of-truth.md) under `live-runtime`
