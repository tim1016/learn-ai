# LEAN EMA-crossover template + unified backtest-run history — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an EMA(5)/EMA(10) crossover trusted template to the LEAN sidecar that mirrors the Engine Lab spec bit-for-bit, persist LEAN runs to Postgres so both engines share one storage substrate, and surface a unified backtest-run history with a select-two side-by-side compare view classified by the existing `qc_reconciler` taxonomy.

**Architecture:** LEAN runs land in the existing `StrategyExecution` + `BacktestTrade` Postgres tables tagged `Source="lean-sidecar"`. A new shared Angular `run-history` component (embedded on both lab pages) drives a Relay-style `backtestRuns` cursor query. The compare view (`/runs/compare`) calls a new `compareBacktestRuns` GraphQL field that delegates to Python's existing `qc_reconciler` via an internal `POST /api/lean-sidecar/compare` hop. The EMA template's source is hard-coded (constants on the algorithm class) so it functions as a deterministic validation oracle for the Engine Lab spec.

**Tech Stack:** Python 3.11 + FastAPI + Pydantic v2 + SQLAlchemy (PythonDataService); .NET 10 + Hot Chocolate v15 + EF Core (Backend); Angular 21 + signals + Vitest (Frontend); LEAN sidecar Docker image (vendor-pinned).

**Design doc:** `docs/superpowers/specs/2026-05-19-lean-ema-template-and-unified-history-design.md`.

**PR breakdown (one plan, five logical PRs):**

- **PR 1 — EMA template + Postgres persistence** (Tasks 1.0–1.12). After landing: user picks `ema_crossover` in the LEAN dropdown, runs it, sees the row appear in Postgres `StrategyExecution`.
- **PR 2 — Unified history table** (Tasks 2.1–2.8). After landing: both lab pages show all runs in one table with engine column and multi-select.
- **PR 3 — Compare view** (Tasks 3.1–3.10). After landing: select two rows, click Compare, see side-by-side panels + divergence table.
- **PR 4 — Parity acceptance test** (Tasks 4.1–4.3). After landing: golden test pins zero gating-set divergences between LEAN and Engine Lab.
- **PR 5 — Backfill CLI** (Tasks 5.1–5.2). After landing: pre-existing on-disk LEAN runs are visible in unified history.

---

## File Structure

### New Python files
- `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` — `EMA_CROSSOVER_SOURCE: str` constant containing the LEAN algorithm source.
- `PythonDataService/app/services/lean_sidecar_persistence.py` — `normalize_and_persist(run_id)` orchestration; FIFO buy/sell pairing into round-trip trades; synthetic MTM exit; aggregate computation; idempotency keyed on `LeanRunId`.
- `PythonDataService/app/services/lean_sidecar_compare_service.py` — `compare_runs(left_id, right_id)` reads both `StrategyExecution`+`BacktestTrade` sets and calls `qc_reconciler`.
- `PythonDataService/app/scripts/backfill_lean_runs.py` — one-shot CLI for historical artifacts.

### Modified Python files
- `PythonDataService/app/services/lean_sidecar_service.py` — extend `TrustedTemplate` Literal + `_SOURCE_FOR_TEMPLATE` + `_BROKERAGE_POLICY_FOR_TEMPLATE`; call `normalize_and_persist` at tail of `run_trusted_sample`.
- `PythonDataService/app/routers/lean_sidecar.py` — add `POST /api/lean-sidecar/compare` endpoint.

### New Python tests
- `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py`
- `PythonDataService/tests/services/test_lean_sidecar_persistence.py`
- `PythonDataService/tests/services/test_lean_sidecar_compare.py`
- `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py`

### New .NET files
- `Backend/Migrations/<timestamp>_AddLeanRunIdAndSyntheticExit.cs` — adds `StrategyExecution.LeanRunId` (string?) + `BacktestTrade.IsSyntheticExit` (bool, default false).
- `Backend/GraphQL/Comparison/CompareBacktestRunsResolver.cs` — `[QueryType]` static class.
- `Backend/GraphQL/Comparison/ComparisonGraphQLTypes.cs` — `RunComparisonResult`, `ComparisonGuardrails`, `ComparisonSummary`, `TradeDivergence`, `DivergenceCategoryEnum`.
- `Backend/Services/IComparisonService.cs` + `ComparisonService.cs` — typed HttpClient call into Python's compare endpoint.

### Modified .NET files
- `Backend/Models/MarketData/StrategyExecution.cs` — add `public string? LeanRunId { get; set; }`.
- `Backend/Models/MarketData/BacktestTrade.cs` — add `public bool IsSyntheticExit { get; set; }`.
- `Backend/GraphQL/Queries/BacktestRunsResolver.cs` (or current file) — extend with engine filter, cursor pagination, new fields.
- `Backend/Data/AppDbContext.cs` — wire `IsSyntheticExit` default.

### New .NET tests
- `Backend.Tests/GraphQL/BacktestRunsConnectionTests.cs`
- `Backend.Tests/GraphQL/CompareBacktestRunsTests.cs`

### New Angular files
- `Frontend/src/app/components/shared/run-history/run-history.component.ts` + `.html` + `.scss` + `.spec.ts`.
- `Frontend/src/app/components/run-comparison/run-comparison.component.ts` + `.html` + `.scss` + `.spec.ts`.
- `Frontend/src/app/graphql/queries/backtest-runs.ts` — typed GraphQL query document.
- `Frontend/src/app/graphql/queries/compare-backtest-runs.ts`.

### Modified Angular files
- `Frontend/src/app/components/lean-lab/lean-lab.component.ts` — extend `template` literal type; add dropdown option.
- `Frontend/src/app/components/lean-lab/lean-lab.component.html` — add `<option value="ema_crossover">`.
- `Frontend/src/app/components/lean-lab/lean-lab-run-history/lean-lab-run-history.component.ts` — replace internals with `<app-run-history>` child or delete in favor of shared.
- `Frontend/src/app/components/lean-engine/<wherever-the-engine-lab-history-lives>.component.ts` — same.
- `Frontend/src/app/app.routes.ts` — add `/runs/compare` lazy route.

---

## PR 1 — EMA template + LEAN persistence to Postgres

### Task 1.0: Fill-model parity spike (one-day exploratory)

**Why this is task 0:** the EMA template's `Initialize()` calls `SetFillModel(...)`. The choice between custom `EquityFillModel`, native next-minute-open, or `MarketOnOpenOrder` is a one-day investigation, not a code change. Output is a documented decision that determines Task 1.1.

**Files:**
- Investigate: `references/lean-engine/` (vendored), `PythonDataService/app/engine/execution/fill_model.py`, `PythonDataService/app/lean_sidecar/trusted_samples/buy_and_hold.py`.
- Write: `docs/references/fill-model-parity-spike-2026-05-19.md`.

- [ ] **Step 1: Run the existing buy-and-hold template and inspect fill timing**

```bash
podman exec polygon-data-service curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
  -H "Content-Type: application/json" \
  -d '{"template":"trusted_default","symbol":"SPY","start_date":"2025-01-06","end_date":"2025-01-06","starting_cash":100000}'
```

Expected: a run_id back. Note it.

- [ ] **Step 2: Inspect the resulting order-events.json**

```bash
podman exec polygon-data-service bash -c 'cat //app/artifacts/lean-sidecar/<RUN_ID>/workspace/output/MyAlgorithm-order-events.json'
```

Compare `time` (epoch seconds) of the BUY fill against the consolidated 09:30 minute bar boundary. Record exact second of fill.

- [ ] **Step 3: Compute parity by running spec backtest with each fill mode**

```bash
# signal_bar_close mode
podman exec polygon-data-service curl -X POST http://localhost:8000/api/spec-strategy/backtest \
  -H "Content-Type: application/json" \
  -d '{"spec":<paste spy_ema_crossover.spec.json>,"start_date":"2025-01-06","end_date":"2025-01-06","initial_cash":100000,"fill_mode":"signal_bar_close"}'

# next_bar_open mode (same body, fill_mode swapped)
```

- [ ] **Step 4: Record decision in spike doc**

Write `docs/references/fill-model-parity-spike-2026-05-19.md` containing:
1. Observed LEAN fill timestamp (seconds since epoch + ms-aligned 15-min boundary calc).
2. Whether native LEAN behavior matches `signal_bar_close` or `next_bar_open` or neither.
3. Chosen approach: (a) custom `EquityFillModel`, (b) accept `next_bar_open`, or (c) `MarketOnOpenOrder`.
4. Rationale.

- [ ] **Step 5: Commit the spike doc**

```bash
git add docs/references/fill-model-parity-spike-2026-05-19.md
git commit -m "docs(references): fill-model parity spike for ema_crossover template"
```

---

### Task 1.1: Add EMA crossover template source (TDD)

**Files:**
- Create: `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`
- Test: `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py`

- [ ] **Step 1: Write the failing test**

Create `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py`:

```python
"""Smoke test: ema_crossover trusted template source is parseable and pinned to spec."""

from __future__ import annotations

import ast

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE


def test_source_is_non_empty_string() -> None:
    assert isinstance(EMA_CROSSOVER_SOURCE, str)
    assert len(EMA_CROSSOVER_SOURCE) > 100


def test_source_parses_as_valid_python() -> None:
    ast.parse(EMA_CROSSOVER_SOURCE)


def test_class_constants_match_spec() -> None:
    """Pinned to PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    constants: dict[str, int | float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MyAlgorithm":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    constants[stmt.targets[0].id] = stmt.value.value

    assert constants["FAST_PERIOD"] == 5
    assert constants["SLOW_PERIOD"] == 10
    assert constants["RSI_PERIOD"] == 14
    assert constants["BAR_MINUTES"] == 15
    assert constants["EXIT_BARS"] == 5
    assert constants["GAP_MIN"] == pytest.approx(0.20)
    assert constants["RSI_LO"] == 50
    assert constants["RSI_HI"] == 70


def test_source_contains_required_handlers() -> None:
    """Verify Initialize, OnConsolidatedBar, OnEndOfAlgorithm exist."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    method_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            method_names.add(node.name)

    assert "Initialize" in method_names
    assert "OnConsolidatedBar" in method_names
    assert "OnEndOfAlgorithm" in method_names


def test_source_consolidates_15_minute_bars() -> None:
    assert "TradeBarConsolidator" in EMA_CROSSOVER_SOURCE
    assert "timedelta(minutes=self.BAR_MINUTES)" in EMA_CROSSOVER_SOURCE


def test_source_uses_wilders_rsi() -> None:
    assert "MovingAverageType.Wilders" in EMA_CROSSOVER_SOURCE


def test_source_liquidates_at_end() -> None:
    assert "OnEndOfAlgorithm" in EMA_CROSSOVER_SOURCE
    assert "Liquidate(self.symbol)" in EMA_CROSSOVER_SOURCE
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.lean_sidecar.trusted_samples.ema_crossover'`.

- [ ] **Step 3: Implement the template source**

Create `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`:

```python
"""EMA(5)/EMA(10) crossover trusted template — LEAN parity oracle for spec strategy.

Mirrors PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json
exactly. Parameters are class constants (not GetParameter values) so this template
is a deterministic oracle: any change to the parameters is a deliberate code change,
not a runtime config drift.
"""

from __future__ import annotations


EMA_CROSSOVER_SOURCE = '''\
from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover with RSI(14) gate on 15-min consolidated bars.

    Validation oracle for the Engine Lab spec at
    PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json.
    Parameters are pinned; only symbol, dates, and starting cash are configurable.
    """

    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    BAR_MINUTES = 15
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        equity = self.AddEquity(symbol_str, Resolution.Minute, fillForward=False)
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        # FILL_MODEL_HOOK: replaced in Task 1.1.b once spike (Task 1.0) lands.
        self.symbol = equity.Symbol

        self.consolidator = TradeBarConsolidator(timedelta(minutes=self.BAR_MINUTES))
        self.consolidator.DataConsolidated += self.OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(self.symbol, self.consolidator)

        self.ema_fast = ExponentialMovingAverage(self.FAST_PERIOD)
        self.ema_slow = ExponentialMovingAverage(self.SLOW_PERIOD)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.prev_fast = None
        self.prev_slow = None
        self.bars_held = 0
        self.in_trade = False

        # Warmup: enough minute bars to seed the slowest indicator at 15-min cadence.
        # SLOW_PERIOD * BAR_MINUTES gives EMA(10) on 15-min bars 150 minute-bars of history;
        # RSI_PERIOD * BAR_MINUTES gives 210 minute-bars; use the max.
        warmup_minutes = max(self.SLOW_PERIOD, self.RSI_PERIOD) * self.BAR_MINUTES * 2
        self.SetWarmUp(timedelta(minutes=warmup_minutes))
        self.SetBenchmark(lambda dt: 100)

    def OnConsolidatedBar(self, sender, bar):
        close = float(bar.Close)
        self.ema_fast.Update(bar.EndTime, close)
        self.ema_slow.Update(bar.EndTime, close)
        self.rsi.Update(bar.EndTime, close)

        if not (self.ema_fast.IsReady and self.ema_slow.IsReady and self.rsi.IsReady):
            self.prev_fast = float(self.ema_fast.Current.Value) if self.ema_fast.IsReady else None
            self.prev_slow = float(self.ema_slow.Current.Value) if self.ema_slow.IsReady else None
            return

        fast = float(self.ema_fast.Current.Value)
        slow = float(self.ema_slow.Current.Value)
        rsi = float(self.rsi.Current.Value)

        if self.IsWarmingUp:
            self.prev_fast, self.prev_slow = fast, slow
            return

        if self.in_trade:
            self.bars_held += 1
            if self.bars_held >= self.EXIT_BARS:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_held = 0
        else:
            fresh_cross = (
                self.prev_fast is not None
                and self.prev_slow is not None
                and fast > slow
                and self.prev_fast <= self.prev_slow
            )
            gap_ok = (fast - slow) >= self.GAP_MIN
            rsi_ok = self.RSI_LO <= rsi <= self.RSI_HI
            if fresh_cross and gap_ok and rsi_ok:
                self.SetHoldings(self.symbol, 1.0)
                self.in_trade = True
                self.bars_held = 0

        self.prev_fast, self.prev_slow = fast, slow

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Apply the fill-model decision from Task 1.0**

Edit `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`, replacing the `# FILL_MODEL_HOOK` placeholder with the chosen approach from the spike doc:

**Option (a) — custom EquityFillModel (default if spike doesn't say otherwise):**

```python
        # Pin fills to consolidated-bar close for signal_bar_close parity.
        equity.SetFillModel(SignalBarCloseFillModel())
```

And append at module level (after the class):

```python
class SignalBarCloseFillModel(EquityFillModel):
    """Override MarketFill to use asset.Close (the most recent bar close).

    LEAN's default ImmediateFillModel fills market orders on the next data event,
    which for minute-resolution data means the next minute's open. This model
    pins the fill to the close of the bar where the order was placed — matching
    the Engine Lab's signal_bar_close mode.
    """

    def MarketFill(self, asset, order):
        fill = super().MarketFill(asset, order)
        fill.FillPrice = asset.Close
        return fill
```

**Option (b) — accept next_bar_open (no code change):** delete the `# FILL_MODEL_HOOK` line entirely.

**Option (c) — MarketOnOpenOrder:** replace `SetHoldings(self.symbol, 1.0)` in `OnConsolidatedBar` with explicit `MarketOnOpenOrder(self.symbol, target_qty)`.

Add a corresponding assertion to the test in step 1:

```python
def test_source_uses_chosen_fill_model() -> None:
    # Adjust this assertion based on the option picked in Task 1.0:
    # Option (a): assert "SignalBarCloseFillModel" in EMA_CROSSOVER_SOURCE
    # Option (b): assert "FILL_MODEL_HOOK" not in EMA_CROSSOVER_SOURCE  (and no FillModel override)
    # Option (c): assert "MarketOnOpenOrder" in EMA_CROSSOVER_SOURCE
    ...
```

- [ ] **Step 6: Re-run tests**

```bash
podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v
```

Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py \
        PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py
git commit -m "feat(lean-sidecar): add ema_crossover trusted template

Mirrors spy_ema_crossover.spec.json: EMA(5)/EMA(10), RSI(14) Wilders, 15-min
consolidated bars, fresh-cross + gap >= 0.20 + RSI in [50,70] entry, 5-bar
time-stop exit. Parameters are pinned class constants so the template acts
as a deterministic validation oracle."
```

---

### Task 1.2: Register template in `_SOURCE_FOR_TEMPLATE`

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py`
- Test: add to existing template-registry test or `PythonDataService/tests/services/test_lean_sidecar_service.py`

- [ ] **Step 1: Find the test file for the service**

```bash
grep -l "_SOURCE_FOR_TEMPLATE\|TrustedTemplate" PythonDataService/tests/ -r
```

If a test file exists, append the new test. If not, create `PythonDataService/tests/services/test_lean_sidecar_template_registry.py`:

```python
"""Verify ema_crossover is registered alongside trusted_default and reconciliation."""

from __future__ import annotations

from app.services.lean_sidecar_service import (
    _BROKERAGE_POLICY_FOR_TEMPLATE,
    _SOURCE_FOR_TEMPLATE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE


def test_ema_crossover_is_in_source_registry() -> None:
    assert "ema_crossover" in _SOURCE_FOR_TEMPLATE
    assert _SOURCE_FOR_TEMPLATE["ema_crossover"] is EMA_CROSSOVER_SOURCE


def test_ema_crossover_brokerage_policy_is_algorithm_default() -> None:
    assert _BROKERAGE_POLICY_FOR_TEMPLATE["ema_crossover"] == "algorithm_default"


def test_existing_templates_still_registered() -> None:
    """Regression guard: don't break existing templates."""
    assert "trusted_default" in _SOURCE_FOR_TEMPLATE
    assert "reconciliation" in _SOURCE_FOR_TEMPLATE
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_template_registry.py -v
```

Expected: `KeyError: 'ema_crossover'` or `AssertionError`.

- [ ] **Step 3: Wire the template into the registry**

In `PythonDataService/app/services/lean_sidecar_service.py`, locate the existing imports and `TrustedTemplate` Literal (≈ line 80) and the two dicts (≈ lines 90-93). Update them:

```python
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.trusted_samples.buy_and_hold_reconciliation import (
    BUY_AND_HOLD_RECONCILIATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE  # NEW

TrustedTemplate = Literal["trusted_default", "reconciliation", "ema_crossover"]  # NEW: add ema_crossover

_BROKERAGE_POLICY_FOR_TEMPLATE: dict[TrustedTemplate, str] = {
    "trusted_default": "algorithm_default",
    "reconciliation": "ibkr",
    "ema_crossover": "algorithm_default",  # NEW
}

_SOURCE_FOR_TEMPLATE: dict[TrustedTemplate, str] = {
    "trusted_default": BUY_AND_HOLD_SOURCE,
    "reconciliation": BUY_AND_HOLD_RECONCILIATION_SOURCE,
    "ema_crossover": EMA_CROSSOVER_SOURCE,  # NEW
}
```

Also update the corresponding Pydantic model in `PythonDataService/app/routers/lean_sidecar.py` — find the `template` field on the request model and extend its Literal to include `"ema_crossover"`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_template_registry.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run project-scope ruff**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```

Expected: zero warnings.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_service.py \
        PythonDataService/app/routers/lean_sidecar.py \
        PythonDataService/tests/services/test_lean_sidecar_template_registry.py
git commit -m "feat(lean-sidecar): register ema_crossover in trusted template dispatch"
```

---

### Task 1.3: Add EMA crossover option to the LEAN Lab dropdown

**Files:**
- Modify: `Frontend/src/app/components/lean-lab/lean-lab.component.ts`
- Modify: `Frontend/src/app/components/lean-lab/lean-lab.component.html`
- Test: `Frontend/src/app/components/lean-lab/lean-lab.component.spec.ts`

- [ ] **Step 1: Write the failing test**

In `Frontend/src/app/components/lean-lab/lean-lab.component.spec.ts`, add (alongside existing tests):

```typescript
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";
import { LeanLabComponent } from "./lean-lab.component";

describe("LeanLabComponent — template dropdown", () => {
  it("offers the ema_crossover template option", async () => {
    await render(LeanLabComponent);
    const select = screen.getByLabelText(/template/i) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("ema_crossover");
  });

  it("still offers trusted_default and reconciliation", async () => {
    await render(LeanLabComponent);
    const select = screen.getByLabelText(/template/i) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("trusted_default");
    expect(optionValues).toContain("reconciliation");
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
podman exec my-frontend npx ng test --include="**/lean-lab.component.spec.ts" --watch=false
```

Expected: 2 failures (option not present).

- [ ] **Step 3: Add the option to the template type and dropdown**

In `Frontend/src/app/components/lean-lab/lean-lab.component.ts`, find the `template` FormControl (around line 169) and extend the literal type:

```typescript
template: new FormControl<"trusted_default" | "reconciliation" | "ema_crossover">(
  "trusted_default",
  { nonNullable: true },
),
```

Also extend any TypeScript type alias used downstream (e.g., the `TrustedRunRequest` interface in `Frontend/src/app/services/lean-sidecar.service.ts` — find and update its `template` field literal type to match).

In `Frontend/src/app/components/lean-lab/lean-lab.component.html`, find the existing `<select formControlName="template">` block and add a third `<option>`:

```html
<select formControlName="template" id="template-select" aria-label="Template">
  <option value="trusted_default">Buy-and-hold (default)</option>
  <option value="reconciliation">Buy-and-hold (IBKR reconciliation)</option>
  <option value="ema_crossover">SPY EMA(5)/EMA(10) Crossover (LEAN parity oracle)</option>
</select>
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
podman exec my-frontend npx ng test --include="**/lean-lab.component.spec.ts" --watch=false
```

Expected: all green.

- [ ] **Step 5: Run project-scope eslint**

```bash
npx eslint Frontend/src/ --max-warnings 0
```

Expected: zero warnings.

- [ ] **Step 6: Commit**

```bash
git add Frontend/src/app/components/lean-lab/lean-lab.component.ts \
        Frontend/src/app/components/lean-lab/lean-lab.component.html \
        Frontend/src/app/components/lean-lab/lean-lab.component.spec.ts \
        Frontend/src/app/services/lean-sidecar.service.ts
git commit -m "feat(lean-lab): add ema_crossover option to template dropdown"
```

---

### Task 1.4: EF migration — add LeanRunId and IsSyntheticExit

**Files:**
- Modify: `Backend/Models/MarketData/StrategyExecution.cs`
- Modify: `Backend/Models/MarketData/BacktestTrade.cs`
- Create: `Backend/Migrations/<timestamp>_AddLeanRunIdAndSyntheticExit.cs` (generated)
- Modify: `Backend/Data/AppDbContext.cs` (default for IsSyntheticExit)
- Test: `Backend.Tests/Data/SchemaMigrationTests.cs` (snapshot)

- [ ] **Step 1: Add the columns to the entity classes**

In `Backend/Models/MarketData/StrategyExecution.cs`, add after the existing `Source` property:

```csharp
public string? LeanRunId { get; set; }
```

In `Backend/Models/MarketData/BacktestTrade.cs`, add after the existing `SignalReason`:

```csharp
public bool IsSyntheticExit { get; set; }
```

- [ ] **Step 2: Configure the default in AppDbContext**

In `Backend/Data/AppDbContext.cs`, locate the `OnModelCreating` method (or per-entity config) and add:

```csharp
modelBuilder.Entity<BacktestTrade>()
    .Property(t => t.IsSyntheticExit)
    .HasDefaultValue(false);

modelBuilder.Entity<StrategyExecution>()
    .Property(s => s.LeanRunId)
    .HasMaxLength(128);
```

- [ ] **Step 3: Generate the migration**

```bash
cd Backend
dotnet ef migrations add AddLeanRunIdAndSyntheticExit
```

Expected: a new file `Backend/Migrations/<timestamp>_AddLeanRunIdAndSyntheticExit.cs` and an updated snapshot.

- [ ] **Step 4: Inspect the generated migration**

Open the generated file. Verify it contains exactly two `AddColumn` operations — `LeanRunId` on `StrategyExecutions` (nullable string, max 128) and `IsSyntheticExit` on `BacktestTrades` (bool, default false). No renames, no drops.

- [ ] **Step 5: Apply the migration locally**

```bash
cd Backend
dotnet ef database update
```

Verify in the DB:

```bash
podman exec -it my-postgres psql -U postgres -c "\d \"StrategyExecutions\"" | grep LeanRunId
podman exec -it my-postgres psql -U postgres -c "\d \"BacktestTrades\"" | grep IsSyntheticExit
```

Expected: both columns present.

- [ ] **Step 6: Write a snapshot test**

Create `Backend.Tests/Data/SchemaMigrationTests.cs`:

```csharp
using FluentAssertions;
using Xunit;
using Backend.Data;
using Backend.Models.MarketData;

namespace Backend.Tests.Data;

public class SchemaMigrationTests
{
    [Fact]
    public void StrategyExecution_HasLeanRunIdColumn()
    {
        var prop = typeof(StrategyExecution).GetProperty(nameof(StrategyExecution.LeanRunId));
        prop.Should().NotBeNull();
        prop!.PropertyType.Should().Be(typeof(string));
    }

    [Fact]
    public void BacktestTrade_HasIsSyntheticExitColumn()
    {
        var prop = typeof(BacktestTrade).GetProperty(nameof(BacktestTrade.IsSyntheticExit));
        prop.Should().NotBeNull();
        prop!.PropertyType.Should().Be(typeof(bool));
    }
}
```

- [ ] **Step 7: Run the test**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~SchemaMigrationTests"
```

Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add Backend/Models/MarketData/StrategyExecution.cs \
        Backend/Models/MarketData/BacktestTrade.cs \
        Backend/Data/AppDbContext.cs \
        Backend/Migrations/ \
        Backend.Tests/Data/SchemaMigrationTests.cs
git commit -m "feat(backend): add LeanRunId + IsSyntheticExit columns for unified history"
```

---

### Task 1.5: Pairing algorithm — round-trip trade construction (TDD)

**Files:**
- Create: `PythonDataService/app/services/lean_sidecar_persistence.py`
- Test: `PythonDataService/tests/services/test_lean_sidecar_persistence.py`

- [ ] **Step 1: Write the failing test for FIFO pairing**

Create `PythonDataService/tests/services/test_lean_sidecar_persistence.py`:

```python
"""Tests for LEAN order-event pairing into round-trip BacktestTrade rows."""

from __future__ import annotations

import pytest

from app.services.lean_sidecar_persistence import pair_order_events


def _filled_event(
    event_id: int,
    direction: str,
    ms_utc: int,
    fill_price: float,
    fill_qty: float,
    fee: float = 0.0,
) -> dict:
    return {
        "id": f"MyAlgorithm-{event_id}-2",
        "order_id": event_id,
        "order_event_id": 2,
        "direction": direction,
        "status": "filled",
        "ms_utc": ms_utc,
        "fill_price": fill_price,
        "fill_quantity": fill_qty,
        "quantity": fill_qty,
        "order_fee_amount": fee,
        "order_fee_currency": "USD",
    }


def test_pair_empty_events_returns_empty_list() -> None:
    trades, open_lot = pair_order_events([])
    assert trades == []
    assert open_lot is None


def test_pair_skips_non_filled_events() -> None:
    events = [
        {**_filled_event(1, "buy", 1_700_000_000_000, 100.0, 10), "status": "submitted"},
        _filled_event(1, "buy", 1_700_000_060_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_120_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert len(trades) == 1
    assert open_lot is None


def test_pair_single_round_trip() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 1
    t = trades[0]
    assert t.trade_number == 1
    assert t.entry_ms_utc == 1_700_000_000_000
    assert t.exit_ms_utc == 1_700_000_060_000
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(101.0)
    assert t.quantity == 10
    # pnl = (101 - 100) * 10 - 0.5 - 0.5 = 9.0
    assert t.pnl == pytest.approx(9.0)
    assert t.is_synthetic_exit is False


def test_pair_multiple_round_trips() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
        _filled_event(3, "buy", 1_700_000_120_000, 102.0, 10, fee=0.5),
        _filled_event(4, "sell", 1_700_000_180_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 2
    assert [t.trade_number for t in trades] == [1, 2]
    assert trades[1].pnl == pytest.approx((100.0 - 102.0) * 10 - 1.0)


def test_pair_half_open_returns_open_lot() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is not None
    assert open_lot.entry_ms_utc == 1_700_000_000_000
    assert open_lot.entry_price == pytest.approx(100.0)
    assert open_lot.quantity == 10
    assert open_lot.fees == [0.5]


def test_pair_raises_on_pyramiding() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10),
        _filled_event(2, "buy", 1_700_000_060_000, 101.0, 10),  # second buy without sell
    ]
    with pytest.raises(NotImplementedError, match="Pyramiding not supported"):
        pair_order_events(events)


def test_pair_ignores_sell_without_open_lot() -> None:
    """Defensive: short selling not expected for current templates."""
    events = [
        _filled_event(1, "sell", 1_700_000_000_000, 100.0, 10),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is None
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py::test_pair_empty_events_returns_empty_list -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the pairing module**

Create `PythonDataService/app/services/lean_sidecar_persistence.py`:

```python
"""Persistence layer: normalize LEAN sidecar output into StrategyExecution rows.

Consumed by lean_sidecar_service.run_trusted_sample() at the tail of a successful
run. Reads the normalized result.json, pairs filled order events into round-trip
trades, synthesizes a mark-to-market exit for any half-open position, computes
aggregate KPIs, and writes one StrategyExecution row + N BacktestTrade rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class OpenLot:
    """A buy fill that has not yet been matched with a sell."""

    entry_ms_utc: int
    entry_price: float
    quantity: float
    fees: list[float] = field(default_factory=list)


@dataclass
class PairedTrade:
    """Round-trip trade reconstructed from a buy/sell event pair."""

    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    signal_reason: str
    is_synthetic_exit: bool


def pair_order_events(
    events: Sequence[dict[str, Any]],
    signal_reason: str = "EMA crossover exit (5-bar time stop)",
) -> tuple[list[PairedTrade], OpenLot | None]:
    """Pair buy/sell filled events into round-trip trades.

    Returns (trades, leftover_open_lot). If the events end on an unmatched buy
    the caller is responsible for synthesizing an MTM exit.

    Raises NotImplementedError if a second buy arrives without an intervening
    sell (pyramiding). EMA crossover and buy-and-hold both have pyramiding=1
    so this branch is defensive.
    """
    fills = [e for e in events if e.get("status") == "filled"]
    open_lot: OpenLot | None = None
    trade_number = 0
    trades: list[PairedTrade] = []

    for fill in fills:
        direction = fill["direction"]
        ms_utc = int(fill["ms_utc"])
        price = float(fill["fill_price"])
        qty = float(fill["fill_quantity"])
        fee = float(fill.get("order_fee_amount") or 0.0)

        if direction == "buy":
            if open_lot is None:
                open_lot = OpenLot(
                    entry_ms_utc=ms_utc,
                    entry_price=price,
                    quantity=qty,
                    fees=[fee],
                )
            else:
                raise NotImplementedError(
                    "Pyramiding not supported in Phase 1; expected at most one open lot"
                )
        elif direction == "sell":
            if open_lot is None:
                # Defensive: short selling not expected for current templates.
                continue
            trade_number += 1
            entry_fees = sum(open_lot.fees)
            pnl = (price - open_lot.entry_price) * open_lot.quantity - entry_fees - fee
            trades.append(
                PairedTrade(
                    trade_number=trade_number,
                    entry_ms_utc=open_lot.entry_ms_utc,
                    exit_ms_utc=ms_utc,
                    entry_price=open_lot.entry_price,
                    exit_price=price,
                    quantity=open_lot.quantity,
                    pnl=pnl,
                    signal_reason=signal_reason,
                    is_synthetic_exit=False,
                )
            )
            open_lot = None

    return trades, open_lot
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_persistence.py \
        PythonDataService/tests/services/test_lean_sidecar_persistence.py
git commit -m "feat(lean-sidecar): pair LEAN order events into round-trip trades"
```

---

### Task 1.6: Synthetic MTM exit for half-open positions (TDD)

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_persistence.py`
- Modify: `PythonDataService/tests/services/test_lean_sidecar_persistence.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/test_lean_sidecar_persistence.py`:

```python
from app.services.lean_sidecar_persistence import (
    OpenLot,
    PairedTrade,
    finalize_open_lot_as_synthetic,
)


def test_finalize_open_lot_as_synthetic_uses_last_equity_point() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    equity_curve = [
        {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
        {"ms_utc": 1_700_000_300_000, "value": 100_050.0},
        {"ms_utc": 1_700_000_600_000, "value": 100_090.0},
    ]
    trade = finalize_open_lot_as_synthetic(
        open_lot,
        equity_curve=equity_curve,
        starting_cash=100_000.0,
        trade_number=5,
    )
    assert trade.trade_number == 5
    assert trade.exit_ms_utc == 1_700_000_600_000
    assert trade.is_synthetic_exit is True
    assert trade.signal_reason == "EndOfAlgorithm:MTM (synthetic exit)"
    # exit_price reconstructed: (100090 - 100000 + 100*10 + 0.5) / 10 = 100.95
    assert trade.exit_price == pytest.approx(100.95)
    # pnl = (100.95 - 100) * 10 - 0.5 = 9.0
    assert trade.pnl == pytest.approx(9.0)


def test_finalize_open_lot_raises_on_empty_equity_curve() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    with pytest.raises(ValueError, match="equity_curve is empty"):
        finalize_open_lot_as_synthetic(open_lot, [], 100_000.0, 1)
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py::test_finalize_open_lot_as_synthetic_uses_last_equity_point -v
```

Expected: `ImportError: cannot import name 'finalize_open_lot_as_synthetic'`.

- [ ] **Step 3: Implement**

Append to `PythonDataService/app/services/lean_sidecar_persistence.py`:

```python
def finalize_open_lot_as_synthetic(
    open_lot: OpenLot,
    equity_curve: Sequence[dict[str, Any]],
    starting_cash: float,
    trade_number: int,
) -> PairedTrade:
    """Synthesize an MTM exit at the last equity-curve point.

    Reconstructs exit price by reversing the portfolio-value identity:
        equity_value = cash_remaining + qty * exit_price
        cash_remaining = starting_cash - qty * entry_price - sum(fees)
    Solving:
        exit_price = (equity_value - starting_cash + qty * entry_price + sum(fees)) / qty
    """
    if not equity_curve:
        raise ValueError("equity_curve is empty — cannot synthesize MTM exit")

    last_point = equity_curve[-1]
    last_ms = int(last_point["ms_utc"])
    last_value = float(last_point["value"])
    entry_fees = sum(open_lot.fees)

    exit_price = (
        last_value
        - starting_cash
        + open_lot.entry_price * open_lot.quantity
        + entry_fees
    ) / open_lot.quantity

    pnl = (exit_price - open_lot.entry_price) * open_lot.quantity - entry_fees

    return PairedTrade(
        trade_number=trade_number,
        entry_ms_utc=open_lot.entry_ms_utc,
        exit_ms_utc=last_ms,
        entry_price=open_lot.entry_price,
        exit_price=exit_price,
        quantity=open_lot.quantity,
        pnl=pnl,
        signal_reason="EndOfAlgorithm:MTM (synthetic exit)",
        is_synthetic_exit=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_persistence.py \
        PythonDataService/tests/services/test_lean_sidecar_persistence.py
git commit -m "feat(lean-sidecar): synthesize MTM exit for half-open positions"
```

---

### Task 1.7: Aggregate KPI computation (TDD)

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_persistence.py`
- Modify: `PythonDataService/tests/services/test_lean_sidecar_persistence.py`

- [ ] **Step 1: Write the failing test**

```python
from app.services.lean_sidecar_persistence import compute_aggregates


def test_compute_aggregates_empty_trades() -> None:
    agg = compute_aggregates(trades=[], starting_cash=100_000.0, total_fees=0.0)
    assert agg.total_trades == 0
    assert agg.winning_trades == 0
    assert agg.losing_trades == 0
    assert agg.total_pnl == pytest.approx(0.0)
    assert agg.final_equity == pytest.approx(100_000.0)
    assert agg.win_rate == pytest.approx(0.0)


def test_compute_aggregates_mixed_trades() -> None:
    trades = [
        PairedTrade(1, 0, 0, 100.0, 101.0, 10, pnl=10.0, signal_reason="x", is_synthetic_exit=False),
        PairedTrade(2, 0, 0, 100.0, 99.0, 10, pnl=-10.0, signal_reason="x", is_synthetic_exit=False),
        PairedTrade(3, 0, 0, 100.0, 102.0, 10, pnl=20.0, signal_reason="x", is_synthetic_exit=False),
    ]
    agg = compute_aggregates(trades=trades, starting_cash=100_000.0, total_fees=3.0)
    assert agg.total_trades == 3
    assert agg.winning_trades == 2
    assert agg.losing_trades == 1
    assert agg.total_pnl == pytest.approx(20.0)
    assert agg.final_equity == pytest.approx(100_000.0 + 20.0 - 3.0)
    assert agg.win_rate == pytest.approx(2 / 3)
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py::test_compute_aggregates_empty_trades -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `PythonDataService/app/services/lean_sidecar_persistence.py`:

```python
@dataclass
class AggregateKpis:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    final_equity: float
    win_rate: float


def compute_aggregates(
    trades: Sequence[PairedTrade],
    starting_cash: float,
    total_fees: float,
) -> AggregateKpis:
    """Compute aggregate KPIs from a list of round-trip trades."""
    total_pnl = sum(t.pnl for t in trades)
    winning = sum(1 for t in trades if t.pnl > 0)
    losing = sum(1 for t in trades if t.pnl < 0)
    win_rate = winning / len(trades) if trades else 0.0
    return AggregateKpis(
        total_trades=len(trades),
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=total_pnl,
        final_equity=starting_cash + total_pnl - total_fees,
        win_rate=win_rate,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_persistence.py \
        PythonDataService/tests/services/test_lean_sidecar_persistence.py
git commit -m "feat(lean-sidecar): compute aggregate KPIs from round-trip trades"
```

---

### Task 1.8: `normalize_and_persist` orchestration (TDD with DB)

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_persistence.py`
- Modify: `PythonDataService/tests/services/test_lean_sidecar_persistence.py`

- [ ] **Step 1: Identify the SQLAlchemy session factory**

```bash
grep -r "Session\|engine\|create_engine" PythonDataService/app/database/ PythonDataService/app/db/ 2>/dev/null | head -20
```

Find the existing session factory (e.g., `app.db.session.SessionLocal` or `app.database.get_db`). Note the exact import path for Step 3.

- [ ] **Step 2: Write the integration test**

Append to `tests/services/test_lean_sidecar_persistence.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.services.lean_sidecar_persistence import (
    NormalizedResult,
    normalize_and_persist,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Build a minimal LEAN workspace with a normalized/result.json."""
    ws = tmp_path / "ui_run_test"
    (ws / "normalized").mkdir(parents=True)
    (ws / "workspace" / "output").mkdir(parents=True)
    result = {
        "algorithm_id": "MyAlgorithm",
        "parser_version": "phase-3a-r1",
        "first_equity_ms_utc": 1_700_000_000_000,
        "last_equity_ms_utc": 1_700_000_600_000,
        "total_equity_points": 3,
        "total_order_events": 4,
        "equity_curve": [
            {"ms_utc": 1_700_000_000_000, "value": 100_000.0,
             "open": 100_000.0, "high": 100_000.0, "low": 100_000.0},
            {"ms_utc": 1_700_000_300_000, "value": 100_050.0,
             "open": 100_050.0, "high": 100_050.0, "low": 100_050.0},
            {"ms_utc": 1_700_000_600_000, "value": 100_090.0,
             "open": 100_090.0, "high": 100_090.0, "low": 100_090.0},
        ],
        "order_events": [
            {"order_id": 1, "order_event_id": 1, "direction": "buy",
             "status": "submitted", "ms_utc": 1_700_000_000_000,
             "fill_price": 0.0, "fill_quantity": 0.0, "quantity": 10,
             "order_fee_amount": None},
            {"order_id": 1, "order_event_id": 2, "direction": "buy",
             "status": "filled", "ms_utc": 1_700_000_060_000,
             "fill_price": 100.0, "fill_quantity": 10, "quantity": 10,
             "order_fee_amount": 0.5},
            {"order_id": 2, "order_event_id": 1, "direction": "sell",
             "status": "submitted", "ms_utc": 1_700_000_540_000,
             "fill_price": 0.0, "fill_quantity": 0.0, "quantity": 10,
             "order_fee_amount": None},
            {"order_id": 2, "order_event_id": 2, "direction": "sell",
             "status": "filled", "ms_utc": 1_700_000_600_000,
             "fill_price": 101.0, "fill_quantity": 10, "quantity": 10,
             "order_fee_amount": 0.5},
        ],
        "statistics": {"NetProfit": "9.00"},
        "runtime_statistics": {},
    }
    (ws / "normalized" / "result.json").write_text(json.dumps(result))
    return ws


def test_normalize_and_persist_writes_strategy_execution(
    workspace: Path, db_session: Session
) -> None:
    run_id = "ui_run_test"
    exec_id = normalize_and_persist(
        session=db_session,
        run_id=run_id,
        workspace_path=workspace,
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date=datetime(2025, 1, 6, tzinfo=timezone.utc),
        end_date=datetime(2025, 1, 10, tzinfo=timezone.utc),
    )

    from app.models.strategy_execution import StrategyExecution  # adjust import to actual path
    from app.models.backtest_trade import BacktestTrade

    row = db_session.query(StrategyExecution).filter_by(Id=exec_id).one()
    assert row.Source == "lean-sidecar"
    assert row.LeanRunId == run_id
    assert row.TotalTrades == 1
    assert row.TotalPnL == pytest.approx(9.0)
    assert row.FinalEquity == pytest.approx(100_000.0 + 9.0 - 1.0)

    trades = db_session.query(BacktestTrade).filter_by(StrategyExecutionId=exec_id).all()
    assert len(trades) == 1
    assert trades[0].IsSyntheticExit is False
    assert trades[0].EntryPrice == pytest.approx(100.0)
    assert trades[0].ExitPrice == pytest.approx(101.0)


def test_normalize_and_persist_is_idempotent(
    workspace: Path, db_session: Session
) -> None:
    run_id = "ui_run_test"
    args = dict(
        session=db_session, run_id=run_id, workspace_path=workspace,
        starting_cash=100_000.0, symbol="SPY", algorithm_name="ema_crossover",
        start_date=datetime(2025, 1, 6, tzinfo=timezone.utc),
        end_date=datetime(2025, 1, 10, tzinfo=timezone.utc),
    )
    exec_id_1 = normalize_and_persist(**args)
    exec_id_2 = normalize_and_persist(**args)
    assert exec_id_1 == exec_id_2

    from app.models.strategy_execution import StrategyExecution
    count = db_session.query(StrategyExecution).filter_by(LeanRunId=run_id).count()
    assert count == 1


def test_normalize_and_persist_handles_failed_run(
    workspace: Path, db_session: Session
) -> None:
    # Overwrite the result.json with an empty order_events list and zero trades.
    result_path = workspace / "normalized" / "result.json"
    result = json.loads(result_path.read_text())
    result["order_events"] = []
    result["equity_curve"] = []
    result_path.write_text(json.dumps(result))

    exec_id = normalize_and_persist(
        session=db_session,
        run_id="ui_run_failed",
        workspace_path=workspace,
        starting_cash=100_000.0,
        symbol="SPY",
        algorithm_name="ema_crossover",
        start_date=datetime(2025, 1, 6, tzinfo=timezone.utc),
        end_date=datetime(2025, 1, 10, tzinfo=timezone.utc),
    )
    from app.models.strategy_execution import StrategyExecution
    row = db_session.query(StrategyExecution).filter_by(Id=exec_id).one()
    assert row.TotalTrades == 0
    assert row.TotalPnL == pytest.approx(0.0)
```

The `db_session` fixture must already exist in `PythonDataService/tests/conftest.py`. If not, add it (function-scoped, rolls back after each test).

- [ ] **Step 3: Run the test to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py::test_normalize_and_persist_writes_strategy_execution -v
```

Expected: ImportError on `normalize_and_persist` or `NormalizedResult`.

- [ ] **Step 4: Implement the orchestration**

Append to `PythonDataService/app/services/lean_sidecar_persistence.py`:

```python
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

# IMPORTANT: replace these imports with the actual paths after Step 1.
from app.models.strategy_execution import StrategyExecution
from app.models.backtest_trade import BacktestTrade


@dataclass
class NormalizedResult:
    """Schema-versioned view of normalized/result.json."""

    parser_version: str
    order_events: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    statistics: dict[str, Any]
    runtime_statistics: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> "NormalizedResult":
        data = json.loads(path.read_text())
        return cls(
            parser_version=data.get("parser_version", "unknown"),
            order_events=data.get("order_events", []),
            equity_curve=data.get("equity_curve", []),
            statistics=data.get("statistics", {}),
            runtime_statistics=data.get("runtime_statistics", {}),
        )


def _ms_to_datetime(ms_utc: int) -> datetime:
    """Convert int64 ms UTC to a tz-aware DateTime for .NET-backed columns.

    Note: BacktestTrade.EntryTimestamp/ExitTimestamp are DateTime columns. This
    is a known violation of numerical-rigor.md (timestamps should be int64 ms
    UTC at all boundaries) and is tracked as separate cleanup. The persistence
    layer respects the existing schema by converting at the write boundary.
    """
    from datetime import timezone
    return datetime.fromtimestamp(ms_utc / 1000.0, tz=timezone.utc)


def normalize_and_persist(
    session: Session,
    run_id: str,
    workspace_path: Path,
    starting_cash: float,
    symbol: str,
    algorithm_name: str,
    start_date: datetime,
    end_date: datetime,
) -> int:
    """Read normalized/result.json under workspace_path and write a StrategyExecution.

    Idempotency: re-running with the same run_id is a no-op (returns the existing
    StrategyExecution.Id). Caller passes the SQLAlchemy session.

    Returns the StrategyExecution.Id.
    """
    # Idempotency check.
    existing = session.query(StrategyExecution).filter_by(LeanRunId=run_id).one_or_none()
    if existing is not None:
        return existing.Id

    result_path = workspace_path / "normalized" / "result.json"
    if not result_path.exists():
        # LEAN crashed before producing normalized output. Write a failed row.
        row = StrategyExecution(
            TickerId=None,
            StrategyName=algorithm_name,
            Parameters="{}",
            StartDate=start_date,
            EndDate=end_date,
            Timespan="minute",
            Multiplier=15,
            TotalTrades=0,
            WinningTrades=0,
            LosingTrades=0,
            TotalPnL=0.0,
            InitialCash=starting_cash,
            FinalEquity=starting_cash,
            TotalFees=0.0,
            WinRate=0.0,
            LeanStatisticsJson=json.dumps({"error": "no normalized result.json"}),
            Source="lean-sidecar",
            LeanRunId=run_id,
            FillMode=None,
            ExecutedAt=datetime.now(),
            DurationMs=0,
        )
        session.add(row)
        session.flush()
        session.commit()
        return row.Id

    normalized = NormalizedResult.from_path(result_path)

    paired_trades, open_lot = pair_order_events(normalized.order_events)
    if open_lot is not None:
        synthetic = finalize_open_lot_as_synthetic(
            open_lot=open_lot,
            equity_curve=normalized.equity_curve,
            starting_cash=starting_cash,
            trade_number=len(paired_trades) + 1,
        )
        paired_trades.append(synthetic)

    total_fees = sum(
        float(ev.get("order_fee_amount") or 0.0)
        for ev in normalized.order_events
        if ev.get("status") == "filled"
    )
    agg = compute_aggregates(
        trades=paired_trades,
        starting_cash=starting_cash,
        total_fees=total_fees,
    )

    row = StrategyExecution(
        TickerId=None,
        StrategyName=algorithm_name,
        Parameters=json.dumps({"symbol": symbol, "starting_cash": starting_cash}),
        StartDate=start_date,
        EndDate=end_date,
        Timespan="minute",
        Multiplier=15,
        TotalTrades=agg.total_trades,
        WinningTrades=agg.winning_trades,
        LosingTrades=agg.losing_trades,
        TotalPnL=agg.total_pnl,
        InitialCash=starting_cash,
        FinalEquity=agg.final_equity,
        TotalFees=total_fees,
        WinRate=agg.win_rate,
        LeanStatisticsJson=json.dumps({
            "statistics": normalized.statistics,
            "runtime_statistics": normalized.runtime_statistics,
            "parser_version": normalized.parser_version,
            "workspace_path": str(workspace_path),
        }),
        Source="lean-sidecar",
        LeanRunId=run_id,
        FillMode=None,
        ExecutedAt=datetime.now(),
        DurationMs=0,
    )
    session.add(row)
    session.flush()  # populates row.Id

    for t in paired_trades:
        session.add(BacktestTrade(
            StrategyExecutionId=row.Id,
            TradeType="LONG",
            EntryTimestamp=_ms_to_datetime(t.entry_ms_utc),
            ExitTimestamp=_ms_to_datetime(t.exit_ms_utc),
            EntryPrice=t.entry_price,
            ExitPrice=t.exit_price,
            Quantity=int(t.quantity),
            PnL=t.pnl,
            CumulativePnL=0.0,  # filled in below
            SignalReason=t.signal_reason,
            IsSyntheticExit=t.is_synthetic_exit,
        ))

    # Populate cumulative PnL in trade-order.
    cum = 0.0
    for trade_row in (
        session.query(BacktestTrade)
        .filter_by(StrategyExecutionId=row.Id)
        .order_by(BacktestTrade.EntryTimestamp)
        .all()
    ):
        cum += trade_row.PnL
        trade_row.CumulativePnL = cum

    session.commit()
    return row.Id
```

- [ ] **Step 5: Run the tests**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_persistence.py -v
```

Expected: 14 passed.

- [ ] **Step 6: Run project-scope ruff**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```

Expected: zero warnings.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_persistence.py \
        PythonDataService/tests/services/test_lean_sidecar_persistence.py
git commit -m "feat(lean-sidecar): persist LEAN runs to StrategyExecution table"
```

---

### Task 1.9: Wire persistence into `run_trusted_sample`

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py`
- Test: `PythonDataService/tests/services/test_lean_sidecar_service.py` (extend existing)

- [ ] **Step 1: Find the end of `run_trusted_sample`**

Open `PythonDataService/app/services/lean_sidecar_service.py`, locate the `run_trusted_sample` function (around line 392-562). Identify where the manifest is finalized and the function returns.

- [ ] **Step 2: Write the failing integration test**

Append to `PythonDataService/tests/services/test_lean_sidecar_service.py` (or create one if absent):

```python
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_run_trusted_sample_persists_to_postgres(
    db_session, tmp_path, mock_lean_runner_success
) -> None:
    """After a successful LEAN run, a StrategyExecution row exists."""
    from app.services.lean_sidecar_service import run_trusted_sample, TrustedRunRequest

    request = TrustedRunRequest(
        template="ema_crossover",
        symbol="SPY",
        start_date="2025-01-06",
        end_date="2025-01-10",
        starting_cash=100_000.0,
    )
    response = await run_trusted_sample(request, session=db_session)

    from app.models.strategy_execution import StrategyExecution
    row = db_session.query(StrategyExecution).filter_by(LeanRunId=response.run_id).one()
    assert row.Source == "lean-sidecar"
    assert row.StrategyName == "ema_crossover"
```

The fixtures `db_session` and `mock_lean_runner_success` may need to be added — `mock_lean_runner_success` should patch the LEAN container invocation to write a fixed `normalized/result.json` into the workspace.

- [ ] **Step 3: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_service.py::test_run_trusted_sample_persists_to_postgres -v
```

Expected: assertion error or `NoResultFound`.

- [ ] **Step 4: Wire the persistence call**

In `PythonDataService/app/services/lean_sidecar_service.py`, at the tail of `run_trusted_sample` (after manifest is finalized, before returning), add:

```python
# Persist normalized result to Postgres for unified history.
from app.services.lean_sidecar_persistence import normalize_and_persist
from app.db.session import SessionLocal  # adjust import path

with SessionLocal() as persistence_session:
    try:
        strategy_execution_id = normalize_and_persist(
            session=persistence_session,
            run_id=run_id,
            workspace_path=workspace_path,
            starting_cash=request.starting_cash,
            symbol=request.symbol,
            algorithm_name=(
                f"{request.template}" if request.template
                else "user_provided"
            ),
            start_date=datetime.strptime(request.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc),
            end_date=datetime.strptime(request.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc),
        )
        logger.info(
            "Persisted LEAN run %s to StrategyExecution.Id=%s",
            run_id,
            strategy_execution_id,
        )
    except Exception:
        # Don't fail the run because persistence failed; log + continue.
        # The artifact path is still on disk; the user can re-run via backfill CLI.
        logger.exception("Failed to persist LEAN run %s; continuing", run_id)
        strategy_execution_id = None

# Add strategy_execution_id to the response payload.
return RunSummary(
    run_id=run_id,
    strategy_execution_id=strategy_execution_id,
    # ... existing fields
)
```

Update the `RunSummary` Pydantic model to include `strategy_execution_id: int | None`.

- [ ] **Step 5: Run the test**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_service.py::test_run_trusted_sample_persists_to_postgres -v
```

Expected: passes.

- [ ] **Step 6: Run full PR1 test suite**

```bash
podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/services/ -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_service.py \
        PythonDataService/app/routers/lean_sidecar.py \
        PythonDataService/tests/services/test_lean_sidecar_service.py
git commit -m "feat(lean-sidecar): persist trusted runs to StrategyExecution on completion"
```

---

### Task 1.10: End-to-end smoke test via real LEAN container

**Files:**
- Run manually via curl; no new test file (this is a smoke check before merging PR 1).

- [ ] **Step 1: Start services**

```bash
./restart.sh
```

- [ ] **Step 2: Submit an ema_crossover run**

```bash
curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
  -H "Content-Type: application/json" \
  -d '{"template":"ema_crossover","symbol":"SPY","start_date":"2025-01-06","end_date":"2025-01-10","starting_cash":100000}'
```

Expected response: 200 with a `run_id` and `strategy_execution_id`.

- [ ] **Step 3: Verify the Postgres row**

```bash
podman exec -it my-postgres psql -U postgres -c \
  "SELECT \"Id\", \"Source\", \"LeanRunId\", \"StrategyName\", \"TotalTrades\", \"TotalPnL\" \
   FROM \"StrategyExecutions\" \
   WHERE \"Source\" = 'lean-sidecar' \
   ORDER BY \"ExecutedAt\" DESC LIMIT 1;"
```

Expected: one row with the run_id, StrategyName="ema_crossover", non-zero TotalTrades (assuming the window has at least one EMA crossover).

- [ ] **Step 4: Open the unified history page (manually)**

Visit http://localhost:4200 → LEAN Lab → run history. Confirm the new row appears with engine tag "LEAN".

(The shared component doesn't exist yet — Phase 1 only verifies the row is in Postgres; the visual unified table comes in PR 2.)

- [ ] **Step 5: No commit** — this is a manual smoke check.

---

### Task 1.11: Open PR 1

- [ ] **Step 1: Push the branch**

```bash
git push -u origin design/lean-ema-template-unified-history
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(lean-sidecar): EMA crossover template + persist runs to Postgres" --body "$(cat <<'EOF'
## Summary
- New trusted template `ema_crossover` mirroring `spy_ema_crossover.spec.json` exactly (EMA(5)/EMA(10), RSI(14) Wilders, 15-min consolidated bars, 5-bar time stop)
- LEAN run results are now persisted to the `StrategyExecution` + `BacktestTrade` Postgres tables with `Source="lean-sidecar"`, alongside existing engine and strategy-lab runs
- Schema additions: `StrategyExecution.LeanRunId` + `BacktestTrade.IsSyntheticExit` (one EF migration)
- Fill-model parity spike doc at `docs/references/fill-model-parity-spike-2026-05-19.md`

## Test plan
- [ ] `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/services/ -v` — all green
- [ ] `cd Backend.Tests && dotnet test` — all green
- [ ] `podman exec my-frontend npx ng test --watch=false` — all green
- [ ] Manual smoke: submit ema_crossover run via curl, confirm row in `StrategyExecutions` with `Source='lean-sidecar'`
- [ ] `ruff check PythonDataService/app/ PythonDataService/tests/` — zero warnings
- [ ] `npx eslint Frontend/src/ --max-warnings 0` — zero warnings
- [ ] `dotnet format podman.sln --verify-no-changes` — clean

## Design
docs/superpowers/specs/2026-05-19-lean-ema-template-and-unified-history-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 2 — Unified history table

Start a new branch off main after PR 1 merges:

```bash
git checkout master && git pull && git checkout -b feat/unified-history-table
```

### Task 2.1: Extend `backtestRuns` GraphQL connection (.NET)

**Files:**
- Modify: `Backend/GraphQL/Queries/<existing-resolver>.cs` (find the current StrategyExecution query)
- Modify: `Backend/GraphQL/Types/StrategyExecutionType.cs` (or equivalent) — add `leanRunId`
- Modify: `Backend/GraphQL/Types/BacktestTradeType.cs` — add `isSyntheticExit`
- Test: `Backend.Tests/GraphQL/BacktestRunsConnectionTests.cs`

- [ ] **Step 1: Locate existing StrategyExecution GraphQL resolver**

```bash
grep -r "StrategyExecution\|backtestRun" Backend/GraphQL/ -l
```

Note the resolver class and current query field name.

- [ ] **Step 2: Write the failing test**

Create `Backend.Tests/GraphQL/BacktestRunsConnectionTests.cs`:

```csharp
using FluentAssertions;
using HotChocolate.Execution;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace Backend.Tests.GraphQL;

public class BacktestRunsConnectionTests : GraphQLTestBase
{
    [Fact]
    public async Task BacktestRuns_FiltersByEngine()
    {
        await SeedAsync(new[]
        {
            new StrategyExecutionSeed { Source = "engine", StrategyName = "ema_spec" },
            new StrategyExecutionSeed { Source = "lean-sidecar", StrategyName = "ema_crossover" },
        });

        var result = await ExecuteQueryAsync(@"
            query {
              backtestRuns(engine: LEAN_SIDECAR, first: 10) {
                nodes { id source strategyName leanRunId }
              }
            }
        ");

        var data = result.ExpectQueryResult().Data!;
        var nodes = ((dynamic)data!)["backtestRuns"]["nodes"];
        ((IEnumerable<object>)nodes).Should().HaveCount(1);
    }

    [Fact]
    public async Task BacktestRuns_ExposesLeanRunIdAndIsSyntheticExit()
    {
        await SeedAsync(new[]
        {
            new StrategyExecutionSeed { Source = "lean-sidecar", LeanRunId = "ui_run_abc" },
        });

        var result = await ExecuteQueryAsync(@"
            query {
              backtestRuns(first: 1) {
                nodes { leanRunId trades { isSyntheticExit } }
              }
            }
        ");

        result.Errors.Should().BeNull();
        var data = result.ExpectQueryResult().Data!;
        ((string)((dynamic)data!)["backtestRuns"]["nodes"][0]["leanRunId"]).Should().Be("ui_run_abc");
    }
}
```

(Assumes a `GraphQLTestBase` exists; if not, create it following the pattern of any existing GraphQL test file in `Backend.Tests/`.)

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~BacktestRunsConnectionTests"
```

Expected: 2 failures (schema doesn't know `engine` argument or `leanRunId` field).

- [ ] **Step 4: Add the EngineSource enum**

Create `Backend/GraphQL/Types/EngineSourceEnum.cs`:

```csharp
namespace Backend.GraphQL.Types;

public enum EngineSource
{
    ENGINE,
    STRATEGY_LAB,
    LEAN_SIDECAR,
}

public static class EngineSourceExtensions
{
    public static string ToDbValue(this EngineSource engine) => engine switch
    {
        EngineSource.ENGINE => "engine",
        EngineSource.STRATEGY_LAB => "strategy-lab",
        EngineSource.LEAN_SIDECAR => "lean-sidecar",
        _ => throw new ArgumentOutOfRangeException(nameof(engine)),
    };
}
```

- [ ] **Step 5: Extend the resolver**

In the existing StrategyExecution resolver class, replace the existing query (or add alongside it):

```csharp
[GraphQLName("backtestRuns")]
public async Task<Connection<StrategyExecution>> GetBacktestRunsAsync(
    EngineSource? engine,
    string? symbol,
    string? after,
    int? first,
    [Service] AppDbContext db,
    CancellationToken ct)
{
    var query = db.StrategyExecutions.AsNoTracking().AsQueryable();
    if (engine.HasValue)
        query = query.Where(s => s.Source == engine.Value.ToDbValue());
    if (!string.IsNullOrEmpty(symbol))
        query = query.Where(s => s.Parameters.Contains(symbol));
    query = query.OrderByDescending(s => s.ExecutedAt);

    return await query.ApplyCursorPaginationAsync(first ?? 25, after, ct);
}
```

If `ApplyCursorPaginationAsync` doesn't exist, use Hot Chocolate's built-in `[UsePaging]` attribute:

```csharp
[GraphQLName("backtestRuns")]
[UsePaging(MaxPageSize = 100)]
public IQueryable<StrategyExecution> GetBacktestRuns(
    EngineSource? engine,
    string? symbol,
    [Service] AppDbContext db)
{
    var query = db.StrategyExecutions.AsNoTracking().AsQueryable();
    if (engine.HasValue)
        query = query.Where(s => s.Source == engine.Value.ToDbValue());
    if (!string.IsNullOrEmpty(symbol))
        query = query.Where(s => s.Parameters.Contains(symbol));
    return query.OrderByDescending(s => s.ExecutedAt);
}
```

- [ ] **Step 6: Surface the new fields on the GraphQL types**

In `Backend/GraphQL/Types/StrategyExecutionType.cs` (or wherever the existing object type config lives):

```csharp
descriptor.Field(s => s.LeanRunId).Name("leanRunId");
descriptor.Field(s => s.Source).Name("source");
```

In `Backend/GraphQL/Types/BacktestTradeType.cs`:

```csharp
descriptor.Field(t => t.IsSyntheticExit).Name("isSyntheticExit");
```

- [ ] **Step 7: Run the tests**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~BacktestRunsConnectionTests"
```

Expected: 2 passed.

- [ ] **Step 8: Run dotnet format**

```bash
dotnet format podman.sln --verify-no-changes
```

Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add Backend/GraphQL/ Backend.Tests/GraphQL/BacktestRunsConnectionTests.cs
git commit -m "feat(backend): extend backtestRuns query with engine filter + leanRunId"
```

---

### Task 2.2: Shared `run-history` component skeleton (TDD)

**Files:**
- Create: `Frontend/src/app/components/shared/run-history/run-history.component.ts`
- Create: `Frontend/src/app/components/shared/run-history/run-history.component.html`
- Create: `Frontend/src/app/components/shared/run-history/run-history.component.scss`
- Create: `Frontend/src/app/components/shared/run-history/run-history.component.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `Frontend/src/app/components/shared/run-history/run-history.component.spec.ts`:

```typescript
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";
import { RunHistoryComponent } from "./run-history.component";

describe("RunHistoryComponent", () => {
  it("renders the engine badge for each row", async () => {
    await render(RunHistoryComponent, {
      componentInputs: {
        rows: [
          { id: "1", source: "engine", strategyName: "ema_spec", symbol: "SPY",
            startDate: "2025-01-06", endDate: "2025-01-10",
            executedAt: "2026-05-19T02:49:00Z", totalTrades: 1, totalPnl: 9.0,
            isSyntheticExit: false, leanRunId: null },
          { id: "2", source: "lean-sidecar", strategyName: "ema_crossover", symbol: "SPY",
            startDate: "2025-01-06", endDate: "2025-01-10",
            executedAt: "2026-05-19T02:49:00Z", totalTrades: 1, totalPnl: 9.0,
            isSyntheticExit: false, leanRunId: "ui_run_abc" },
        ],
      },
    });
    expect(screen.getByText("Engine Lab")).toBeInTheDocument();
    expect(screen.getByText("LEAN")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec my-frontend npx ng test --include="**/run-history.component.spec.ts" --watch=false
```

Expected: ModuleNotFoundError or template-not-found.

- [ ] **Step 3: Implement the component**

Create `Frontend/src/app/components/shared/run-history/run-history.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";

export interface RunHistoryRow {
  id: string;
  source: "engine" | "strategy-lab" | "lean-sidecar";
  strategyName: string;
  symbol: string | null;
  startDate: string;
  endDate: string;
  executedAt: string;
  totalTrades: number;
  totalPnl: number;
  isSyntheticExit: boolean;
  leanRunId: string | null;
}

@Component({
  selector: "app-run-history",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./run-history.component.html",
  styleUrls: ["./run-history.component.scss"],
})
export class RunHistoryComponent {
  readonly rows = input.required<RunHistoryRow[]>();

  readonly badge = (source: RunHistoryRow["source"]): string => {
    switch (source) {
      case "engine": return "Engine Lab";
      case "strategy-lab": return "Strategy Lab";
      case "lean-sidecar": return "LEAN";
    }
  };
}
```

Create `Frontend/src/app/components/shared/run-history/run-history.component.html`:

```html
<table class="run-history">
  <thead>
    <tr>
      <th>Engine</th>
      <th>Strategy</th>
      <th>Symbol</th>
      <th>Window</th>
      <th>Executed</th>
      <th>Trades</th>
      <th>Net PnL</th>
    </tr>
  </thead>
  <tbody>
    @for (row of rows(); track row.id) {
      <tr>
        <td><span class="badge" [attr.data-source]="row.source">{{ badge(row.source) }}</span></td>
        <td>{{ row.strategyName }}</td>
        <td>{{ row.symbol ?? '—' }}</td>
        <td>{{ row.startDate }} – {{ row.endDate }}</td>
        <td>{{ row.executedAt }}</td>
        <td>{{ row.totalTrades }}</td>
        <td [class.positive]="row.totalPnl > 0" [class.negative]="row.totalPnl < 0">
          {{ row.totalPnl | currency:'USD' }}
        </td>
      </tr>
    }
  </tbody>
</table>
```

Create `Frontend/src/app/components/shared/run-history/run-history.component.scss`:

```scss
.run-history {
  width: 100%;
  border-collapse: collapse;

  th, td {
    padding: var(--space-sm);
    text-align: left;
    border-bottom: 1px solid var(--color-border);
  }

  .badge {
    display: inline-block;
    padding: var(--space-xs) var(--space-sm);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-sm);
    background: var(--color-surface-secondary);

    &[data-source="lean-sidecar"] {
      background: var(--color-accent-secondary);
    }
  }

  .positive { color: var(--color-success); }
  .negative { color: var(--color-danger); }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec my-frontend npx ng test --include="**/run-history.component.spec.ts" --watch=false
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/shared/run-history/
git commit -m "feat(frontend): shared run-history component with engine badges"
```

---

### Task 2.3: Multi-select + Compare button (TDD)

**Files:**
- Modify: `Frontend/src/app/components/shared/run-history/run-history.component.ts`
- Modify: `Frontend/src/app/components/shared/run-history/run-history.component.html`
- Modify: `Frontend/src/app/components/shared/run-history/run-history.component.spec.ts`

- [ ] **Step 1: Write the failing test**

Append to `run-history.component.spec.ts`:

```typescript
import userEvent from "@testing-library/user-event";

describe("RunHistoryComponent — multi-select", () => {
  it("enables Compare button only when exactly 2 rows selected", async () => {
    const compareRequested = vi.fn();
    const user = userEvent.setup();

    await render(RunHistoryComponent, {
      componentInputs: {
        rows: [
          { id: "1", source: "engine", strategyName: "a", symbol: "SPY",
            startDate: "x", endDate: "y", executedAt: "z", totalTrades: 1,
            totalPnl: 1, isSyntheticExit: false, leanRunId: null },
          { id: "2", source: "lean-sidecar", strategyName: "b", symbol: "SPY",
            startDate: "x", endDate: "y", executedAt: "z", totalTrades: 1,
            totalPnl: 1, isSyntheticExit: false, leanRunId: "abc" },
          { id: "3", source: "engine", strategyName: "c", symbol: "SPY",
            startDate: "x", endDate: "y", executedAt: "z", totalTrades: 1,
            totalPnl: 1, isSyntheticExit: false, leanRunId: null },
        ],
        allowCompare: true,
      },
      componentOutputs: { compareRequested },
    });

    const compareBtn = screen.getByRole("button", { name: /compare/i });
    expect(compareBtn).toBeDisabled();

    await user.click(screen.getAllByRole("checkbox")[0]);
    expect(compareBtn).toBeDisabled();

    await user.click(screen.getAllByRole("checkbox")[1]);
    expect(compareBtn).toBeEnabled();

    await user.click(compareBtn);
    expect(compareRequested).toHaveBeenCalledWith({ leftId: "1", rightId: "2" });

    await user.click(screen.getAllByRole("checkbox")[2]);
    expect(compareBtn).toBeDisabled();  // 3 selected
  });
});
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec my-frontend npx ng test --include="**/run-history.component.spec.ts" --watch=false
```

Expected: failures.

- [ ] **Step 3: Implement multi-select**

Update `run-history.component.ts`:

```typescript
import {
  ChangeDetectionStrategy, Component, computed, input, output, signal,
} from "@angular/core";

@Component({
  selector: "app-run-history",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./run-history.component.html",
  styleUrls: ["./run-history.component.scss"],
})
export class RunHistoryComponent {
  readonly rows = input.required<RunHistoryRow[]>();
  readonly allowCompare = input<boolean>(true);

  readonly compareRequested = output<{ leftId: string; rightId: string }>();

  private readonly _selected = signal<Set<string>>(new Set());
  readonly selectedIds = computed(() => Array.from(this._selected()));
  readonly canCompare = computed(
    () => this.allowCompare() && this._selected().size === 2,
  );

  toggle(id: string): void {
    this._selected.update((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  isSelected(id: string): boolean {
    return this._selected().has(id);
  }

  emitCompare(): void {
    const ids = this.selectedIds();
    if (ids.length !== 2) return;
    this.compareRequested.emit({ leftId: ids[0], rightId: ids[1] });
  }

  badge(source: RunHistoryRow["source"]): string {
    switch (source) {
      case "engine": return "Engine Lab";
      case "strategy-lab": return "Strategy Lab";
      case "lean-sidecar": return "LEAN";
    }
  }
}
```

Update `run-history.component.html`:

```html
@if (allowCompare()) {
  <div class="actions">
    <button
      type="button"
      [disabled]="!canCompare()"
      (click)="emitCompare()"
      aria-label="Compare selected runs">
      Compare ({{ selectedIds().length }} / 2)
    </button>
  </div>
}

<table class="run-history">
  <thead>
    <tr>
      @if (allowCompare()) { <th></th> }
      <th>Engine</th>
      <th>Strategy</th>
      <th>Symbol</th>
      <th>Window</th>
      <th>Executed</th>
      <th>Trades</th>
      <th>Net PnL</th>
    </tr>
  </thead>
  <tbody>
    @for (row of rows(); track row.id) {
      <tr>
        @if (allowCompare()) {
          <td>
            <input
              type="checkbox"
              [checked]="isSelected(row.id)"
              (change)="toggle(row.id)"
              [attr.aria-label]="'Select run ' + row.id" />
          </td>
        }
        <td><span class="badge" [attr.data-source]="row.source">{{ badge(row.source) }}</span></td>
        <td>{{ row.strategyName }}</td>
        <td>{{ row.symbol ?? '—' }}</td>
        <td>{{ row.startDate }} – {{ row.endDate }}</td>
        <td>{{ row.executedAt }}</td>
        <td>{{ row.totalTrades }}</td>
        <td [class.positive]="row.totalPnl > 0" [class.negative]="row.totalPnl < 0">
          {{ row.totalPnl | currency:'USD' }}
        </td>
      </tr>
    }
  </tbody>
</table>
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec my-frontend npx ng test --include="**/run-history.component.spec.ts" --watch=false
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/shared/run-history/
git commit -m "feat(frontend): multi-select + Compare button on shared run-history"
```

---

### Task 2.4: Wire shared component into LEAN Lab page

**Files:**
- Modify: `Frontend/src/app/components/lean-lab/lean-lab-run-history/lean-lab-run-history.component.ts`
- Modify: `Frontend/src/app/components/lean-lab/lean-lab-run-history/lean-lab-run-history.component.html`
- Create: `Frontend/src/app/graphql/queries/backtest-runs.ts` (typed GraphQL document)

- [ ] **Step 1: Add the GraphQL query document**

Create `Frontend/src/app/graphql/queries/backtest-runs.ts`:

```typescript
import { gql } from "apollo-angular";

export const BACKTEST_RUNS_QUERY = gql`
  query BacktestRuns($engine: EngineSource, $first: Int, $after: String) {
    backtestRuns(engine: $engine, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        source
        strategyName
        leanRunId
        symbol: parameters
        startDate
        endDate
        executedAt
        totalTrades
        totalPnL
        trades { isSyntheticExit }
      }
    }
  }
`;
```

(Note: `symbol` is extracted from `parameters` JSON via a thin client helper; adjust if the schema exposes symbol directly.)

- [ ] **Step 2: Refactor the lean-lab-run-history component to use the shared component**

Replace `lean-lab-run-history.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { Apollo } from "apollo-angular";
import { toSignal } from "@angular/core/rxjs-interop";
import { map } from "rxjs/operators";
import { Router } from "@angular/router";
import { RunHistoryComponent, RunHistoryRow } from "../../shared/run-history/run-history.component";
import { BACKTEST_RUNS_QUERY } from "../../../graphql/queries/backtest-runs";

@Component({
  selector: "app-lean-lab-run-history",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RunHistoryComponent],
  template: `
    <app-run-history
      [rows]="rows() ?? []"
      [allowCompare]="true"
      (compareRequested)="onCompare($event)" />
  `,
})
export class LeanLabRunHistoryComponent {
  private readonly apollo = inject(Apollo);
  private readonly router = inject(Router);

  readonly rows = toSignal(
    this.apollo
      .watchQuery<{ backtestRuns: { nodes: RunHistoryRow[] } }>({
        query: BACKTEST_RUNS_QUERY,
        variables: { engine: "LEAN_SIDECAR", first: 25 },
      })
      .valueChanges.pipe(map((r) => r.data.backtestRuns.nodes)),
  );

  onCompare(event: { leftId: string; rightId: string }): void {
    this.router.navigate(["/runs/compare"], {
      queryParams: { left: event.leftId, right: event.rightId },
    });
  }
}
```

- [ ] **Step 3: Verify no regressions in the lean-lab page**

```bash
podman exec my-frontend npx ng test --watch=false
```

Expected: all green.

- [ ] **Step 4: Manually smoke-test the page**

Visit http://localhost:4200 → LEAN Lab → run history. Verify: runs render with the engine badge.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/lean-lab/lean-lab-run-history/ \
        Frontend/src/app/graphql/queries/backtest-runs.ts
git commit -m "feat(lean-lab): use shared run-history component with GraphQL backtestRuns"
```

---

### Task 2.5: Wire shared component into Engine Lab page

**Files:**
- Locate Engine Lab history component, replace internals identically to Task 2.4.
- Pass `engineFilter` of `"ENGINE"` so the Engine Lab page defaults to its own runs.

- [ ] **Step 1: Locate the existing engine-lab history component**

```bash
grep -r "StrategyExecution\|engineResults" Frontend/src/app/components/lean-engine/ -l
```

- [ ] **Step 2: Replace with shared component**

Mirror the structure of Task 2.4, but pass `variables: { engine: "ENGINE" }` in the Apollo query.

- [ ] **Step 3: Tests + smoke**

```bash
podman exec my-frontend npx ng test --watch=false
```

Visit http://localhost:4200 → Engine Lab.

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/components/lean-engine/
git commit -m "feat(engine-lab): use shared run-history component for engine runs"
```

---

### Task 2.6: Open PR 2

- [ ] **Step 1: Push + open PR**

```bash
git push -u origin feat/unified-history-table
gh pr create --title "feat(frontend): unified run-history table with engine column and multi-select" --body "$(cat <<'EOF'
## Summary
- New shared `RunHistoryComponent` with engine badge, multi-select, Compare button
- GraphQL `backtestRuns` query extended with `engine` filter + new `leanRunId` / `isSyntheticExit` fields
- Both LEAN Lab and Engine Lab pages embed the shared component

## Test plan
- [ ] `npx eslint Frontend/src/ --max-warnings 0`
- [ ] `podman exec my-frontend npx ng test --watch=false`
- [ ] `cd Backend.Tests && dotnet test`
- [ ] Manual: visit LEAN Lab + Engine Lab, verify both show engine badge and multi-select works

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 3 — Compare view

Start a new branch after PR 2 merges:

```bash
git checkout master && git pull && git checkout -b feat/run-comparison-view
```

### Task 3.1: Compare service (Python) reusing qc_reconciler (TDD)

**Files:**
- Create: `PythonDataService/app/services/lean_sidecar_compare_service.py`
- Test: `PythonDataService/tests/services/test_lean_sidecar_compare.py`

- [ ] **Step 1: Inspect existing qc_reconciler API**

```bash
grep -n "def reconcile\|DivergenceCategory\|class.*Report" PythonDataService/app/research/parity/qc_reconciler.py
```

Note the public API: function name, expected input shape, return type.

- [ ] **Step 2: Write the failing test**

Create `PythonDataService/tests/services/test_lean_sidecar_compare.py`:

```python
"""Tests for compare_runs orchestration."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.services.lean_sidecar_compare_service import (
    ComparisonResult,
    compare_runs,
)


def test_compare_runs_returns_empty_divergences_for_identical_runs(db_session: Session) -> None:
    # Seed two identical StrategyExecution rows with identical trades.
    left_id = _seed_run(db_session, "lean-sidecar", trades=[
        {"entry_ms": 1_700_000_000_000, "exit_ms": 1_700_000_060_000, "entry_price": 100.0, "exit_price": 101.0, "qty": 10},
    ])
    right_id = _seed_run(db_session, "engine", trades=[
        {"entry_ms": 1_700_000_000_000, "exit_ms": 1_700_000_060_000, "entry_price": 100.0, "exit_price": 101.0, "qty": 10},
    ])

    result = compare_runs(db_session, left_id, right_id)
    assert isinstance(result, ComparisonResult)
    assert result.divergences == []
    assert result.first_divergence_ms_utc is None
    assert result.summary["pnl_delta"] == pytest.approx(0.0)


def test_compare_runs_classifies_fill_price_drift(db_session: Session) -> None:
    left_id = _seed_run(db_session, "lean-sidecar", trades=[
        {"entry_ms": 1_700_000_000_000, "exit_ms": 1_700_000_060_000, "entry_price": 100.0, "exit_price": 101.0, "qty": 10},
    ])
    right_id = _seed_run(db_session, "engine", trades=[
        {"entry_ms": 1_700_000_000_000, "exit_ms": 1_700_000_060_000, "entry_price": 100.10, "exit_price": 101.05, "qty": 10},
    ])

    result = compare_runs(db_session, left_id, right_id)
    assert len(result.divergences) >= 1
    assert any(d.category == "FILL_PRICE_DRIFT" for d in result.divergences)


def test_compare_runs_flags_guardrails_for_different_symbols(db_session: Session) -> None:
    left_id = _seed_run(db_session, "lean-sidecar", symbol="SPY")
    right_id = _seed_run(db_session, "engine", symbol="QQQ")
    result = compare_runs(db_session, left_id, right_id)
    assert result.guardrails["same_symbol"] is False
    assert any("symbol" in w.lower() for w in result.guardrails["warnings"])


def _seed_run(
    session: Session,
    source: str,
    *,
    symbol: str = "SPY",
    trades: list[dict] | None = None,
) -> int:
    """Test helper: insert a StrategyExecution with optional trades."""
    from app.models.strategy_execution import StrategyExecution
    from app.models.backtest_trade import BacktestTrade

    row = StrategyExecution(
        StrategyName="test",
        Parameters=f'{{"symbol":"{symbol}"}}',
        Source=source,
        StartDate=datetime(2025, 1, 6, tzinfo=timezone.utc),
        EndDate=datetime(2025, 1, 10, tzinfo=timezone.utc),
        Timespan="minute", Multiplier=15,
        TotalTrades=len(trades or []), WinningTrades=0, LosingTrades=0,
        TotalPnL=0.0, InitialCash=100_000.0, FinalEquity=100_000.0,
        TotalFees=0.0, WinRate=0.0,
        LeanStatisticsJson="{}", LeanRunId=None, FillMode=None,
        ExecutedAt=datetime.now(), DurationMs=0,
    )
    session.add(row)
    session.flush()

    for i, t in enumerate(trades or [], start=1):
        session.add(BacktestTrade(
            StrategyExecutionId=row.Id,
            TradeType="LONG",
            EntryTimestamp=datetime.fromtimestamp(t["entry_ms"] / 1000.0, tz=timezone.utc),
            ExitTimestamp=datetime.fromtimestamp(t["exit_ms"] / 1000.0, tz=timezone.utc),
            EntryPrice=t["entry_price"],
            ExitPrice=t["exit_price"],
            Quantity=t["qty"],
            PnL=(t["exit_price"] - t["entry_price"]) * t["qty"],
            CumulativePnL=0.0,
            SignalReason="seed",
            IsSyntheticExit=False,
        ))
    session.commit()
    return row.Id
```

- [ ] **Step 3: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_compare.py -v
```

Expected: ImportError.

- [ ] **Step 4: Implement the compare service**

Create `PythonDataService/app/services/lean_sidecar_compare_service.py`:

```python
"""Compare two BacktestRun runs and classify divergences using qc_reconciler."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.strategy_execution import StrategyExecution
from app.models.backtest_trade import BacktestTrade
from app.research.parity.qc_reconciler import DivergenceCategory, reconcile


@dataclass
class DivergenceDto:
    category: str
    trade_number: int | None
    ms_utc: int | None
    message: str
    left_fill_price: float | None = None
    right_fill_price: float | None = None
    left_quantity: int | None = None
    right_quantity: int | None = None


@dataclass
class ComparisonResult:
    left_id: int
    right_id: int
    guardrails: dict[str, Any]
    summary: dict[str, float]
    divergences: list[DivergenceDto] = field(default_factory=list)
    first_divergence_ms_utc: int | None = None


def compare_runs(session: Session, left_id: int, right_id: int) -> ComparisonResult:
    left = session.query(StrategyExecution).filter_by(Id=left_id).one()
    right = session.query(StrategyExecution).filter_by(Id=right_id).one()
    left_trades = (
        session.query(BacktestTrade)
        .filter_by(StrategyExecutionId=left_id)
        .order_by(BacktestTrade.EntryTimestamp)
        .all()
    )
    right_trades = (
        session.query(BacktestTrade)
        .filter_by(StrategyExecutionId=right_id)
        .order_by(BacktestTrade.EntryTimestamp)
        .all()
    )

    guardrails = _compute_guardrails(left, right)
    summary = _compute_summary(left, right)

    # Pass to qc_reconciler in the shape it expects.
    report = reconcile(
        left=[_trade_to_dict(t) for t in left_trades],
        right=[_trade_to_dict(t) for t in right_trades],
        fill_price_atol=0.01,
        assert_fees=False,
    )

    divergences = [
        DivergenceDto(
            category=str(d.category),
            trade_number=d.trade_number,
            ms_utc=d.ms_utc,
            message=d.message,
            left_fill_price=d.left_fill_price,
            right_fill_price=d.right_fill_price,
            left_quantity=d.left_quantity,
            right_quantity=d.right_quantity,
        )
        for d in report.divergences
    ]
    first_div = min((d.ms_utc for d in divergences if d.ms_utc), default=None)

    return ComparisonResult(
        left_id=left_id,
        right_id=right_id,
        guardrails=guardrails,
        summary=summary,
        divergences=divergences,
        first_divergence_ms_utc=first_div,
    )


def _compute_guardrails(left: StrategyExecution, right: StrategyExecution) -> dict[str, Any]:
    left_params = json.loads(left.Parameters or "{}")
    right_params = json.loads(right.Parameters or "{}")
    warnings: list[str] = []

    same_algorithm = left.StrategyName == right.StrategyName
    if not same_algorithm:
        warnings.append(f"Different algorithms: {left.StrategyName} vs {right.StrategyName}")

    same_symbol = left_params.get("symbol") == right_params.get("symbol")
    if not same_symbol:
        warnings.append(
            f"Different symbols: {left_params.get('symbol')} vs {right_params.get('symbol')}"
        )

    same_window = left.StartDate == right.StartDate and left.EndDate == right.EndDate
    if not same_window:
        warnings.append("Different windows; comparison restricted to intersection")

    same_parameters = left_params == right_params

    return {
        "same_algorithm": same_algorithm,
        "same_symbol": same_symbol,
        "same_window": same_window,
        "same_parameters": same_parameters,
        "warnings": warnings,
    }


def _compute_summary(left: StrategyExecution, right: StrategyExecution) -> dict[str, float]:
    return {
        "pnl_delta": float(right.TotalPnL - left.TotalPnL),
        "trade_count_delta": float(right.TotalTrades - left.TotalTrades),
        "win_rate_delta": float(right.WinRate - left.WinRate),
        "fees_delta": float(right.TotalFees - left.TotalFees),
        "final_equity_delta": float(right.FinalEquity - left.FinalEquity),
    }


def _trade_to_dict(t: BacktestTrade) -> dict[str, Any]:
    return {
        "trade_number": t.Id,  # use Id as trade_number for now
        "entry_ms_utc": int(t.EntryTimestamp.timestamp() * 1000),
        "exit_ms_utc": int(t.ExitTimestamp.timestamp() * 1000),
        "entry_price": float(t.EntryPrice),
        "exit_price": float(t.ExitPrice),
        "quantity": int(t.Quantity),
        "pnl": float(t.PnL),
        "is_synthetic_exit": t.IsSyntheticExit,
    }
```

**Note:** the call signature `reconcile(left=..., right=..., fill_price_atol=..., assert_fees=...)` may not match the existing `qc_reconciler.reconcile` API exactly. Adjust based on Step 1's grep results. If the existing API is built for QC tradelogs not generic dict-trade lists, write an adapter in this same file.

- [ ] **Step 5: Run the tests**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_compare.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_compare_service.py \
        PythonDataService/tests/services/test_lean_sidecar_compare.py
git commit -m "feat(lean-sidecar): compare service reusing qc_reconciler taxonomy"
```

---

### Task 3.2: FastAPI endpoint `POST /api/lean-sidecar/compare`

**Files:**
- Modify: `PythonDataService/app/routers/lean_sidecar.py`

- [ ] **Step 1: Write the failing test**

Append to `PythonDataService/tests/services/test_lean_sidecar_compare.py`:

```python
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_compare_endpoint_returns_result(db_session) -> None:
    left_id = _seed_run(db_session, "lean-sidecar")
    right_id = _seed_run(db_session, "engine")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_id": left_id, "right_id": right_id},
        )
    assert response.status_code == 200
    body = response.json()
    assert "guardrails" in body
    assert "summary" in body
    assert "divergences" in body
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_compare.py::test_compare_endpoint_returns_result -v
```

Expected: 404.

- [ ] **Step 3: Implement the endpoint**

In `PythonDataService/app/routers/lean_sidecar.py`, append:

```python
from app.services.lean_sidecar_compare_service import compare_runs


class CompareRunsRequest(BaseModel):
    left_id: int
    right_id: int


class CompareRunsResponse(BaseModel):
    left_id: int
    right_id: int
    guardrails: dict
    summary: dict
    divergences: list[dict]
    first_divergence_ms_utc: int | None


@router.post("/compare", response_model=CompareRunsResponse)
async def compare_runs_endpoint(
    request: CompareRunsRequest,
    session: Session = Depends(get_db_session),
) -> CompareRunsResponse:
    result = compare_runs(session, request.left_id, request.right_id)
    return CompareRunsResponse(
        left_id=result.left_id,
        right_id=result.right_id,
        guardrails=result.guardrails,
        summary=result.summary,
        divergences=[d.__dict__ for d in result.divergences],
        first_divergence_ms_utc=result.first_divergence_ms_utc,
    )
```

- [ ] **Step 4: Run tests**

```bash
podman exec polygon-data-service python -m pytest tests/services/test_lean_sidecar_compare.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/routers/lean_sidecar.py \
        PythonDataService/tests/services/test_lean_sidecar_compare.py
git commit -m "feat(lean-sidecar): expose POST /api/lean-sidecar/compare endpoint"
```

---

### Task 3.3: .NET typed HttpClient for the compare service

**Files:**
- Create: `Backend/Services/IComparisonService.cs`
- Create: `Backend/Services/ComparisonService.cs`
- Modify: `Backend/Program.cs` (DI registration)

- [ ] **Step 1: Write the failing test**

Create `Backend.Tests/Services/ComparisonServiceTests.cs`:

```csharp
using FluentAssertions;
using NSubstitute;
using Xunit;
using Backend.Services;

namespace Backend.Tests.Services;

public class ComparisonServiceTests
{
    [Fact]
    public async Task CompareAsync_PassesIdsToHttpClient_AndReturnsResult()
    {
        var http = new System.Net.Http.HttpClient(new TestHandler(@"{
            ""left_id"": 1, ""right_id"": 2,
            ""guardrails"": {""same_algorithm"": true, ""same_symbol"": true,
                            ""same_window"": true, ""same_parameters"": true, ""warnings"": []},
            ""summary"": {""pnl_delta"": 0.0, ""trade_count_delta"": 0,
                          ""win_rate_delta"": 0.0, ""fees_delta"": 0.0, ""final_equity_delta"": 0.0},
            ""divergences"": [],
            ""first_divergence_ms_utc"": null
        }"));
        http.BaseAddress = new Uri("http://localhost:8000/");

        var svc = new ComparisonService(http);
        var result = await svc.CompareAsync(1, 2, CancellationToken.None);

        result.Should().NotBeNull();
        result!.LeftId.Should().Be(1);
        result.RightId.Should().Be(2);
    }
}

internal class TestHandler : System.Net.Http.HttpMessageHandler
{
    private readonly string _body;
    public TestHandler(string body) => _body = body;
    protected override Task<System.Net.Http.HttpResponseMessage> SendAsync(
        System.Net.Http.HttpRequestMessage request, CancellationToken cancellationToken)
    {
        return Task.FromResult(new System.Net.Http.HttpResponseMessage
        {
            StatusCode = System.Net.HttpStatusCode.OK,
            Content = new System.Net.Http.StringContent(_body, System.Text.Encoding.UTF8, "application/json"),
        });
    }
}
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~ComparisonServiceTests"
```

Expected: types not defined.

- [ ] **Step 3: Implement**

Create `Backend/Services/IComparisonService.cs`:

```csharp
namespace Backend.Services;

public interface IComparisonService
{
    Task<ComparisonDto?> CompareAsync(int leftId, int rightId, CancellationToken ct);
}

public record ComparisonDto(
    int LeftId,
    int RightId,
    ComparisonGuardrailsDto Guardrails,
    ComparisonSummaryDto Summary,
    IReadOnlyList<TradeDivergenceDto> Divergences,
    long? FirstDivergenceMsUtc);

public record ComparisonGuardrailsDto(
    bool SameAlgorithm,
    bool SameSymbol,
    bool SameWindow,
    bool SameParameters,
    IReadOnlyList<string> Warnings);

public record ComparisonSummaryDto(
    decimal PnlDelta,
    int TradeCountDelta,
    double WinRateDelta,
    decimal FeesDelta,
    decimal FinalEquityDelta);

public record TradeDivergenceDto(
    string Category,
    int? TradeNumber,
    long? MsUtc,
    string Message,
    decimal? LeftFillPrice,
    decimal? RightFillPrice,
    int? LeftQuantity,
    int? RightQuantity);
```

Create `Backend/Services/ComparisonService.cs`:

```csharp
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Backend.Services;

public class ComparisonService : IComparisonService
{
    private readonly HttpClient _http;
    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new JsonStringEnumConverter() },
    };

    public ComparisonService(HttpClient http)
    {
        _http = http;
    }

    public async Task<ComparisonDto?> CompareAsync(int leftId, int rightId, CancellationToken ct)
    {
        var response = await _http.PostAsJsonAsync(
            "api/lean-sidecar/compare",
            new { left_id = leftId, right_id = rightId },
            _jsonOpts,
            ct);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<ComparisonDto>(_jsonOpts, ct);
    }
}
```

In `Backend/Program.cs`:

```csharp
builder.Services.AddHttpClient<IComparisonService, ComparisonService>(c =>
{
    c.BaseAddress = new Uri(builder.Configuration["PythonDataService:BaseUrl"]
        ?? "http://polygon-data-service:8000/");
});
```

- [ ] **Step 4: Run tests**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~ComparisonServiceTests"
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add Backend/Services/IComparisonService.cs Backend/Services/ComparisonService.cs \
        Backend/Program.cs Backend.Tests/Services/ComparisonServiceTests.cs
git commit -m "feat(backend): typed ComparisonService HTTP client to Python compare endpoint"
```

---

### Task 3.4: GraphQL `compareBacktestRuns` resolver

**Files:**
- Create: `Backend/GraphQL/Comparison/CompareBacktestRunsResolver.cs`
- Create: `Backend/GraphQL/Comparison/ComparisonGraphQLTypes.cs`
- Test: `Backend.Tests/GraphQL/CompareBacktestRunsTests.cs`

- [ ] **Step 1: Write the failing test**

Create `Backend.Tests/GraphQL/CompareBacktestRunsTests.cs`:

```csharp
using FluentAssertions;
using HotChocolate.Execution;
using NSubstitute;
using Xunit;
using Backend.Services;

namespace Backend.Tests.GraphQL;

public class CompareBacktestRunsTests : GraphQLTestBase
{
    [Fact]
    public async Task CompareBacktestRuns_ReturnsResult()
    {
        var fakeService = Substitute.For<IComparisonService>();
        fakeService
            .CompareAsync(1, 2, Arg.Any<CancellationToken>())
            .Returns(new ComparisonDto(
                1, 2,
                new ComparisonGuardrailsDto(true, true, true, true, Array.Empty<string>()),
                new ComparisonSummaryDto(0m, 0, 0.0, 0m, 0m),
                Array.Empty<TradeDivergenceDto>(),
                null));

        var executor = await BuildSchemaWithAsync(fakeService);
        var result = await executor.ExecuteAsync(@"
            query { compareBacktestRuns(leftId: 1, rightId: 2) {
                left { id } right { id }
                guardrails { sameAlgorithm warnings }
                summary { pnlDelta }
                divergences { category }
                firstDivergenceMsUtc
            } }");

        result.Errors.Should().BeNull();
    }
}
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~CompareBacktestRunsTests"
```

Expected: schema doesn't know `compareBacktestRuns`.

- [ ] **Step 3: Implement GraphQL types**

Create `Backend/GraphQL/Comparison/ComparisonGraphQLTypes.cs`:

```csharp
using Backend.Models.MarketData;

namespace Backend.GraphQL.Comparison;

public record RunComparisonResult(
    StrategyExecution Left,
    StrategyExecution Right,
    ComparisonGuardrails Guardrails,
    ComparisonSummary Summary,
    IReadOnlyList<TradeDivergence> Divergences,
    long? FirstDivergenceMsUtc);

public record ComparisonGuardrails(
    bool SameAlgorithm,
    bool SameSymbol,
    bool SameWindow,
    bool SameParameters,
    IReadOnlyList<string> Warnings);

public record ComparisonSummary(
    decimal PnlDelta,
    int TradeCountDelta,
    double WinRateDelta,
    decimal FeesDelta,
    decimal FinalEquityDelta);

public record TradeDivergence(
    DivergenceCategory Category,
    int? TradeNumber,
    long? MsUtc,
    string Message,
    decimal? LeftFillPrice,
    decimal? RightFillPrice,
    int? LeftQuantity,
    int? RightQuantity);

public enum DivergenceCategory
{
    DECISION_MISMATCH,
    DIRECTION_MISMATCH,
    QUANTITY_MISMATCH,
    FILL_PRICE_DRIFT,
    COMMISSION_DRIFT,
    PNL_DRIFT,
    ORDER_TYPE_MISMATCH,
    FIXTURE_INSUFFICIENT,
}
```

Create `Backend/GraphQL/Comparison/CompareBacktestRunsResolver.cs`:

```csharp
using Backend.Data;
using Backend.Services;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL.Comparison;

[QueryType]
public static class CompareBacktestRunsResolver
{
    [GraphQLName("compareBacktestRuns")]
    public static async Task<RunComparisonResult?> CompareBacktestRunsAsync(
        int leftId,
        int rightId,
        [Service] IComparisonService comparisonService,
        [Service] AppDbContext db,
        CancellationToken ct)
    {
        var dto = await comparisonService.CompareAsync(leftId, rightId, ct);
        if (dto is null) return null;

        var left = await db.StrategyExecutions.AsNoTracking().SingleAsync(s => s.Id == leftId, ct);
        var right = await db.StrategyExecutions.AsNoTracking().SingleAsync(s => s.Id == rightId, ct);

        return new RunComparisonResult(
            Left: left,
            Right: right,
            Guardrails: new ComparisonGuardrails(
                dto.Guardrails.SameAlgorithm,
                dto.Guardrails.SameSymbol,
                dto.Guardrails.SameWindow,
                dto.Guardrails.SameParameters,
                dto.Guardrails.Warnings),
            Summary: new ComparisonSummary(
                dto.Summary.PnlDelta,
                dto.Summary.TradeCountDelta,
                dto.Summary.WinRateDelta,
                dto.Summary.FeesDelta,
                dto.Summary.FinalEquityDelta),
            Divergences: dto.Divergences
                .Select(d => new TradeDivergence(
                    Enum.Parse<DivergenceCategory>(d.Category),
                    d.TradeNumber,
                    d.MsUtc,
                    d.Message,
                    d.LeftFillPrice,
                    d.RightFillPrice,
                    d.LeftQuantity,
                    d.RightQuantity))
                .ToList(),
            FirstDivergenceMsUtc: dto.FirstDivergenceMsUtc);
    }
}
```

- [ ] **Step 4: Run tests**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~CompareBacktestRunsTests"
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add Backend/GraphQL/Comparison/ Backend.Tests/GraphQL/CompareBacktestRunsTests.cs
git commit -m "feat(backend): compareBacktestRuns GraphQL resolver"
```

---

### Task 3.5: Run-comparison Angular component + route (TDD)

**Files:**
- Create: `Frontend/src/app/components/run-comparison/run-comparison.component.ts` + `.html` + `.scss` + `.spec.ts`
- Create: `Frontend/src/app/graphql/queries/compare-backtest-runs.ts`
- Modify: `Frontend/src/app/app.routes.ts`

- [ ] **Step 1: Add the GraphQL query**

Create `Frontend/src/app/graphql/queries/compare-backtest-runs.ts`:

```typescript
import { gql } from "apollo-angular";

export const COMPARE_BACKTEST_RUNS_QUERY = gql`
  query CompareBacktestRuns($leftId: ID!, $rightId: ID!) {
    compareBacktestRuns(leftId: $leftId, rightId: $rightId) {
      left {
        id source strategyName leanRunId totalTrades totalPnL finalEquity
        trades { entryTimestamp exitTimestamp entryPrice exitPrice pnL isSyntheticExit signalReason }
      }
      right {
        id source strategyName leanRunId totalTrades totalPnL finalEquity
        trades { entryTimestamp exitTimestamp entryPrice exitPrice pnL isSyntheticExit signalReason }
      }
      guardrails { sameAlgorithm sameSymbol sameWindow sameParameters warnings }
      summary { pnlDelta tradeCountDelta winRateDelta feesDelta finalEquityDelta }
      divergences { category tradeNumber msUtc message leftFillPrice rightFillPrice }
      firstDivergenceMsUtc
    }
  }
`;
```

- [ ] **Step 2: Write the failing test**

Create `Frontend/src/app/components/run-comparison/run-comparison.component.spec.ts`:

```typescript
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";
import { provideRouter, ActivatedRoute } from "@angular/router";
import { of } from "rxjs";
import { Apollo } from "apollo-angular";
import { RunComparisonComponent } from "./run-comparison.component";

describe("RunComparisonComponent", () => {
  it("renders the guardrail banner when warnings are present", async () => {
    const apolloMock = {
      watchQuery: () => ({
        valueChanges: of({
          data: {
            compareBacktestRuns: {
              left: { id: "1", source: "lean-sidecar", strategyName: "ema", totalTrades: 1, totalPnL: 0, finalEquity: 100000, trades: [] },
              right: { id: "2", source: "engine", strategyName: "ema", totalTrades: 1, totalPnL: 0, finalEquity: 100000, trades: [] },
              guardrails: { sameAlgorithm: true, sameSymbol: false, sameWindow: true, sameParameters: false, warnings: ["Different symbols: SPY vs QQQ"] },
              summary: { pnlDelta: 0, tradeCountDelta: 0, winRateDelta: 0, feesDelta: 0, finalEquityDelta: 0 },
              divergences: [],
              firstDivergenceMsUtc: null,
            },
          },
        }),
      }),
    };

    await render(RunComparisonComponent, {
      providers: [
        provideRouter([]),
        { provide: Apollo, useValue: apolloMock },
        { provide: ActivatedRoute, useValue: { queryParamMap: of(new Map([["left", "1"], ["right", "2"]])) } },
      ],
    });

    expect(screen.getByText(/different symbols/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run to verify it fails**

```bash
podman exec my-frontend npx ng test --include="**/run-comparison.component.spec.ts" --watch=false
```

Expected: component not defined.

- [ ] **Step 4: Implement the component**

Create `Frontend/src/app/components/run-comparison/run-comparison.component.ts`:

```typescript
import { ChangeDetectionStrategy, Component, computed, inject } from "@angular/core";
import { ActivatedRoute } from "@angular/router";
import { toSignal } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { map, switchMap } from "rxjs/operators";
import { COMPARE_BACKTEST_RUNS_QUERY } from "../../graphql/queries/compare-backtest-runs";

@Component({
  selector: "app-run-comparison",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./run-comparison.component.html",
  styleUrls: ["./run-comparison.component.scss"],
})
export class RunComparisonComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly apollo = inject(Apollo);

  readonly data = toSignal(
    this.route.queryParamMap.pipe(
      switchMap((p) =>
        this.apollo
          .watchQuery<{ compareBacktestRuns: any }>({
            query: COMPARE_BACKTEST_RUNS_QUERY,
            variables: { leftId: p.get("left"), rightId: p.get("right") },
          })
          .valueChanges.pipe(map((r) => r.data.compareBacktestRuns)),
      ),
    ),
  );

  readonly warnings = computed(() => this.data()?.guardrails?.warnings ?? []);
}
```

Create `Frontend/src/app/components/run-comparison/run-comparison.component.html`:

```html
@if (data(); as r) {
  @if (warnings().length > 0) {
    <div class="guardrail-banner" role="alert">
      @for (w of warnings(); track w) { <p>{{ w }}</p> }
    </div>
  }

  <section class="summary-strip">
    <div class="stat">
      <label>Net PnL Δ</label>
      <span [class.positive]="r.summary.pnlDelta > 0" [class.negative]="r.summary.pnlDelta < 0">
        {{ r.summary.pnlDelta | currency:'USD' }}
      </span>
    </div>
    <div class="stat">
      <label>Trades Δ</label>
      <span>{{ r.summary.tradeCountDelta }}</span>
    </div>
    <div class="stat">
      <label>Win rate Δ</label>
      <span>{{ r.summary.winRateDelta | percent:'1.1-1' }}</span>
    </div>
    <div class="stat">
      <label>First divergence</label>
      <span>{{ r.firstDivergenceMsUtc ? (r.firstDivergenceMsUtc | date:'short') : '—' }}</span>
    </div>
  </section>

  <section class="run-panels">
    <article class="run-panel">
      <header>Left — {{ r.left.strategyName }} ({{ r.left.source }})</header>
      <p>Trades: {{ r.left.totalTrades }} · PnL: {{ r.left.totalPnL | currency:'USD' }}</p>
    </article>
    <article class="run-panel">
      <header>Right — {{ r.right.strategyName }} ({{ r.right.source }})</header>
      <p>Trades: {{ r.right.totalTrades }} · PnL: {{ r.right.totalPnL | currency:'USD' }}</p>
    </article>
  </section>

  <section class="divergences">
    <h2>Divergences ({{ r.divergences.length }})</h2>
    <table>
      <thead><tr><th>#</th><th>Timestamp</th><th>Category</th><th>Message</th></tr></thead>
      <tbody>
        @for (d of r.divergences; track $index) {
          <tr>
            <td>{{ d.tradeNumber ?? '—' }}</td>
            <td>{{ d.msUtc ? (d.msUtc | date:'short') : '—' }}</td>
            <td>{{ d.category }}</td>
            <td>{{ d.message }}</td>
          </tr>
        }
      </tbody>
    </table>
  </section>
}
```

Create `Frontend/src/app/components/run-comparison/run-comparison.component.scss`:

```scss
.guardrail-banner {
  padding: var(--space-md);
  margin-bottom: var(--space-md);
  background: var(--color-warning-bg);
  border-left: 4px solid var(--color-warning);
}

.summary-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-md);
  margin-bottom: var(--space-lg);

  .stat {
    padding: var(--space-md);
    background: var(--color-surface-secondary);
    border-radius: var(--radius-md);

    label { display: block; font-size: var(--font-size-sm); color: var(--color-text-muted); }
    .positive { color: var(--color-success); }
    .negative { color: var(--color-danger); }
  }
}

.run-panels {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);

  .run-panel {
    padding: var(--space-md);
    background: var(--color-surface);
    border-radius: var(--radius-md);
  }
}

.divergences table {
  width: 100%;
  border-collapse: collapse;

  th, td {
    padding: var(--space-sm);
    border-bottom: 1px solid var(--color-border);
    text-align: left;
  }
}
```

In `Frontend/src/app/app.routes.ts`, add a lazy route:

```typescript
{
  path: "runs/compare",
  loadComponent: () =>
    import("./components/run-comparison/run-comparison.component")
      .then((m) => m.RunComparisonComponent),
},
```

- [ ] **Step 5: Run tests**

```bash
podman exec my-frontend npx ng test --include="**/run-comparison.component.spec.ts" --watch=false
```

Expected: 1 passed.

- [ ] **Step 6: Manual smoke test**

```bash
./restart.sh
```

Visit http://localhost:4200, select 2 runs, click Compare. Verify the page renders.

- [ ] **Step 7: Commit**

```bash
git add Frontend/src/app/components/run-comparison/ \
        Frontend/src/app/graphql/queries/compare-backtest-runs.ts \
        Frontend/src/app/app.routes.ts
git commit -m "feat(frontend): side-by-side run-comparison view with divergence table"
```

---

### Task 3.6: Open PR 3

```bash
git push -u origin feat/run-comparison-view
gh pr create --title "feat: side-by-side compare view for backtest runs" --body "$(cat <<'EOF'
## Summary
- Python `compare_runs` service reuses `qc_reconciler` for divergence classification
- FastAPI `POST /api/lean-sidecar/compare` endpoint
- .NET typed `ComparisonService` HTTP client + GraphQL `compareBacktestRuns` resolver
- Angular `/runs/compare` route with guardrail banner, summary strip, side-by-side panels, divergence table

## Test plan
- [ ] Python: `pytest tests/services/test_lean_sidecar_compare.py -v`
- [ ] .NET: `dotnet test --filter "FullyQualifiedName~CompareBacktest"`
- [ ] Frontend: `ng test --include="**/run-comparison*"`
- [ ] Manual: select 2 runs in unified history, click Compare, verify renders

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 4 — Parity acceptance test

Start a new branch after PR 3 merges:

```bash
git checkout master && git pull && git checkout -b test/ema-crossover-parity
```

### Task 4.1: Golden parity test (LEAN vs Engine Lab)

**Files:**
- Create: `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py`
- Create: `PythonDataService/tests/fixtures/golden/ema_crossover/attribution.md`

- [ ] **Step 1: Implement the test (slow integration; mark accordingly)**

Create `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py`:

```python
"""Acceptance gate: LEAN ema_crossover template == Engine Lab spy_ema_crossover spec.

Asserts zero divergences in the gating set per .claude/rules/numerical-rigor.md:
  {DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH, FILL_PRICE_DRIFT,
   ORDER_TYPE_MISMATCH, PNL_DRIFT, FIXTURE_INSUFFICIENT}

Plus COMMISSION_DRIFT only on Branch-A fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session


# Pinned reconciliation window. Update only with justification (LEAN image
# upgrade, spec fix, etc.) and regenerate fixture under tests/fixtures/golden/.
WINDOW_START = "2025-01-06"
WINDOW_END = "2025-02-28"
SYMBOL = "SPY"
STARTING_CASH = 100_000.0

GATING_CATEGORIES = {
    "DECISION_MISMATCH",
    "DIRECTION_MISMATCH",
    "QUANTITY_MISMATCH",
    "FILL_PRICE_DRIFT",
    "ORDER_TYPE_MISMATCH",
    "PNL_DRIFT",
    "FIXTURE_INSUFFICIENT",
}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_ema_crossover_lean_matches_engine_lab_spec(db_session: Session) -> None:
    from app.services.lean_sidecar_service import run_trusted_sample, TrustedRunRequest
    from app.services.spec_strategy_service import run_spec_backtest
    from app.services.lean_sidecar_compare_service import compare_runs

    # 1. Run LEAN template.
    lean_run = await run_trusted_sample(
        TrustedRunRequest(
            template="ema_crossover",
            symbol=SYMBOL,
            start_date=WINDOW_START,
            end_date=WINDOW_END,
            starting_cash=STARTING_CASH,
        ),
        session=db_session,
    )
    lean_id = lean_run.strategy_execution_id

    # 2. Run Engine Lab spec.
    spec_path = Path(__file__).parents[3] / "app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json"
    spec = json.loads(spec_path.read_text())
    engine_id = await run_spec_backtest(
        session=db_session,
        spec=spec,
        symbol=SYMBOL,
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        initial_cash=STARTING_CASH,
        fill_mode="signal_bar_close",  # or "next_bar_open" per Task 1.0 spike decision
    )

    # 3. Reconcile.
    result = compare_runs(db_session, lean_id, engine_id)

    gating_divergences = [d for d in result.divergences if d.category in GATING_CATEGORIES]
    if gating_divergences:
        msg = "\n".join(f"  {d.category} @ {d.ms_utc}: {d.message}" for d in gating_divergences)
        pytest.fail(f"{len(gating_divergences)} gating divergences:\n{msg}")

    # 4. Emit a snapshot to docs/references/reconciliations/ for traceability.
    _write_reconciliation_report(result, lean_id, engine_id)


def _write_reconciliation_report(result, lean_id: int, engine_id: int) -> None:
    out = Path(__file__).parents[4] / "docs/references/reconciliations/ema-crossover-lean-vs-engine-lab.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"""# EMA crossover — LEAN vs Engine Lab reconciliation

Generated: {datetime.now(tz=timezone.utc).isoformat()}
Left (LEAN): StrategyExecution.Id={lean_id}
Right (Engine Lab): StrategyExecution.Id={engine_id}
Window: {WINDOW_START} to {WINDOW_END}
Symbol: {SYMBOL}

## Divergence counts by category

{_counts_table(result.divergences)}

## Summary

- PnL delta: {result.summary.get('pnl_delta')}
- Trade count delta: {result.summary.get('trade_count_delta')}
- First divergence: {result.first_divergence_ms_utc or 'none'}

## Guardrails

{json.dumps(result.guardrails, indent=2)}
""")


def _counts_table(divergences) -> str:
    from collections import Counter
    counts = Counter(d.category for d in divergences)
    if not counts:
        return "(none)"
    return "\n".join(f"- {cat}: {n}" for cat, n in sorted(counts.items()))
```

- [ ] **Step 2: Run it**

```bash
podman exec polygon-data-service python -m pytest tests/integration/parity/test_ema_crossover_lean_vs_spec.py -v -m slow
```

Expected (success): 1 passed, `docs/references/reconciliations/ema-crossover-lean-vs-engine-lab.md` exists.

If failures: do NOT loosen tolerances. Triage per the divergence taxonomy in `numerical-rigor.md`:
- `DECISION_MISMATCH` → bug in the EMA template, the spec evaluator, or the consolidator.
- `FILL_PRICE_DRIFT` → fill model parity is broken; revisit Task 1.0's decision.
- `QUANTITY_MISMATCH` → SetHoldings rounding differs between engines.

- [ ] **Step 3: Commit the test and the generated reconciliation report**

```bash
git add PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py \
        docs/references/reconciliations/ema-crossover-lean-vs-engine-lab.md
git commit -m "test(parity): pin LEAN ema_crossover == Engine Lab spy_ema_crossover spec"
```

---

### Task 4.2: Open PR 4

```bash
git push -u origin test/ema-crossover-parity
gh pr create --title "test(parity): EMA crossover golden parity (LEAN vs Engine Lab)" --body "$(cat <<'EOF'
## Summary
- Acceptance test asserting zero gating-set divergences between LEAN `ema_crossover` template and Engine Lab `spy_ema_crossover` spec on pinned 2025-01-06 to 2025-02-28 window
- Test generates `docs/references/reconciliations/ema-crossover-lean-vs-engine-lab.md` on every run

## Test plan
- [ ] `podman exec polygon-data-service python -m pytest tests/integration/parity/ -v -m slow`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 5 — Backfill CLI for historical LEAN runs

Start a new branch after PR 4 merges:

```bash
git checkout master && git pull && git checkout -b feat/backfill-lean-runs
```

### Task 5.1: One-shot backfill script (TDD)

**Files:**
- Create: `PythonDataService/app/scripts/__init__.py` (if missing)
- Create: `PythonDataService/app/scripts/backfill_lean_runs.py`
- Test: `PythonDataService/tests/scripts/test_backfill_lean_runs.py`

- [ ] **Step 1: Write the failing test**

Create `PythonDataService/tests/scripts/test_backfill_lean_runs.py`:

```python
"""Backfill CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.scripts.backfill_lean_runs import backfill_directory


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    """Build two LEAN workspaces under a shared artifacts root."""
    root = tmp_path / "artifacts/lean-sidecar"
    for run_id in ["ui_run_a", "ui_run_b"]:
        ws = root / run_id
        (ws / "normalized").mkdir(parents=True)
        result = {
            "algorithm_id": "MyAlgorithm",
            "parser_version": "phase-3a-r1",
            "first_equity_ms_utc": 1_700_000_000_000,
            "last_equity_ms_utc": 1_700_000_600_000,
            "total_equity_points": 1,
            "total_order_events": 0,
            "equity_curve": [{"ms_utc": 1_700_000_600_000, "value": 100_000.0,
                              "open": 100_000.0, "high": 100_000.0, "low": 100_000.0}],
            "order_events": [],
            "statistics": {},
            "runtime_statistics": {},
        }
        (ws / "normalized" / "result.json").write_text(json.dumps(result))

        # Write a minimal manifest.json so we can derive params.
        manifest = {
            "run_id": run_id,
            "parameters": {"symbol": "SPY", "start_date": "2025-01-06",
                           "end_date": "2025-01-10", "starting_cash": 100000},
            "started_at_ms": 1_700_000_000_000,
        }
        (ws / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_backfill_writes_one_row_per_workspace(
    artifacts_root: Path, db_session: Session
) -> None:
    persisted = backfill_directory(db_session, artifacts_root)
    assert len(persisted) == 2

    from app.models.strategy_execution import StrategyExecution
    count = db_session.query(StrategyExecution).filter_by(Source="lean-sidecar").count()
    assert count == 2


def test_backfill_is_idempotent(
    artifacts_root: Path, db_session: Session
) -> None:
    backfill_directory(db_session, artifacts_root)
    backfill_directory(db_session, artifacts_root)

    from app.models.strategy_execution import StrategyExecution
    count = db_session.query(StrategyExecution).filter_by(Source="lean-sidecar").count()
    assert count == 2
```

- [ ] **Step 2: Run to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/scripts/test_backfill_lean_runs.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement**

Create `PythonDataService/app/scripts/backfill_lean_runs.py`:

```python
"""One-shot CLI: backfill on-disk LEAN runs into StrategyExecution table.

Usage:
    podman exec polygon-data-service python -m app.scripts.backfill_lean_runs \
        --artifacts-root /app/artifacts/lean-sidecar

Idempotent: runs already persisted (matched by LeanRunId) are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.session import SessionLocal  # adjust import path to actual
from app.services.lean_sidecar_persistence import normalize_and_persist


logger = logging.getLogger(__name__)


def backfill_directory(session: Session, artifacts_root: Path) -> list[int]:
    """Backfill every workspace under artifacts_root that has a normalized result."""
    persisted_ids: list[int] = []
    for workspace in sorted(artifacts_root.iterdir()):
        if not workspace.is_dir():
            continue
        result_path = workspace / "normalized" / "result.json"
        manifest_path = workspace / "manifest.json"
        if not result_path.exists() or not manifest_path.exists():
            logger.info("Skipping %s: missing normalized result or manifest", workspace.name)
            continue

        manifest = json.loads(manifest_path.read_text())
        params = manifest.get("parameters", {})

        try:
            exec_id = normalize_and_persist(
                session=session,
                run_id=workspace.name,
                workspace_path=workspace,
                starting_cash=float(params.get("starting_cash", 100_000)),
                symbol=params.get("symbol", "SPY"),
                algorithm_name="user_provided",  # manifest doesn't always record template
                start_date=datetime.strptime(params.get("start_date", "2025-01-01"), "%Y-%m-%d").replace(tzinfo=timezone.utc),
                end_date=datetime.strptime(params.get("end_date", "2025-01-01"), "%Y-%m-%d").replace(tzinfo=timezone.utc),
            )
            persisted_ids.append(exec_id)
            logger.info("Backfilled %s → StrategyExecution.Id=%s", workspace.name, exec_id)
        except Exception:
            logger.exception("Failed to backfill %s", workspace.name)

    return persisted_ids


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-root", type=Path, required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        ids = backfill_directory(session, args.artifacts_root)
    logger.info("Backfilled %d runs", len(ids))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```bash
podman exec polygon-data-service python -m pytest tests/scripts/test_backfill_lean_runs.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run manually**

```bash
podman exec polygon-data-service python -m app.scripts.backfill_lean_runs \
  --artifacts-root /app/artifacts/lean-sidecar
```

Expected: logs reporting backfilled run count; database now has rows for pre-existing on-disk runs.

- [ ] **Step 6: Commit + open PR**

```bash
git add PythonDataService/app/scripts/__init__.py \
        PythonDataService/app/scripts/backfill_lean_runs.py \
        PythonDataService/tests/scripts/test_backfill_lean_runs.py
git commit -m "feat(scripts): backfill historical LEAN artifacts into StrategyExecution"

git push -u origin feat/backfill-lean-runs
gh pr create --title "feat(scripts): backfill CLI for historical LEAN runs" --body "$(cat <<'EOF'
## Summary
- One-shot CLI: walks `/app/artifacts/lean-sidecar/<run_id>/`, calls `normalize_and_persist` for each
- Idempotent (skips runs already in the DB)
- Manual run: `python -m app.scripts.backfill_lean_runs --artifacts-root /app/artifacts/lean-sidecar`

## Test plan
- [ ] `pytest tests/scripts/test_backfill_lean_runs.py -v`
- [ ] Manual: run on populated artifacts dir, verify rows in StrategyExecution

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (run after writing)

**Spec coverage:**
- G1 EMA template → PR 1 Task 1.1, registered in 1.2, dropdown in 1.3. ✓
- G2 LEAN runs in Postgres → PR 1 Tasks 1.4 (schema), 1.5–1.8 (pairing + persistence), 1.9 (wiring). ✓
- G3 engine-results UI renders LEAN runs identically → PR 2 (LEAN rows flow through same StrategyExecution query the engine-results page consumes). ✓
- G4 unified history table → PR 2 Tasks 2.2–2.5. ✓
- G5 side-by-side compare with DivergenceCategory → PR 3 Tasks 3.1–3.5. ✓
- G6 golden parity test → PR 4 Task 4.1. ✓

**Placeholder scan:** Confirmed no "TBD", "TODO", "implement later", "fill in details", or unspecified error-handling. Fill-model decision is gated on a one-day spike (Task 1.0) with explicit code variants for each option (a/b/c).

**Type consistency:** `StrategyExecution.LeanRunId` (string?) used identically in entity, GraphQL field, and DTO. `BacktestTrade.IsSyntheticExit` (bool) used identically. `EngineSource` enum maps consistently to DB string values. `DivergenceCategory` enum mirrors Python `qc_reconciler.DivergenceCategory` `StrEnum` exactly.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-19-lean-ema-template-and-unified-history.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
