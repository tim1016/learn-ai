# Phase 3 — QC trade-level P&L parity design

**Status:** approved-in-conversation (Tim, 2026-05-11). Plan + scaffolding implementation follow.
**Predecessors:**
- `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md` — Phase 1 spec (plumbing)
- `docs/references/quantconnect-precomputed-predictions.md` — Phase 1 reference doc + runbook

## Goal

Validate that learn-ai's backtest engine, fed QC's exact ground-truth inputs (predictions + price bars), produces a trade log that matches QC's recorded backtest within explicit per-field tolerances.

Acceptance claim:

> Given QC's captured `qc_orders.json` + `qc_price_history.csv` for the AAPL single-symbol degenerate of QC's "Precomputed ML Predictions" tutorial algorithm, our engine produces a trade log where for **every** trade: `symbol`, `quantity`, `direction`, `order_type` are bit-exact; `fill_price` matches within `atol=$0.01`; `order_fee_amount` matches within `atol=$0.01` (gated on Branch A — see §2.2.3); per-trade P&L matches within the propagated tolerance `Σ |fill_qty_i| × $0.01 + Σ fee_atol_i`; and `fill_time` resolves to the same trading date.

If QC's order export turns out to be insufficient for trade-level comparison, the claim downgrades to **B — equity-curve parity** and the missing QC fields are documented as the blocker. The capture-smoke step (§2.1.2) determines which path is live.

## Non-goals (deferred)

| Out of scope | Where it goes |
|---|---|
| Multi-symbol top-N ranking (QC's full algorithm) | **Phase 4** — needs `StrategySpec` `PortfolioConstruction` extension |
| Minute-resolution fill timing | **Phase 3.5** — only if Phase 3 reconciliation shows daily-bar fills don't explain QC's `lastFillTime` |
| Faithful LEAN `EquityFillModel` port (partial fills, halts, gap auctions) | **Phase 3.5** — only on observed `FILL_PRICE_DRIFT` clustering |
| Sub-cent (`1e-4`) fill-price tolerance | Tightenable only if fixture demonstrably supports it |
| Daily MTM equity curve and Sharpe parity | Diagnostic only; reported, not asserted |
| Matching QC order submission timestamp / scheduler semantics beyond same trading-date decision | Phase 3 acceptance is fill/trade parity on daily bars; scheduler-timestamp parity is not in scope |
| QC live-trading parity | Out of project |

## Architecture

Four new components in `PythonDataService/`, plus reuse of existing engine surfaces. Hard boundary: **no public `StrategySpec` / manifest schema changes.** Soft boundary: small extensions to `RunRequest` test harness wiring are permitted.

```
┌──────────────────────────────────────────────────────────────────────┐
│  QC Cloud (run-once capture)                                         │
│  Notebook: GBM predictions → research-to-backtest-factors.json      │
│             (already captured in PR #215)                            │
│  Algorithm: PredictionUniverse + set_holdings @ 8am ET               │
│    ├─ AAPL-only universe override (single-symbol degenerate)         │
│    ├─ /backtests/orders/read response → qc_orders.json              │
│    ├─ qb.history(AAPL) → qc_price_history.csv                       │
│    └─ /backtests/read response → qc_equity.json (diagnostic)         │
└──────────────────────────────────────────────────────────────────────┘
                              │ fixture commit
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Repo: tests/fixtures/golden/qc-aapl-phase3/                         │
│  ├─ qc_orders.json         (canonical fill ground truth)             │
│  ├─ qc_price_history.csv   (daily bars; engine consumes via reader)  │
│  ├─ qc_equity.json         (diagnostic)                              │
│  ├─ qc_algorithm_screenshot.png  (audit trail)                       │
│  └─ attribution.md         (versions, brokerage model, etc.)         │
└──────────────────────────────────────────────────────────────────────┘
        │                     │                     │
        ▼                     ▼                     ▼
┌──────────────┐    ┌─────────────────────┐   ┌──────────────────────┐
│ FixtureData  │    │ FillMode.           │   │ IbkrEquity           │
│ Reader (NEW) │    │ NEXT_BAR_OPEN       │   │ CommissionModel(NEW) │
│              │    │ (already exists ✓)  │   │                      │
│ CSV → bars   │    │                     │   │ Tier rates / min /   │
│              │    │ Wiring + parity     │   │ max from QC IBKR     │
│              │    │ tests new           │   │ docs                 │
└──────────────┘    └─────────────────────┘   └──────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Existing engine + StrategySpec (unchanged)                          │
│  ├─ SpecAlgorithm runs single-symbol AAPL spec                       │
│  ├─ PredictionComparison(qc_pred > 0) entry                          │
│  ├─ PredictionComparison(qc_pred <= 0) exit                          │
│  └─ Produces our trade log via RunLedger + BacktestRunResult         │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  QcReconciler (NEW) — app/research/parity/qc_reconciler.py           │
│  ├─ _parse_qc_orders, _normalize_our_trades                          │
│  ├─ _audit_fixture (FIXTURE_INSUFFICIENT gate)                       │
│  ├─ _align_fills, _classify_divergences                              │
│  └─ → typed ReconciliationReport with render_markdown()              │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Pytest harness — tests/research/parity/                             │
│  ├─ test_qc_fixture_smoke.py (skipped until fixture lands)           │
│  ├─ test_qc_aapl_phase3_trade_parity.py (skipped until fixture)      │
│  ├─ conftest.py: --write-recon-report flag                           │
│  └─ Default report sink: artifacts/reconciliations/qc-aapl-phase3-   │
│       latest.md (gitignored). Accepted summary lives at              │
│       docs/references/reconciliations/qc-aapl-phase3.md              │
└──────────────────────────────────────────────────────────────────────┘
```

### Existing engine surfaces (no changes required)

- `FillMode.NEXT_BAR_OPEN` already exists at `app/engine/execution/order.py:36` and is implemented in `app/engine/execution/fill_model.py`. Phase 3 uses it as-is.
- `RunRequest.fill_mode: str` accepts `"next_bar_open"` (normalized to enum in `run_strategy_spec`). No signature change.
- `RunRequest.commission_per_order: float` exists. Phase 3 test runs with `commission_per_order=0` and applies IBKR commission **in the reconciler** when comparing against QC's recorded fees, not inside the engine. This avoids engine-wide wiring churn while still validating the commission model.

## Capture workflow (§2.1)

### 2.1.1 Algorithm setup (in QC Cloud)

Reuse the prediction fixture from PR #215. The algorithm code is the canonical QC tutorial algorithm with one Phase-3 modification — universe pinned to AAPL:

```python
def _select_assets(self, data):
    # Phase 3 modification: pin universe to AAPL.
    # Phase 4 restores the full prediction-threshold filter.
    return [self.symbol_aapl]
```

Backtest config:
- `set_start_date(2026, 2, 10)`, `set_end_date(2026, 3, 12)` — match PR #215 validation window
- `set_cash(100_000)`
- `universe_settings.resolution = Resolution.DAILY`
- No `set_brokerage_model()` call — record the resulting commission model in `attribution.md`

### 2.1.2 Capture-smoke step (hard gate before reconciler implementation)

Run the backtest once. Pull three artifacts via QC API:
1. `/backtests/orders/read` → `qc_orders.json`
2. `qb.history(self.symbol_aapl, start, end, Resolution.DAILY)` → `qc_price_history.csv`
3. `/backtests/read` → `qc_equity.json`

Then `tests/research/parity/test_qc_fixture_smoke.py` asserts the orders JSON has `{id, symbol, type, quantity, events[].{time, fillQuantity, fillPrice, direction}}`. Logs `FEE PRESENCE` boolean — decides Branch A vs B.

### 2.1.3 Fixture layout

```
PythonDataService/tests/fixtures/golden/qc-aapl-phase3/
├── qc_orders.json
├── qc_price_history.csv
├── qc_equity.json
├── qc_algorithm_screenshot.png
└── attribution.md
```

## Engine extensions (§2.2)

### 2.2.1 `FixtureDataReader`

`PythonDataService/app/research/parity/fixture_data_reader.py`. Mirrors `FakeDataReader`'s contract — `iter_bars(symbol, start, end) -> Iterator[TradeBar]`. Reads CSV with columns `time,open,high,low,close,volume`. Plugs into `run_strategy_spec`'s existing `data_source_factory` kwarg. **No runner signature change.**

### 2.2.2 `NextBarOpen` fill mode (already exists)

`FillMode.NEXT_BAR_OPEN` is the canonical value. Phase 3 only needs to:
- Verify formal rule set holds (rules 1–7, Chunk 2 of brainstorm) — covered by existing `tests/engine/test_fill_model.py`
- Pass `fill_mode="next_bar_open"` in the Phase 3 test's `RunRequest`

No new engine code. Phase 3 just consumes the existing mode.

### 2.2.3 IBKR commission model (staged)

`PythonDataService/app/research/parity/ibkr_commission.py` — a standalone callable that computes IBKR equity commission per QC's docs:
- Per-share: $0.005 (default tier)
- Min: $1.00 per order
- Max: 0.5% of trade value per order

Used in the **reconciler**, not the engine. Per-trade flow: the engine charges $0 commission (test sets `commission_per_order=0`); the reconciler computes IBKR commission externally for each of our fills and compares against QC's `orderFeeAmount`. This keeps the engine untouched and confines commission logic to one testable module.

**Branch A** (QC reports non-zero consistent fees): reconciler asserts `order_fee_amount` parity within `atol=$0.01`.
**Branch B** (QC reports zero / absent / inconsistent fees): reconciler emits `order_fee_amount` diff as informational only; `attribution.md` records QC's commission model assumption.

Branch decision is locked at fixture-commit time, not design time.

## Strategy spec (§2.3)

Single-symbol AAPL degenerate of QC's algorithm, expressible in existing `StrategySpec`:

```python
StrategySpec.model_validate({
    "schema_version": "1.0",
    "name": "QC AAPL Phase 3 trade-level parity",
    "symbols": ["AAPL"],
    "resolution": {"period_minutes": 1440},
    "indicators": [],
    "predictions": [
        {"id": "qc_pred", "prediction_set_id": "qc_aapl_gbm_v001", "field": "prediction"},
    ],
    "entry": {
        "logic": "AND",
        "conditions": [
            {"kind": "PredictionComparison", "prediction": "qc_pred", "op": ">", "value": 0.0},
        ],
        "size": {"kind": "SetHoldings", "fraction": 1.0},
        "pyramiding": 1,
    },
    "position": {"kind": "EQUITY_LONG"},
    "survival": [],
    "exit": {
        "logic": "OR",
        "conditions": [
            {"kind": "PredictionComparison", "prediction": "qc_pred", "op": "<=", "value": 0.0},
        ],
    },
})
```

Semantics:
- **Entry**: bar T close, `qc_pred[T] > 0` → target 100% AAPL → fills at T+1 open (via `NEXT_BAR_OPEN`)
- **Exit**: bar T close, `qc_pred[T] <= 0` and currently holding → target 0% AAPL → sells at T+1 open
- **Hold**: condition unchanged → no order

If captured QC algorithm uses a different prediction threshold than `0.0`, the spec's `value` field tracks it. Settled at capture time.

## Reconciliation module (§3)

### 3.1 Public surface

```python
def reconcile_qc_aapl_phase3(
    *,
    qc_orders_path: Path,
    qc_price_history_path: Path,
    our_trade_log: list[LoggedTrade],
    tolerances: Tolerances = Tolerances.phase3_default(),
    assert_fees: bool = False,
) -> ReconciliationReport: ...
```

### 3.2 Five internal steps (single module, private functions)

1. `_parse_qc_orders(path) -> list[QcFill]` — flatten orders JSON events; one `QcFill` per fill event, `Decimal`-typed prices.
2. `_normalize_our_trades(trades) -> list[OurFill]` — adapt `LoggedTrade` records to the same `Fill` shape. (`LoggedTrade` currently lacks `qty`/`side`/`commission`; Phase 3 either adds those as `Optional` fields with defaults, or derives them from the round-trip pair `(entry_price, exit_price)` + signed `pnl_pts`.)
3. `_audit_fixture(qc_fills, price_history) -> list[FixtureAudit]` — for every QC fill, verify the fill price is explainable by the same/next daily-bar `open` within `atol=$0.01`. Failures classified `FIXTURE_INSUFFICIENT`; shortcut the rest of reconciliation.
4. `_align_fills(qc_fills, our_fills) -> list[Pair]` — pair by `(symbol, trading_date, direction)`. Daily AAPL → at most one fill per (date, direction). Unmatched on either side → `Pair` with one side `None`.
5. `_classify_divergences(pairs, tolerances) -> list[Divergence]` — walk tolerance table, emit zero or more typed divergences per pair.

### 3.3 Divergence taxonomy

```python
class DivergenceCategory(Enum):
    FIXTURE_INSUFFICIENT = "fixture_insufficient"  # QC fill not explainable from bars
    DECISION_MISMATCH    = "decision_mismatch"     # one side has a trade the other doesn't
    DIRECTION_MISMATCH   = "direction_mismatch"
    QUANTITY_MISMATCH    = "quantity_mismatch"
    FILL_PRICE_DRIFT     = "fill_price_drift"      # price > atol=$0.01
    COMMISSION_DRIFT     = "commission_drift"      # fee > atol (only if asserted)
    PNL_DRIFT            = "pnl_drift"             # propagated tolerance exceeded
    ORDER_TYPE_MISMATCH  = "order_type_mismatch"
```

Acceptance gate: `report.status == "passed"` iff zero divergences in gating categories. `COMMISSION_DRIFT` is gating only if `assert_fees=True`.

### 3.4 ReconciliationReport

```python
@dataclass(frozen=True)
class ReconciliationReport:
    status: Literal["passed", "failed"]
    summary: ReconciliationSummary
    tolerances: Tolerances
    fixture_audit: list[FixtureAudit]
    pairs: list[ReconciledPair]
    divergences: list[Divergence]
    diagnostics: Diagnostics
    fixture_metadata: FixtureMetadata

    def render_markdown(self) -> str: ...
    def render_json(self) -> dict: ...
```

## Test infrastructure (§4)

Three test files under `PythonDataService/tests/research/parity/`:

### 4.1 `test_qc_fixture_smoke.py` (capture-smoke gate)

Module-level `pytest.mark.skipif(not _FIXTURE_DIR.is_file())`. Asserts orders/equity/price-history fixture shape + logs the fee-presence boolean that decides Branch A vs B.

### 4.2 `test_qc_aapl_phase3_trade_parity.py` (acceptance test)

Single test asserting trade-level parity:
- Module-level `pytest.mark.skipif` on fixture absence
- Imports prediction set via PR #215's `import_qc_fixture`
- Builds the AAPL spec, runs `run_strategy_spec` with `fill_mode="next_bar_open"`, `commission_per_order=0`
- Calls `reconcile_qc_aapl_phase3(...)`
- Writes report on failure OR when `--write-recon-report` flag is set
- Asserts `report.status == "passed"`

### 4.3 `conftest.py`

Registers `--write-recon-report` option.

### 4.4 Report sinks

- **Runtime artifact** (gitignored): `PythonDataService/artifacts/reconciliations/qc-aapl-phase3-latest.md`. Overwritten each run; no timestamped filenames.
- **Committed summary** (hand-authored, post-acceptance): `docs/references/reconciliations/qc-aapl-phase3.md`.

## Acceptance gate (§5)

Phase 3 is accepted when all of the following are true:

1. `test_qc_fixture_smoke.py` committed and passing.
2. `tests/fixtures/golden/qc-aapl-phase3/` committed with all five files.
3. `FixtureDataReader`, `IbkrEquityCommissionModel`, `QcReconciler` committed with unit tests passing in isolation.
4. Branch-A commission committed iff `orderFeeAmount` field is non-zero and consistent. Otherwise no commission code lands and `attribution.md` records the assumption.
5. `test_qc_aapl_phase3_trade_parity.py` passes on master with `report.status == "passed"`.
6. Hand-authored summary committed at `docs/references/reconciliations/qc-aapl-phase3.md`.
7. `.claude/rules/numerical-rigor.md` extended with trade-level reconciliation taxonomy.

## Escalation paths (§6)

| Divergence category observed | Routes to |
|---|---|
| `FIXTURE_INSUFFICIENT` | Phase 3.5 minute-bar promotion, OR fixture re-capture if data error |
| `FILL_PRICE_DRIFT` clustered | Phase 3.5 LEAN port for that specific edge case |
| `COMMISSION_DRIFT` (Branch B → A promotion) | Phase 3.5 IBKR commission model wired into engine |
| `DECISION_MISMATCH`, `DIRECTION_MISMATCH`, `QUANTITY_MISMATCH` | Engine / spec bug — fix in Phase 3 directly |

Phase 4 (multi-symbol top-N ranking) is unrelated to Phase 3 escalations — it's the natural next milestone after Phase 3 is green.

## Open risks & assumptions (§7)

1. QC's algorithm code from web-fetch may not reflect canonical template; capture-smoke screenshots the actual file.
2. `PredictionUniverse._select_assets` override may interact with universe lifecycle (forced liquidation on threshold miss). Document exact override; switch to "always include AAPL, let threshold drive set_holdings weight" if phantom liquidations appear.
3. `seed_initial_prices=True` warmup effect — audit step compares first-fill dates.
4. `qb.history(...)` price adjustment mode may differ from QC backtester's feed. `_audit_fixture` catches this regardless.
5. Our `SetHoldings` primitive may calculate quantity differently from QC's `set_holdings()` (rounding, cash buffer). First reconciliation will surface as `QUANTITY_MISMATCH`; fix in Phase 3, don't escalate.
6. Engine default `commission_per_order` may be non-zero; Phase 3 test sets it to `0` explicitly.
7. QC orders may have multiple events per order (partial fills); if `len(order.events) > 1` for any order, classify as `FIXTURE_INSUFFICIENT` and escalate to Phase 3.5.
8. `NEXT_BAR_OPEN` rule 4 (fail on opposite signal queued): capture-smoke verifies no consecutive opposite signals within a single trading day.

## What this design explicitly does NOT specify

- Concrete IBKR per-share rate / tier cutoffs (settled from QC docs at implementation time, only if Branch A).
- The `FixtureMetadata` dataclass field set (defined alongside the reconciler).
- Pytest parametrization details for the smoke test (implementation detail).
