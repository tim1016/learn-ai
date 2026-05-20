# PR B — Unified Engine Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify `/engine` and `/lean-lab` behind one launch surface with a shared `DataPolicy` contract, unified run history, and a parity-compare view.

**Architecture:** Four phase-based sub-PRs. Phase 1+2 land the backend contract and persistence (no UI change). Phase 3 lands the GraphQL schema additions and unified history table. Phase 4 lands the compare endpoint + view. Phase 5+6 land the Engine dropdown, LEAN source editor, ruff lint endpoint, and retire `/lean-lab`. Each phase ships independently.

**Tech Stack:** Python 3.11 (FastAPI, Pydantic v2, pandas), .NET 10 (Hot Chocolate v15 GraphQL, EF Core 10, Postgres 16), Angular 21 (signals, standalone components, Vitest, Monaco/CodeMirror), ruff (lint endpoint).

**Spec:** `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md`

---

## File structure

This maps every file the plan creates or modifies. Files that change together are in the same phase.

### Phase 1 — DataPolicy contract (backend only)

- **Modify** `PythonDataService/app/lean_sidecar/manifest.py` — rename `DataPolicyManifest` → `DataPolicy`; keep alias with `DeprecationWarning`.
- **Create** `PythonDataService/app/lean_sidecar/data_policy.py` — re-exports `DataPolicy` and `BarsSpec` from `manifest.py`; declares the alias.
- **Modify** `PythonDataService/app/services/lean_sidecar_service.py` — `TrustedRunRequest` drops `symbol`, `bar_minutes`, `session`, `adjustment`; gains `data_policy: DataPolicy`. `_build_data_policy()` threads provider identity into the request's existing `data_policy`.
- **Modify** `PythonDataService/app/routers/lean_sidecar.py` — `TrustedRunRequestModel` accepts either legacy shape OR new `data_policy` block. Rejects mixed shape. Logs deprecation warning on legacy.
- **Modify** `PythonDataService/app/services/lean_sidecar_service.py` — `_assert_adjustment_vocabulary_consistent` widens its truth table.
- **Modify** `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` — pin `strategy_bars={minute, 15}` inside the template's DataPolicy default.
- **Create** `PythonDataService/tests/unit/lean_sidecar/test_data_policy.py` — JSON roundtrip, alias deprecation, vocab assertion truth table.
- **Create** `PythonDataService/tests/unit/lean_sidecar/test_bars_spec.py` — `{minute,60} != {hour,1}` v1 pin.
- **Modify** `PythonDataService/tests/lean_sidecar/test_manifest.py` — `RunManifest.data_policy` is `DataPolicy`; schema stays at 4.

### Phase 2 — Engine-side persistence

- **Create** `Backend/Migrations/<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.cs` + `.Designer.cs` — adds `DataPolicyJson`, `CommissionPerOrder`, `BrokeragePolicy` columns + symbol index.
- **Modify** `Backend/Models/MarketData/StrategyExecution.cs` — add three new properties.
- **Modify** `Backend/Models/Persistence/PersistEnginePayload.cs` (exact name to be confirmed via grep) — add `data_policy_json`, `commission_per_order`, `brokerage_policy` fields.
- **Modify** `Backend/Models/Persistence/PersistLeanPayload.cs` — same additions.
- **Modify** `Backend/Services/BacktestRunPersistenceService.cs` — write the new columns in both `PersistEngineAsync` and `PersistLeanAsync`. Python engine writes `brokerage_policy="algorithm_default"`.
- **Modify** `PythonDataService/app/routers/engine.py` — `EngineBacktestRequest` gains `data_policy: DataPolicy`. Synthesizes from legacy fields when omitted.
- **Modify** `PythonDataService/app/services/strategy_engine.py` — populate `data_policy` echo in `EngineBacktestResponse`.
- **Modify** `Backend/Controllers/BacktestRunsApi.cs` — accept the new payload shape, pass through.
- **Modify** `Frontend/src/app/components/lean-engine/lean-engine.component.ts` (around line 669-679) — engine request includes `data_policy` synthesized from current form fields.
- **Modify** `Frontend/src/app/services/lean-sidecar.service.ts` — `startTrustedRun` request includes `data_policy`.
- **Modify** `Frontend/src/app/components/lean-lab/lean-lab.component.ts` — submission includes `data_policy` (this page is retired in Phase 5; this is a one-cycle compatibility step).
- **Create** `PythonDataService/tests/integration/test_engine_persistence_data_policy.py`.

### Phase 3 — History surfaces

- **Create** `Backend/GraphQL/Types/EngineType.cs`, `BarsSpecType.cs`, `DataPolicyType.cs`.
- **Modify** `Backend/GraphQL/Resolvers/BacktestRunResolver.cs` — `BacktestRun.engine` derived from `Source`; `BacktestRun.dataPolicy` parsed from JSON; `backtestRuns(engine: Engine = null, ...)`.
- **Create or extend** `Backend/GraphQL/Mutations/BacktestRunMutation.cs` — `updateBacktestRunNotes(id, notes)`.
- **Modify** `Frontend/src/app/components/engine-lab-run-history/backtest-runs.query.ts` — query gains `engine`, `dataPolicy`, `commissionPerOrder`, `brokeragePolicy`; `engine` argument optional.
- **Modify** `Frontend/src/app/components/engine-lab-run-history/engine-lab-run-history.component.{ts,html,scss}` — Engine column, Engine filter dropdown, DataPolicy summary column, multi-select for compare, notes editing, CSV export, column visibility (features ported from deleted REST table).
- **Delete** `Frontend/src/app/components/engine-lab/engine-history/` directory.
- **Modify** `Frontend/src/app/components/lean-engine/lean-engine.component.{ts,html}` — remove `EngineHistoryComponent` reference.

### Phase 4 — Compare endpoint + view

- **Create** `Backend/Controllers/CompareController.cs` — `GET /api/runs/compare?left=&right=`.
- **Create** `Backend/Models/Compare/CompareResponse.cs` + supporting records.
- **Create** `Backend/Services/RunCompareService.cs` — equivalence gate, summary deltas, state-trace detection. Delegates trade reconciliation to Python.
- **Create** `PythonDataService/app/routers/reconcile_trades.py` — new endpoint `POST /api/lean-sidecar/reconcile-trades` (wraps existing `reconcile_trade_lists`).
- **Modify** `Frontend/src/app/app.routes.ts` — `/runs/compare` route loads new component.
- **Create** `Frontend/src/app/components/runs-compare/runs-compare.component.{ts,html,scss,spec.ts}`.
- **Create** `Frontend/src/app/services/runs-compare.service.ts` — HTTP client for `/api/runs/compare`.
- **Create** `PythonDataService/tests/integration/test_runs_compare_e2e.py` — `@pytest.mark.slow`.

### Phase 5 — Unified Engine Lab UI + cleanup

- **Modify** `Frontend/src/app/components/lean-engine/lean-engine.component.{ts,html,scss}` — Engine dropdown signal; `composeDataPolicy(form)`; submit branches by engine.
- **Create** `Frontend/src/app/components/lean-script-editor/lean-script-editor.component.{ts,html,scss,spec.ts}` — Monaco/CodeMirror wrapper + Problems panel.
- **Create** `Frontend/src/app/services/lean-lint.service.ts` — HTTP client for `/api/lean-sidecar/lint`.
- **Create** `PythonDataService/app/routers/lean_lint.py` — new ruff-backed lint router.
- **Modify** `PythonDataService/app/main.py` — register lint router.
- **Create** `PythonDataService/tests/unit/lean_sidecar/test_lint_endpoint.py`.
- **Modify** `Frontend/src/app/app.routes.ts` — delete `/lean-lab` route; add redirect to `/engine`.
- **Delete** `Frontend/src/app/components/lean-lab/` directory.
- **Modify** `Frontend/src/app/services/lean-sidecar.service.ts` — keep `startTrustedRun` only.

---

## Tasks

# PHASE 1 — DataPolicy contract

Lands the renamed `DataPolicy` type, removes `bar_minutes` from the LEAN request boundary, and widens the adjustment-vocabulary assertion. Pure backend; no behavior change for existing callers.

---

### Task 1.1: Add `DataPolicy` re-export module + deprecation alias

**Files:**
- Create: `PythonDataService/app/lean_sidecar/data_policy.py`
- Create: `PythonDataService/tests/unit/lean_sidecar/test_data_policy.py`

- [ ] **Step 1: Write the failing test**

```python
# PythonDataService/tests/unit/lean_sidecar/test_data_policy.py
"""DataPolicy contract tests."""

from __future__ import annotations

import json
import warnings


def test_data_policy_canonical_import_path() -> None:
    """DataPolicy is importable from app.lean_sidecar.data_policy."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy  # noqa: F401


def test_data_policy_manifest_alias_emits_deprecation_warning() -> None:
    """DataPolicyManifest alias still works for one cycle but warns."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from app.lean_sidecar.data_policy import DataPolicyManifest  # noqa: F401

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) >= 1
    assert "DataPolicy" in str(deprecations[0].message)


def test_data_policy_roundtrips_to_json_with_sorted_keys() -> None:
    """Canonical serialization is sort_keys=True; roundtrip preserves values."""
    from dataclasses import asdict

    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    dp = DataPolicy(
        source="polygon",
        symbol="SPY",
        adjusted=True,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    serialized = json.dumps(asdict(dp), sort_keys=True)
    parsed = json.loads(serialized)
    assert parsed["symbol"] == "SPY"
    assert parsed["input_bars"]["multiplier"] == 1
    assert parsed["strategy_bars"]["multiplier"] == 15
    assert parsed["adjusted"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_data_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.lean_sidecar.data_policy'`.

- [ ] **Step 3: Create the re-export module + alias**

```python
# PythonDataService/app/lean_sidecar/data_policy.py
"""Canonical import path for the shared DataPolicy contract.

PR B (2026-05-19) introduces ``DataPolicy`` as a backend-neutral shared
shape. The dataclass definition lives in ``manifest.py`` because PR A
embeds it inside ``RunManifest``; this module re-exports it from a
neutral path so non-manifest callers (engine persistence, GraphQL
mapping, compare endpoint) don't reach into the LEAN-specific module.

``DataPolicyManifest`` is kept as a re-export alias for one deprecation
cycle. New code imports ``DataPolicy``.
"""

from __future__ import annotations

import warnings

from app.lean_sidecar.manifest import BarsSpec, DataPolicy

__all__ = ["BarsSpec", "DataPolicy", "DataPolicyManifest"]


def __getattr__(name: str):
    if name == "DataPolicyManifest":
        warnings.warn(
            "DataPolicyManifest is renamed to DataPolicy; import from "
            "app.lean_sidecar.data_policy.DataPolicy. Alias removed in a "
            "later cleanup PR.",
            DeprecationWarning,
            stacklevel=2,
        )
        return DataPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Note: until Task 1.2 renames the class in `manifest.py`, this import will fail. The test stays red between Tasks 1.1 and 1.2.

- [ ] **Step 4: Confirm test still fails (waiting for Task 1.2)**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_data_policy.py -v`
Expected: FAIL on `ImportError: cannot import name 'DataPolicy' from 'app.lean_sidecar.manifest'`.

---

### Task 1.2: Rename `DataPolicyManifest` → `DataPolicy` in `manifest.py`

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/manifest.py`
- Modify: `PythonDataService/app/services/lean_sidecar_service.py`
- Modify: `PythonDataService/tests/lean_sidecar/test_manifest.py`

- [ ] **Step 1: Locate callers**

Run: `grep -rn "DataPolicyManifest" PythonDataService/ --include="*.py"`
Expected: lists `app/lean_sidecar/manifest.py` (class definition), `app/services/lean_sidecar_service.py` (import + usage), `tests/lean_sidecar/test_manifest.py` (test fixtures).

- [ ] **Step 2: Rename the class in `manifest.py`**

Replace `class DataPolicyManifest:` with `class DataPolicy:`. Update the docstring to:

```python
@dataclass(frozen=True, slots=True)
class DataPolicy:
    """Where the bars came from and what processing they got.

    Backend-neutral shared shape. Embedded inside ``RunManifest.data_policy``
    for LEAN runs; persisted as JSONB on ``StrategyExecution`` rows for both
    Python and LEAN runs. Renamed from ``DataPolicyManifest`` in PR B
    (2026-05-19); the old name is kept as a deprecation-warning alias in
    ``app.lean_sidecar.data_policy``.
    """
```

Update `RunManifest.data_policy` annotation from `DataPolicyManifest` to `DataPolicy`.

- [ ] **Step 3: Update `app/services/lean_sidecar_service.py` import**

Change `from app.lean_sidecar.manifest import (..., DataPolicyManifest, ...)` to use `DataPolicy`. Rename usage at the `_build_data_policy` return type annotation and dataclass instantiation.

- [ ] **Step 4: Update `tests/lean_sidecar/test_manifest.py`**

Find every `DataPolicyManifest(` and replace with `DataPolicy(`.

- [ ] **Step 5: Run Task 1.1's test**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_data_policy.py -v`
Expected: 3 passed.

- [ ] **Step 6: Run the full lean_sidecar test suite for regressions**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ -v -k "not slow"`
Expected: same pass/fail count as the pre-Phase-1 baseline. The 19 pre-existing podman-on-PATH failures remain; everything else passes.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/lean_sidecar/manifest.py \
        PythonDataService/app/lean_sidecar/data_policy.py \
        PythonDataService/app/services/lean_sidecar_service.py \
        PythonDataService/tests/unit/lean_sidecar/test_data_policy.py \
        PythonDataService/tests/lean_sidecar/test_manifest.py
git commit -m "refactor(lean-sidecar): rename DataPolicyManifest to DataPolicy + canonical import path"
```

---

### Task 1.3: Pin `BarsSpec(minute, 60) != BarsSpec(hour, 1)` (v1 no-normalization contract)

**Files:**
- Create: `PythonDataService/tests/unit/lean_sidecar/test_bars_spec.py`

- [ ] **Step 1: Write the test**

```python
# PythonDataService/tests/unit/lean_sidecar/test_bars_spec.py
"""BarsSpec equality and serialization contract.

The v1 PR B equivalence gate does NOT normalize timespan/multiplier pairs.
{minute, 60} and {hour, 1} are NOT equal even though they describe the
same bar length. Flipping this contract requires changing this test
deliberately. See docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md § 4.3.
"""

from __future__ import annotations

import json
from dataclasses import asdict


def test_bars_spec_equality_is_field_level() -> None:
    from app.lean_sidecar.data_policy import BarsSpec

    assert BarsSpec(timespan="minute", multiplier=15) == BarsSpec(timespan="minute", multiplier=15)


def test_bars_spec_minute_60_not_equal_to_hour_1() -> None:
    """V1 contract pin: no semantic normalization. Different (timespan, multiplier) → not equal."""
    from app.lean_sidecar.data_policy import BarsSpec

    assert BarsSpec(timespan="minute", multiplier=60) != BarsSpec(timespan="hour", multiplier=1)


def test_bars_spec_json_shape() -> None:
    from app.lean_sidecar.data_policy import BarsSpec

    serialized = json.dumps(asdict(BarsSpec(timespan="minute", multiplier=15)))
    parsed = json.loads(serialized)
    assert parsed == {"timespan": "minute", "multiplier": 15}
```

- [ ] **Step 2: Run test (should pass against Task 1.2's renamed class)**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_bars_spec.py -v`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/tests/unit/lean_sidecar/test_bars_spec.py
git commit -m "test(lean-sidecar): pin BarsSpec v1 no-normalization contract"
```

---

### Task 1.4: Widen `_assert_adjustment_vocabulary_consistent`

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py`
- Modify: `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py`

- [ ] **Step 1: Add failing test cases**

Append to `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py`:

```python
def test_adjusted_true_with_raw_normalization_is_accepted() -> None:
    """PR B widens: pre-adjusted staging + LEAN Raw is the new default pairing."""
    from app.services.lean_sidecar_service import _assert_adjustment_vocabulary_consistent

    _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Raw")  # no raise


def test_adjusted_false_with_adjusted_normalization_is_rejected() -> None:
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_adjustment_vocabulary_consistent,
    )
    import pytest

    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Adjusted")


def test_adjusted_true_with_adjusted_normalization_is_rejected() -> None:
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_adjustment_vocabulary_consistent,
    )
    import pytest

    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Adjusted")


def test_adjusted_false_with_raw_normalization_is_accepted() -> None:
    """PR A's existing case: raw → raw."""
    from app.services.lean_sidecar_service import _assert_adjustment_vocabulary_consistent

    _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Raw")  # no raise
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py::test_adjusted_true_with_raw_normalization_is_accepted -v`
Expected: FAIL — current assertion raises on `(True, "Raw")`.

- [ ] **Step 3: Update the assertion function body**

In `PythonDataService/app/services/lean_sidecar_service.py`, replace the body of `_assert_adjustment_vocabulary_consistent` with:

```python
def _assert_adjustment_vocabulary_consistent(
    *,
    adjusted: bool,
    data_normalization_mode: str,
) -> None:
    """Enforce the matrix:

      (adjusted=False, "Raw")       -> accept (PR A's existing case: raw -> raw)
      (adjusted=True,  "Raw")       -> accept (PR B: pre-adjusted staging; LEAN reads as Raw)
      (adjusted=False, "Adjusted")  -> reject (LEAN would adjust unadjusted Polygon data)
      (adjusted=True,  "Adjusted")  -> reject (double-adjustment)

    The ``adjusted`` flag is the staging-pipeline policy, not LEAN's runtime
    normalization mode. See docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md § 4.4.
    """
    if data_normalization_mode == "Adjusted":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: data_normalization_mode='Adjusted' is "
            f"never valid in PR B's pre-adjusted-staging contract; got adjusted={adjusted}"
        )
    if data_normalization_mode != "Raw":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: unsupported data_normalization_mode={data_normalization_mode!r}; "
            "valid: 'Raw'"
        )
```

- [ ] **Step 4: Run the four adjustment tests**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v -k adjustment`
Expected: all pass. If PR A's existing `test_build_manifest_raises_when_adjusted_disagrees_with_normalization_mode` covers the now-valid `(True, "Raw")` case, update it: change the assertion or split into separate cases.

- [ ] **Step 5: Run the full file**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_service.py \
        PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git commit -m "feat(lean-sidecar): widen adjustment vocab to accept (adjusted=true, Raw)"
```

---

### Task 1.5: `TrustedRunRequest` carries `data_policy` (drops legacy top-level fields)

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py`
- Modify: `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`
- Modify: `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py`

- [ ] **Step 1: Read current `TrustedRunRequest`**

Read `PythonDataService/app/services/lean_sidecar_service.py` around lines 139-216.

- [ ] **Step 2: Write the failing test**

Append to `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py`:

```python
def test_trusted_run_request_carries_data_policy() -> None:
    """TrustedRunRequest exposes a single data_policy field; legacy top-level fields are gone."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
    from app.services.lean_sidecar_service import TrustedRunRequest

    dp = DataPolicy(
        source="polygon",
        symbol="SPY",
        adjusted=True,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    req = TrustedRunRequest(
        run_id="test-data-policy",
        algorithm_source="<source>",
        starting_cash=100_000.0,
        start_ms_utc=1736777400000,
        end_ms_utc=1737298200000,
        template="ema_crossover",
        data_policy=dp,
    )

    assert req.data_policy is dp
    assert req.symbol == "SPY"  # property accessor reads from data_policy
    # Legacy top-level dataclass fields removed:
    fields = {f.name for f in TrustedRunRequest.__dataclass_fields__.values()}
    assert "bar_minutes" not in fields
    assert "data_source" not in fields
    assert "adjustment" not in fields
    assert "session" not in fields
```

- [ ] **Step 3: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py::test_trusted_run_request_carries_data_policy -v`
Expected: FAIL — `symbol`/`bar_minutes`/etc. are still in the dataclass fields.

- [ ] **Step 4: Refactor `TrustedRunRequest`**

Replace the existing class with:

```python
@dataclass(frozen=True, slots=True)
class TrustedRunRequest:
    """LEAN sidecar run input — PR B canonical shape.

    Top-level ``symbol``/``bar_minutes``/``session``/``adjustment`` are gone;
    they live inside ``data_policy``. The router (TrustedRunRequestModel)
    accepts both shapes for one cycle and converts before constructing
    this dataclass.
    """

    run_id: str
    starting_cash: float
    start_ms_utc: int
    end_ms_utc: int
    data_policy: DataPolicy
    algorithm_source: str | None = None
    template: TrustedTemplate = "trusted_default"

    @property
    def symbol(self) -> str:
        return self.data_policy.symbol

    @property
    def start_date(self) -> date:
        return datetime.fromtimestamp(self.start_ms_utc / 1000, tz=UTC).astimezone(_ET).date()

    @property
    def end_date(self) -> date:
        from app.lean_sidecar.trading_calendar import is_trading_day

        exclusive_end = datetime.fromtimestamp(self.end_ms_utc / 1000, tz=UTC).astimezone(_ET).date()
        d = exclusive_end - timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d
```

Note: keeping `symbol`/`start_date`/`end_date` as `@property` so the existing orchestrator body works unchanged.

- [ ] **Step 5: Update `_build_data_policy`**

```python
def _build_data_policy(
    request: TrustedRunRequest,
    provider: Any | None,
) -> DataPolicy:
    """Return the request's data_policy with provider identity threaded in.

    The request already carries a fully-formed DataPolicy from the router.
    This function injects the provider's fixture identity (live vs replay).
    """
    from dataclasses import replace as dataclass_replace

    base = request.data_policy
    fixture_id = provider.fixture_id if provider is not None else None
    fixture_sha256 = provider.fixture_sha256 if provider is not None else None
    provider_kind = "fixture" if fixture_id is not None else "live"

    enriched = dataclass_replace(
        base,
        provider_kind=provider_kind,
        fixture_id=fixture_id,
        fixture_sha256=fixture_sha256,
    )

    _assert_adjustment_vocabulary_consistent(
        adjusted=enriched.adjusted,
        data_normalization_mode="Raw",
    )
    return enriched
```

- [ ] **Step 6: Update `run_trusted_sample` body**

Inside `run_trusted_sample`, find references to:
- `request.bar_minutes` → `request.data_policy.strategy_bars.multiplier`
- `request.adjustment` → `"raw"` if `not request.data_policy.adjusted` else `"adjusted"`
- `request.session` → `request.data_policy.session`
- `request.data_source` → `request.data_policy.source`

Update each call site (most are in the Polygon path's `fetch_canonical_minute_bars(...)` arguments).

- [ ] **Step 7: Update `ema_crossover` template DataPolicy default**

In `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`, ensure the trusted-sample default DataPolicy uses `strategy_bars=BarsSpec(timespan="minute", multiplier=15)`. The template-internal pin replaces PR A's global Literal.

- [ ] **Step 8: Run the test from Step 2**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py::test_trusted_run_request_carries_data_policy -v`
Expected: PASS.

- [ ] **Step 9: Run broader sidecar suite**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/unit/lean_sidecar/ -v -k "not slow"`
Expected: all pass except the 19 pre-existing podman-on-PATH failures.

- [ ] **Step 10: Commit**

```bash
git add PythonDataService/app/services/lean_sidecar_service.py \
        PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py \
        PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git commit -m "refactor(lean-sidecar): TrustedRunRequest carries data_policy block"
```

---

### Task 1.6: `TrustedRunRequestModel` accepts legacy + new shapes with adapter

**Files:**
- Modify: `PythonDataService/app/routers/lean_sidecar.py`
- Modify: `PythonDataService/tests/lean_sidecar/test_router_lean_sidecar.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_trusted_run_request_model_accepts_legacy_top_level_shape() -> None:
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-legacy-shape",
        "symbol": "SPY",
        "start_ms_utc": 1736777400000,
        "end_ms_utc": 1737298200000,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy is not None
    assert model.data_policy.symbol == "SPY"
    assert model.data_policy.session == "regular"
    assert model.data_policy.adjusted is False  # adjustment="raw" -> adjusted=False
    assert model.data_policy.strategy_bars.multiplier == 15


def test_trusted_run_request_model_accepts_new_data_policy_shape() -> None:
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-new-shape",
        "start_ms_utc": 1736777400000,
        "end_ms_utc": 1737298200000,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": True,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
            "timestamp_policy": "bar_close_ms_utc",
            "timezone": "America/New_York",
            "provider_kind": "live",
            "fixture_id": None,
            "fixture_sha256": None,
        },
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy.symbol == "SPY"
    assert model.data_policy.adjusted is True


def test_trusted_run_request_model_rejects_mixed_shape() -> None:
    import pytest
    from pydantic import ValidationError

    from app.routers.lean_sidecar import TrustedRunRequestModel

    with pytest.raises(ValidationError, match="data_policy"):
        TrustedRunRequestModel(
            run_id="test-mixed",
            symbol="SPY",
            start_ms_utc=1736777400000,
            end_ms_utc=1737298200000,
            starting_cash=100_000.0,
            data_policy={
                "source": "polygon", "symbol": "SPY", "adjusted": True, "session": "regular",
                "input_bars": {"timespan": "minute", "multiplier": 1},
                "strategy_bars": {"timespan": "minute", "multiplier": 15},
                "timestamp_policy": "bar_close_ms_utc",
                "timezone": "America/New_York",
                "provider_kind": "live", "fixture_id": None, "fixture_sha256": None,
            },
        )


def test_trusted_run_request_model_defaults_adjustment_to_true() -> None:
    """Omitting both legacy `adjustment` and `data_policy.adjusted` -> adjusted=True."""
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-default-adj",
        "symbol": "SPY",
        "start_ms_utc": 1736777400000,
        "end_ms_utc": 1737298200000,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        # no "adjustment" key
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy.adjusted is True
```

- [ ] **Step 2: Verify failures**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_router_lean_sidecar.py -v -k "data_policy or legacy or mixed or defaults_adjustment"`
Expected: all fail (`data_policy` field doesn't exist yet).

- [ ] **Step 3: Replace `TrustedRunRequestModel`**

In `PythonDataService/app/routers/lean_sidecar.py`:

```python
class _BarsSpecModel(BaseModel):
    timespan: Literal["minute", "hour", "day"]
    multiplier: int = Field(..., ge=1)


class _DataPolicyModel(BaseModel):
    source: Literal["synthetic", "polygon"]
    symbol: str = Field(..., pattern=TICKER_SYMBOL_PATTERN.pattern)
    adjusted: bool = True
    session: Literal["regular", "extended"]
    input_bars: _BarsSpecModel
    strategy_bars: _BarsSpecModel
    timestamp_policy: Literal["bar_close_ms_utc"] = "bar_close_ms_utc"
    timezone: Literal["America/New_York"] = "America/New_York"
    provider_kind: Literal["live", "fixture"] = "live"
    fixture_id: str | None = None
    fixture_sha256: str | None = None


class TrustedRunRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., pattern=RUN_ID_PATTERN.pattern)
    start_ms_utc: int = Field(..., ge=_MIN_EPOCH_MS, le=_MAX_EPOCH_MS)
    end_ms_utc: int = Field(..., ge=_MIN_EPOCH_MS, le=_MAX_EPOCH_MS)
    starting_cash: float = Field(default=100_000.0, ge=_MIN_STARTING_CASH, le=_MAX_STARTING_CASH)
    algorithm_source: str | None = None
    template: Literal["trusted_default", "reconciliation", "ema_crossover"] = "trusted_default"

    data_policy: _DataPolicyModel | None = None

    # Legacy top-level fields (one deprecation cycle).
    symbol: str | None = Field(default=None, pattern=TICKER_SYMBOL_PATTERN.pattern)
    data_source: Literal["synthetic", "polygon"] | None = None
    bar_minutes: int | None = Field(default=None, ge=1)
    session: Literal["regular", "extended"] | None = None
    adjustment: Literal["raw", "adjusted"] | None = None

    @model_validator(mode="after")
    def _normalize_to_data_policy(self) -> "TrustedRunRequestModel":
        legacy_present = any(
            v is not None
            for v in (self.symbol, self.data_source, self.bar_minutes, self.session, self.adjustment)
        )
        if self.data_policy is not None and legacy_present:
            raise ValueError(
                "Cannot mix top-level legacy fields with data_policy block; choose one shape."
            )
        if self.data_policy is None:
            if self.symbol is None or self.session is None or self.bar_minutes is None or self.data_source is None:
                raise ValueError(
                    "When data_policy is omitted, symbol/session/bar_minutes/data_source are required."
                )
            adjusted = True if self.adjustment in (None, "adjusted") else False
            self.data_policy = _DataPolicyModel(
                source=self.data_source,
                symbol=self.symbol.upper(),
                adjusted=adjusted,
                session=self.session,
                input_bars=_BarsSpecModel(timespan="minute", multiplier=1),
                strategy_bars=_BarsSpecModel(timespan="minute", multiplier=self.bar_minutes),
            )
            logger.warning(
                "TrustedRunRequest using legacy top-level shape; convert to data_policy block. run_id=%s",
                self.run_id,
            )
        # Reuse PR A's window validation, but read from data_policy.
        self._validate_window_normalized()
        return self

    def _validate_window_normalized(self) -> None:
        # Carry over PR A's existing _validate_window body, but replace
        # `self.symbol`/`self.session`/etc. with `self.data_policy.symbol`/etc.
        # The window validation logic itself is unchanged.
        ...  # implementer: paste PR A's window logic, swap field reads
```

- [ ] **Step 4: Update `post_trusted_run` to thread `data_policy`**

Change:

```python
request = TrustedRunRequest(
    run_id=payload.run_id,
    symbol=payload.symbol.upper(),
    ...
)
```

to:

```python
request = TrustedRunRequest(
    run_id=payload.run_id,
    starting_cash=payload.starting_cash,
    start_ms_utc=payload.start_ms_utc,
    end_ms_utc=payload.end_ms_utc,
    algorithm_source=payload.algorithm_source,
    template=payload.template,
    data_policy=DataPolicy(**payload.data_policy.model_dump()),
)
```

- [ ] **Step 5: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_router_lean_sidecar.py -v -k "data_policy or legacy or mixed or defaults_adjustment"`
Expected: all four new tests pass.

- [ ] **Step 6: Run full router suite + sidecar suite**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/unit/lean_sidecar/ -v -k "not slow"`
Expected: all pass except the 19 pre-existing podman-PATH failures.

- [ ] **Step 7: Lint**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/` (from host).
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add PythonDataService/app/routers/lean_sidecar.py \
        PythonDataService/tests/lean_sidecar/test_router_lean_sidecar.py
git commit -m "feat(lean-sidecar): TrustedRunRequestModel accepts data_policy block + legacy adapter"
```

---

### Task 1.7: Open PR B.1

- [ ] **Step 1: Push branch**

```bash
git push -u origin pr-b-1-data-policy-contract
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(lean-sidecar): PR B.1 — DataPolicy contract + LEAN request shape refactor" --body "$(cat <<'EOF'
## Summary

PR B Phase 1 (per docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md):

- Rename DataPolicyManifest -> DataPolicy; canonical import at app.lean_sidecar.data_policy; old name kept as DeprecationWarning alias.
- Drop bar_minutes/session/adjustment/symbol from TrustedRunRequest; new data_policy: DataPolicy field.
- TrustedRunRequestModel accepts both legacy top-level shape (one deprecation cycle) and new data_policy block; mixed -> 422.
- Widen _assert_adjustment_vocabulary_consistent: accept (adjusted=True, "Raw") as pre-adjusted-staging case.
- Default adjusted=True when omitted on both paths.
- v1 contract pin: BarsSpec(minute, 60) != BarsSpec(hour, 1) (no normalization).

No UI change. No persistence change yet (Phase 2). No behavior change for existing LEAN sidecar callers that use the legacy shape.

## Test plan

- [ ] `ruff check PythonDataService/app/ PythonDataService/tests/` clean
- [ ] `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ tests/unit/lean_sidecar/ -v -k "not slow"` — all pass except 19 pre-existing podman-PATH failures
- [ ] Manual: POST /api/lean-sidecar/trusted-runs with legacy shape -> succeeds + logs deprecation warning
- [ ] Manual: POST /api/lean-sidecar/trusted-runs with new data_policy shape -> succeeds

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Pull master, start Phase 2**

```bash
git checkout master
git pull
git checkout -b pr-b-2-persistence
```

---

# PHASE 2 — Engine-side persistence

Adds `DataPolicyJson`, `CommissionPerOrder`, `BrokeragePolicy` to `StrategyExecution`; both engines write the shared shape.

---

### Task 2.1: Postgres migration — add columns + symbol index

**Files:**
- Create: `Backend/Migrations/<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.cs`
- Modify: `Backend/Models/MarketData/StrategyExecution.cs`

- [ ] **Step 1: Add EF Core model properties**

In `Backend/Models/MarketData/StrategyExecution.cs`, add:

```csharp
[Column(TypeName = "jsonb")]
public string? DataPolicyJson { get; set; }

[Column(TypeName = "numeric(18,8)")]
public decimal? CommissionPerOrder { get; set; }

[Column(TypeName = "varchar(40)")]
public string? BrokeragePolicy { get; set; }
```

- [ ] **Step 2: Generate the migration**

```bash
cd Backend
dotnet ef migrations add AddDataPolicyAndCommissionToStrategyExecution
```

Expected: creates `<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.cs` + `.Designer.cs` + updates `MyDbContextModelSnapshot.cs`.

- [ ] **Step 3: Edit migration to add the symbol index**

In the generated `<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.cs`, after the `AddColumn` calls in `Up()`:

```csharp
migrationBuilder.Sql(
    @"CREATE INDEX IF NOT EXISTS ix_strategyexecution_datapolicy_symbol
      ON ""StrategyExecution"" ((""DataPolicyJson""->>'symbol'));");
```

And in `Down()`, before the `DropColumn` calls:

```csharp
migrationBuilder.Sql("DROP INDEX IF EXISTS ix_strategyexecution_datapolicy_symbol;");
```

- [ ] **Step 4: Apply the migration**

```bash
dotnet ef database update
```

Expected: applied; no errors.

- [ ] **Step 5: Verify columns + index**

```bash
podman exec -it my-postgres psql -U postgres -d <dbname> -c "\d \"StrategyExecution\""
```

Expected: `DataPolicyJson` jsonb, `CommissionPerOrder` numeric(18,8), `BrokeragePolicy` varchar(40), and `ix_strategyexecution_datapolicy_symbol` listed.

- [ ] **Step 6: Commit**

```bash
git add Backend/Models/MarketData/StrategyExecution.cs \
        Backend/Migrations/<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.cs \
        Backend/Migrations/<timestamp>_AddDataPolicyAndCommissionToStrategyExecution.Designer.cs \
        Backend/Migrations/MyDbContextModelSnapshot.cs
git commit -m "feat(db): add DataPolicyJson/CommissionPerOrder/BrokeragePolicy columns to StrategyExecution"
```

---

### Task 2.2: Persist payload shapes carry `DataPolicy` fields

**Files:**
- Modify: persist payload record(s) — locate via grep

- [ ] **Step 1: Locate the payloads**

```bash
grep -rn "PersistLeanPayload\|PersistEnginePayload\|PersistLeanRunPayload" Backend/ --include="*.cs" | head -20
```

Identify the exact file names (PR #291 introduced the persistence endpoints).

- [ ] **Step 2: Add fields to each payload record**

For each payload, add:

```csharp
public string? DataPolicyJson { get; init; }
public decimal? CommissionPerOrder { get; init; }
public string? BrokeragePolicy { get; init; }
```

`DataPolicyJson` is a string (Python emits JSON, .NET persists as `jsonb`).

- [ ] **Step 3: Commit**

```bash
git add Backend/Models/Persistence/
git commit -m "feat(persistence): add DataPolicy/Commission/Brokerage fields to persist payloads"
```

---

### Task 2.3: `BacktestRunPersistenceService` writes the new columns

**Files:**
- Modify: `Backend/Services/BacktestRunPersistenceService.cs`
- Modify: `Backend.Tests/Services/BacktestRunPersistenceServiceTests.cs`

- [ ] **Step 1: Write failing tests**

Append to `Backend.Tests/Services/BacktestRunPersistenceServiceTests.cs`:

```csharp
[Fact]
public async Task PersistEngineAsync_StoresDataPolicy_OnStrategyExecution()
{
    var ctx = CreateDbContext();
    var sut = new BacktestRunPersistenceService(ctx, NullLogger<BacktestRunPersistenceService>.Instance);
    var payload = new PersistEnginePayload
    {
        // ... existing required fields ...
        DataPolicyJson = """{"source":"polygon","symbol":"SPY","adjusted":true,"session":"regular","input_bars":{"timespan":"minute","multiplier":1},"strategy_bars":{"timespan":"minute","multiplier":15},"timestamp_policy":"bar_close_ms_utc","timezone":"America/New_York","provider_kind":"live","fixture_id":null,"fixture_sha256":null}""",
        CommissionPerOrder = 0m,
        BrokeragePolicy = "algorithm_default",
    };

    var id = await sut.PersistEngineAsync(payload);

    var row = await ctx.StrategyExecution.SingleAsync(s => s.Id == id);
    row.DataPolicyJson.Should().Be(payload.DataPolicyJson);
    row.CommissionPerOrder.Should().Be(0m);
    row.BrokeragePolicy.Should().Be("algorithm_default");
    row.Source.Should().Be("engine");
}

[Fact]
public async Task PersistLeanAsync_StoresDataPolicy_FromManifestPassthrough()
{
    // Same shape, Source="lean-sidecar", LeanRunId set
}

[Fact]
public async Task PersistEngineAsync_DefaultsAdjustedToTrue_WhenDataPolicyOmittedFromLegacyClient()
{
    var ctx = CreateDbContext();
    var sut = new BacktestRunPersistenceService(ctx, NullLogger<BacktestRunPersistenceService>.Instance);
    var payload = new PersistEnginePayload
    {
        // existing required fields, but DataPolicyJson = null (legacy)
        Symbol = "SPY",
    };

    var id = await sut.PersistEngineAsync(payload);

    var row = await ctx.StrategyExecution.SingleAsync(s => s.Id == id);
    row.DataPolicyJson.Should().NotBeNull();
    var parsed = JsonSerializer.Deserialize<JsonElement>(row.DataPolicyJson!);
    parsed.GetProperty("adjusted").GetBoolean().Should().BeTrue();
    parsed.GetProperty("symbol").GetString().Should().Be("SPY");
}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~PersistEngineAsync_StoresDataPolicy|PersistLeanAsync_StoresDataPolicy|PersistEngineAsync_DefaultsAdjusted"
```

Expected: compile errors or test failures.

- [ ] **Step 3: Update `PersistEngineAsync`**

Find the existing row construction. Add:

```csharp
var row = new StrategyExecution
{
    // ... existing fields ...
    DataPolicyJson = payload.DataPolicyJson ?? SynthesizeLegacyDataPolicy(payload),
    CommissionPerOrder = payload.CommissionPerOrder ?? 0m,
    BrokeragePolicy = payload.BrokeragePolicy ?? "algorithm_default",
};
```

Add the synthesis helper at class scope:

```csharp
private static string SynthesizeLegacyDataPolicy(PersistEnginePayload p)
{
    // One-cycle backwards-compat for pre-PR-B clients.
    var dp = new
    {
        source = "polygon",
        symbol = p.Symbol?.ToUpperInvariant() ?? "",
        adjusted = true,
        session = "regular",
        input_bars = new { timespan = "minute", multiplier = 1 },
        strategy_bars = new { timespan = "minute", multiplier = 15 },
        timestamp_policy = "bar_close_ms_utc",
        timezone = "America/New_York",
        provider_kind = "live",
        fixture_id = (string?)null,
        fixture_sha256 = (string?)null,
    };
    return JsonSerializer.Serialize(dp);
}
```

- [ ] **Step 4: Update `PersistLeanAsync` likewise**

Same three fields, but `BrokeragePolicy = payload.BrokeragePolicy ?? <whatever-manifest-set>` (reads from manifest in the existing passthrough flow).

- [ ] **Step 5: Run the new tests**

```bash
dotnet test --filter "FullyQualifiedName~PersistEngineAsync_StoresDataPolicy|PersistLeanAsync_StoresDataPolicy|PersistEngineAsync_DefaultsAdjusted"
```

Expected: all 3 pass.

- [ ] **Step 6: Run full persistence test class**

```bash
dotnet test --filter "FullyQualifiedName~BacktestRunPersistenceService"
```

Expected: all existing + new tests pass.

- [ ] **Step 7: Commit**

```bash
git add Backend/Services/BacktestRunPersistenceService.cs \
        Backend.Tests/Services/BacktestRunPersistenceServiceTests.cs
git commit -m "feat(persistence): write DataPolicy/Commission/BrokeragePolicy on engine + lean rows"
```

---

### Task 2.4: Python `EngineBacktestRequest` + `Response` carry `data_policy`

**Files:**
- Modify: `PythonDataService/app/routers/engine.py`
- Modify: `PythonDataService/app/services/strategy_engine.py`
- Create: `PythonDataService/tests/integration/test_engine_persistence_data_policy.py`

- [ ] **Step 1: Write the integration test**

```python
"""Phase 2 integration: Python engine request/response carry data_policy."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_engine_backtest_request_accepts_data_policy_block() -> None:
    """A request including a data_policy block is accepted and echoed."""
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "backtest": {
                "strategy_name": "spy_ema_crossover",
                "starting_cash": 100_000.0,
                "params": {"symbol": "SPY"},
                "start_date": "2025-01-13",
                "end_date": "2025-01-17",
                "resolution": "minute",
                "data_policy": {
                    "source": "polygon",
                    "symbol": "SPY",
                    "adjusted": True,
                    "session": "regular",
                    "input_bars": {"timespan": "minute", "multiplier": 1},
                    "strategy_bars": {"timespan": "minute", "multiplier": 15},
                    "timestamp_policy": "bar_close_ms_utc",
                    "timezone": "America/New_York",
                    "provider_kind": "live",
                    "fixture_id": None,
                    "fixture_sha256": None,
                },
            }
        }
        # The schema layer validates without actually running the engine.
        # Engine execution is gated behind a longer integration test.
        from app.routers.engine import EngineBacktestRequest
        req = EngineBacktestRequest(**payload["backtest"])
        assert req.data_policy is not None
        assert req.data_policy.symbol == "SPY"
        assert req.data_policy.adjusted is True


@pytest.mark.asyncio
async def test_engine_backtest_synthesizes_data_policy_from_legacy_fields() -> None:
    """A request without data_policy synthesizes it from symbol/resolution."""
    from app.routers.engine import EngineBacktestRequest

    req = EngineBacktestRequest(
        strategy_name="spy_ema_crossover",
        starting_cash=100_000.0,
        params={"symbol": "SPY"},
        start_date="2025-01-13",
        end_date="2025-01-17",
        resolution="minute",
    )
    assert req.data_policy is not None
    assert req.data_policy.symbol == "SPY"
    assert req.data_policy.adjusted is True  # PR B default
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/integration/test_engine_persistence_data_policy.py -v`
Expected: FAIL — `EngineBacktestRequest` doesn't yet have `data_policy`.

- [ ] **Step 3: Add `data_policy` to `EngineBacktestRequest`**

In `PythonDataService/app/routers/engine.py`:

```python
from app.routers.lean_sidecar import _BarsSpecModel, _DataPolicyModel


class EngineBacktestRequest(BaseModel):
    strategy_name: str
    starting_cash: float
    fill_mode: str = "signal_bar_close"
    params: dict = Field(default_factory=dict)
    start_date: str
    end_date: str
    auto_fetch: bool = True
    resolution: Literal["minute", "daily"] = "minute"

    data_policy: _DataPolicyModel | None = None

    @model_validator(mode="after")
    def _synthesize_legacy_data_policy(self) -> "EngineBacktestRequest":
        if self.data_policy is None:
            symbol = self.params.get("symbol") if isinstance(self.params, dict) else None
            if not symbol:
                raise ValueError("data_policy is required when params.symbol is absent")
            timespan = "day" if self.resolution == "daily" else "minute"
            self.data_policy = _DataPolicyModel(
                source="polygon",
                symbol=str(symbol).upper(),
                adjusted=True,
                session="regular",
                input_bars=_BarsSpecModel(timespan=timespan, multiplier=1),
                strategy_bars=_BarsSpecModel(timespan=timespan, multiplier=1),
            )
        return self
```

- [ ] **Step 4: Add `data_policy` to `EngineBacktestResponse`**

```python
class EngineBacktestResponse(BaseModel):
    # ... existing fields ...
    data_policy: _DataPolicyModel
```

In `app/services/strategy_engine.py` (or wherever the response is constructed), populate `data_policy` from the request's normalized value before returning.

- [ ] **Step 5: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/integration/test_engine_persistence_data_policy.py -v`
Expected: 2 passed.

- [ ] **Step 6: Run broader engine + router suites**

Run: `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow and (engine or strategy or router)"`
Expected: all pass (except the 19 podman-PATH).

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/routers/engine.py \
        PythonDataService/app/services/strategy_engine.py \
        PythonDataService/tests/integration/test_engine_persistence_data_policy.py
git commit -m "feat(engine): EngineBacktestRequest/Response carry data_policy block"
```

---

### Task 2.5: Frontend Python engine submission includes `data_policy`

**Files:**
- Create: `Frontend/src/app/models/data-policy.ts`
- Modify: `Frontend/src/app/components/lean-engine/lean-engine.component.ts` (around line 669-679)

- [ ] **Step 1: Write the component test**

In `Frontend/src/app/components/lean-engine/lean-engine.component.spec.ts`, add:

```typescript
describe('LeanEngineComponent.composeDataPolicy', () => {
  it('synthesizes a canonical DataPolicy from current form fields', () => {
    const fixture = TestBed.createComponent(LeanEngineComponent);
    const component = fixture.componentInstance;

    component.ticker.set('SPY');
    component.fromDate.set('2025-01-13');
    component.toDate.set('2025-01-17');
    component.resolution.set('minute');
    component.session.set('rth');
    component.startingCash.set(100_000);

    const dp = component.composeDataPolicy();

    expect(dp).toEqual({
      source: 'polygon',
      symbol: 'SPY',
      adjusted: true,
      session: 'regular',
      input_bars: { timespan: 'minute', multiplier: 1 },
      strategy_bars: { timespan: 'minute', multiplier: 1 },
      timestamp_policy: 'bar_close_ms_utc',
      timezone: 'America/New_York',
      provider_kind: 'live',
      fixture_id: null,
      fixture_sha256: null,
    });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec my-frontend npx ng test --include='**/lean-engine.component.spec.ts' --watch=false`
Expected: FAIL — `composeDataPolicy` not defined.

- [ ] **Step 3: Create the DataPolicy interface**

```typescript
// Frontend/src/app/models/data-policy.ts
export interface BarsSpec {
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
}

export interface DataPolicy {
  source: 'polygon' | 'synthetic';
  symbol: string;
  adjusted: boolean;
  session: 'regular' | 'extended';
  input_bars: BarsSpec;
  strategy_bars: BarsSpec;
  timestamp_policy: 'bar_close_ms_utc';
  timezone: 'America/New_York';
  provider_kind: 'live' | 'fixture';
  fixture_id: string | null;
  fixture_sha256: string | null;
}
```

- [ ] **Step 4: Add `composeDataPolicy()` to `LeanEngineComponent`**

```typescript
import { DataPolicy } from '../../models/data-policy';

// inside the class:
composeDataPolicy(): DataPolicy {
  const sessionFromForm = this.session() === 'rth' ? 'regular' : 'extended';
  const timespan = this.resolution() === 'daily' ? 'day' : 'minute';
  return {
    source: 'polygon',
    symbol: this.ticker(),
    adjusted: true,
    session: sessionFromForm,
    input_bars: { timespan, multiplier: 1 },
    strategy_bars: { timespan, multiplier: 1 },
    timestamp_policy: 'bar_close_ms_utc',
    timezone: 'America/New_York',
    provider_kind: 'live',
    fixture_id: null,
    fixture_sha256: null,
  };
}
```

- [ ] **Step 5: Include `data_policy` in the `run()` payload**

Around line 669-679, change the `backtest` payload to include `data_policy: this.composeDataPolicy()`.

- [ ] **Step 6: Run the test**

Run: `podman exec my-frontend npx ng test --include='**/lean-engine.component.spec.ts' --watch=false`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add Frontend/src/app/components/lean-engine/lean-engine.component.ts \
        Frontend/src/app/components/lean-engine/lean-engine.component.spec.ts \
        Frontend/src/app/models/data-policy.ts
git commit -m "feat(frontend): compose data_policy in engine backtest submission"
```

---

### Task 2.6: Frontend LEAN sidecar submission includes `data_policy`

**Files:**
- Modify: `Frontend/src/app/services/lean-sidecar.service.ts`
- Modify: `Frontend/src/app/services/lean-sidecar.types.ts` (or wherever the type lives)
- Modify: `Frontend/src/app/components/lean-lab/lean-lab.component.ts` (still in service in this phase)

- [ ] **Step 1: Update `TrustedRunRequest` type**

In `Frontend/src/app/services/lean-sidecar.types.ts`:

```typescript
import { DataPolicy } from '../models/data-policy';

export interface TrustedRunRequest {
  run_id: string;
  algorithm_source: string;
  starting_cash: number;
  start_ms_utc: number;
  end_ms_utc: number;
  template?: 'trusted_default' | 'reconciliation' | 'ema_crossover';
  data_policy: DataPolicy;
}
```

- [ ] **Step 2: Update `LeanLabComponent` submit handler**

Replace the legacy top-level symbol/bar_minutes/session fields in the submit body with a `data_policy` block composed from current form state.

- [ ] **Step 3: Run frontend tests**

Run: `podman exec my-frontend npx ng test --include='**/lean-lab.component.spec.ts' --include='**/lean-sidecar.service.spec.ts' --watch=false`
Expected: pass (update existing specs that assert on legacy shape).

- [ ] **Step 4: Commit**

```bash
git add Frontend/src/app/services/lean-sidecar.service.ts \
        Frontend/src/app/services/lean-sidecar.types.ts \
        Frontend/src/app/components/lean-lab/lean-lab.component.ts
git commit -m "feat(frontend): LEAN sidecar submission includes data_policy block"
```

---

### Task 2.7: Open PR B.2 (Phase 2 persistence)

- [ ] **Step 1: Push branch + open PR**

Title: `feat(persistence): PR B.2 — both engines persist DataPolicy + Commission + BrokeragePolicy`
Body: same template as Task 1.7 (lint clean, full test suite vs pre-PR-B baseline, manual e2e: post a Python engine run + a LEAN sidecar run, verify rows have matching JSON shape).

- [ ] **Step 2: Pull master, start Phase 3**

```bash
git checkout master
git pull
git checkout -b pr-b-3-history-surfaces
```

---

# PHASE 3 — History surfaces

GraphQL exposes `engine` + `dataPolicy`; unified history table gets the Engine column; REST-backed `EngineHistoryComponent` retires.

---

### Task 3.1: GraphQL types — `Engine`, `BarsSpec`, `DataPolicy`

**Files:**
- Create: `Backend/GraphQL/Types/EngineType.cs`
- Create: `Backend/GraphQL/Types/BarsSpecType.cs`
- Create: `Backend/GraphQL/Types/DataPolicyType.cs`
- Modify: `Backend/Program.cs` (or wherever HC v15 is configured)

- [ ] **Step 1: Create `EngineType.cs`**

```csharp
namespace Backend.GraphQL.Types;

public enum Engine
{
    Python,
    Lean,
}
```

- [ ] **Step 2: Create `BarsSpecType.cs`**

```csharp
namespace Backend.GraphQL.Types;

public record BarsSpec(string Timespan, int Multiplier);
```

- [ ] **Step 3: Create `DataPolicyType.cs`**

```csharp
namespace Backend.GraphQL.Types;

public record DataPolicy(
    string Source,
    string Symbol,
    bool Adjusted,
    string Session,
    BarsSpec InputBars,
    BarsSpec StrategyBars,
    string TimestampPolicy,
    string Timezone,
    string ProviderKind,
    string? FixtureId,
    string? FixtureSha256
);
```

- [ ] **Step 4: Register the types**

In `Program.cs`:

```csharp
builder.Services.AddGraphQLServer()
    // ... existing config ...
    .AddType<BarsSpec>()
    .AddType<DataPolicy>()
    .BindRuntimeType<Engine, EnumType<Engine>>();
```

- [ ] **Step 5: Commit**

```bash
git add Backend/GraphQL/Types/EngineType.cs \
        Backend/GraphQL/Types/BarsSpecType.cs \
        Backend/GraphQL/Types/DataPolicyType.cs \
        Backend/Program.cs
git commit -m "feat(graphql): add Engine/BarsSpec/DataPolicy types"
```

---

### Task 3.2: `BacktestRun.engine` + `BacktestRun.dataPolicy` resolvers

**Files:**
- Modify: `Backend/GraphQL/Resolvers/BacktestRunResolver.cs`
- Modify: `Backend/GraphQL/Types/BacktestRunDto.cs`
- Modify: `Backend.Tests/Resolvers/BacktestRunResolverTests.cs`

- [ ] **Step 1: Write failing tests**

```csharp
[Fact]
public async Task BacktestRun_Engine_DerivedFromSource()
{
    var ctx = CreateDbContext();
    ctx.StrategyExecution.Add(new StrategyExecution { Source = "engine", /* required fields */ });
    ctx.StrategyExecution.Add(new StrategyExecution { Source = "lean-sidecar", LeanRunId = "run-x", /* ... */ });
    await ctx.SaveChangesAsync();

    var executor = await BuildExecutor(ctx);
    var result = await executor.ExecuteAsync(@"{ backtestRuns { id engine } }");

    var data = result.ExpectQueryResult().Data!;
    var runs = (List<object?>)data["backtestRuns"]!;
    var engines = runs.Select(r => ((Dictionary<string, object?>)r!)["engine"]!.ToString()).ToList();
    engines.Should().Contain("PYTHON");
    engines.Should().Contain("LEAN");
}

[Fact]
public async Task BacktestRuns_EngineFilter_FiltersResultSet()
{
    // backtestRuns(engine: PYTHON) returns only "engine" rows
    // backtestRuns(engine: LEAN) returns only "lean-sidecar" rows
    // backtestRuns(engine: null) returns all rows
}

[Fact]
public async Task BacktestRun_DataPolicy_ParsedFromJson_NullOnLegacyRows()
{
    // Row with DataPolicyJson set -> dataPolicy non-null with parsed fields
    // Row with DataPolicyJson null (legacy) -> dataPolicy null in response
}
```

- [ ] **Step 2: Verify failure**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~BacktestRun_Engine_DerivedFromSource"
```

Expected: FAIL — `engine` field doesn't exist on the `BacktestRun` type.

- [ ] **Step 3: Update `BacktestRunDto`**

Add:

```csharp
public Engine Engine { get; set; }
public DataPolicy? DataPolicy { get; set; }
public decimal? CommissionPerOrder { get; set; }
public string? BrokeragePolicy { get; set; }
```

- [ ] **Step 4: Update the resolver**

In `BacktestRunResolver.cs`:

```csharp
[GraphQLName("backtestRuns")]
public static async Task<List<BacktestRunDto>> GetBacktestRuns(
    [Service] MyDbContext ctx,
    Engine? engine = null,
    string? symbol = null,
    int first = 50,
    CancellationToken cancellationToken = default)
{
    var query = ctx.StrategyExecution.AsNoTracking();
    if (engine == Engine.Python) query = query.Where(s => s.Source == "engine");
    else if (engine == Engine.Lean) query = query.Where(s => s.Source == "lean-sidecar");

    if (!string.IsNullOrEmpty(symbol)) query = query.Where(s => s.Ticker.Symbol == symbol);

    return await query
        .OrderByDescending(s => s.ExecutedAt)
        .Take(first)
        .Select(s => new BacktestRunDto
        {
            Id = s.Id,
            Source = s.Source,
            Engine = s.Source == "engine" ? Engine.Python : Engine.Lean,
            DataPolicy = s.DataPolicyJson != null
                ? JsonSerializer.Deserialize<DataPolicy>(s.DataPolicyJson)
                : null,
            CommissionPerOrder = s.CommissionPerOrder,
            BrokeragePolicy = s.BrokeragePolicy,
            // ... existing fields ...
        })
        .ToListAsync(cancellationToken);
}
```

- [ ] **Step 5: Run the tests**

```bash
dotnet test --filter "FullyQualifiedName~BacktestRun_Engine_DerivedFromSource|BacktestRuns_EngineFilter|BacktestRun_DataPolicy_ParsedFromJson"
```

Expected: 3 pass.

- [ ] **Step 6: Commit**

```bash
git add Backend/GraphQL/Resolvers/BacktestRunResolver.cs \
        Backend/GraphQL/Types/BacktestRunDto.cs \
        Backend.Tests/Resolvers/BacktestRunResolverTests.cs
git commit -m "feat(graphql): BacktestRun.engine derived from Source; dataPolicy parsed from JSON"
```

---

### Task 3.3: `updateBacktestRunNotes` mutation

**Files:**
- Create or modify: `Backend/GraphQL/Mutations/BacktestRunMutation.cs`
- Modify: `Backend.Tests/Resolvers/BacktestRunMutationTests.cs` (create if absent)

- [ ] **Step 1: Write failing test**

```csharp
[Fact]
public async Task UpdateBacktestRunNotes_PersistsNote_AndReturnsUpdatedRun()
{
    var ctx = CreateDbContext();
    var row = new StrategyExecution { /* required fields */ };
    ctx.StrategyExecution.Add(row);
    await ctx.SaveChangesAsync();

    var executor = await BuildExecutor(ctx);
    var query = $@"mutation {{ updateBacktestRunNotes(id: {row.Id}, notes: ""test note"") {{ id notes }} }}";
    var result = await executor.ExecuteAsync(query);

    var data = result.ExpectQueryResult().Data!;
    var returned = (Dictionary<string, object?>)data["updateBacktestRunNotes"]!;
    returned["notes"]!.ToString().Should().Be("test note");

    var refreshed = await ctx.StrategyExecution.SingleAsync(s => s.Id == row.Id);
    refreshed.Notes.Should().Be("test note");
}
```

- [ ] **Step 2: Implement mutation**

```csharp
[MutationType]
public static partial class BacktestRunMutation
{
    [GraphQLName("updateBacktestRunNotes")]
    public static async Task<BacktestRunDto> UpdateBacktestRunNotes(
        [Service] MyDbContext ctx,
        int id,
        string notes,
        CancellationToken cancellationToken = default)
    {
        var row = await ctx.StrategyExecution.SingleAsync(s => s.Id == id, cancellationToken);
        row.Notes = notes;
        await ctx.SaveChangesAsync(cancellationToken);
        return BacktestRunDto.From(row);
    }
}
```

Add `BacktestRunDto.From(StrategyExecution)` static factory if absent.

- [ ] **Step 3: Run, then commit**

```bash
git add Backend/GraphQL/Mutations/BacktestRunMutation.cs \
        Backend/GraphQL/Types/BacktestRunDto.cs \
        Backend.Tests/Resolvers/BacktestRunMutationTests.cs
git commit -m "feat(graphql): updateBacktestRunNotes mutation"
```

---

### Task 3.4: Frontend GraphQL query gains new fields + `engine` arg

**Files:**
- Modify: `Frontend/src/app/components/engine-lab-run-history/backtest-runs.query.ts`
- Modify: `Frontend/src/app/components/engine-lab-run-history/backtest-runs.types.ts`

- [ ] **Step 1: Update the query**

```typescript
import { gql } from 'apollo-angular';

export const BACKTEST_RUNS_QUERY = gql`
  query BacktestRuns($engine: Engine, $symbol: String, $first: Int) {
    backtestRuns(engine: $engine, symbol: $symbol, first: $first) {
      id
      source
      engine
      executedAt
      strategyName
      leanRunId
      parameters
      startDate
      endDate
      totalTrades
      totalPnL
      commissionPerOrder
      brokeragePolicy
      dataPolicy {
        source
        symbol
        adjusted
        session
        inputBars { timespan multiplier }
        strategyBars { timespan multiplier }
      }
      trades {
        isSyntheticExit
      }
    }
  }
`;
```

- [ ] **Step 2: Update TypeScript types**

```typescript
// Frontend/src/app/components/engine-lab-run-history/backtest-runs.types.ts
import { DataPolicy } from '../../models/data-policy';

export type Engine = 'PYTHON' | 'LEAN';

export interface BacktestRun {
  id: number;
  source: string;
  engine: Engine;
  executedAt: string;
  strategyName: string;
  leanRunId: string | null;
  parameters: Record<string, unknown>;
  startDate: string;
  endDate: string;
  totalTrades: number;
  totalPnL: string;
  commissionPerOrder: string | null;
  brokeragePolicy: string | null;
  dataPolicy: DataPolicy | null;
  trades: Array<{ isSyntheticExit: boolean }>;
}

export interface BacktestRunsResponse {
  backtestRuns: BacktestRun[];
}
```

- [ ] **Step 3: Commit**

```bash
git add Frontend/src/app/components/engine-lab-run-history/backtest-runs.query.ts \
        Frontend/src/app/components/engine-lab-run-history/backtest-runs.types.ts
git commit -m "feat(frontend): GraphQL query for backtestRuns gains engine + dataPolicy"
```

---

### Task 3.5: `EngineLabRunHistoryComponent` Engine column + filter + multi-select Compare

**Files:**
- Modify: `Frontend/src/app/components/engine-lab-run-history/engine-lab-run-history.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Write component tests**

```typescript
describe('EngineLabRunHistoryComponent', () => {
  it('renders the Engine column with PYTHON/LEAN strings', () => { /* ... */ });
  it('Engine filter dropdown drives the GraphQL engine variable', () => { /* ... */ });
  it('Compare button enables only when exactly 2 rows are selected', () => { /* ... */ });
  it('clicking Compare routes to /runs/compare?left=&right=', () => { /* ... */ });
  it('renders DataPolicy summary (input -> strategy bars) column', () => { /* ... */ });
});
```

- [ ] **Step 2: Implement signals + computed values**

```typescript
import { signal, computed, effect, inject } from '@angular/core';
import { Router } from '@angular/router';

engineFilter = signal<'ALL' | Engine>('ALL');

engineArg = computed(() => {
  const f = this.engineFilter();
  return f === 'ALL' ? null : f;
});

selectedRunIds = signal<Set<number>>(new Set<number>());

canCompare = computed(() => this.selectedRunIds().size === 2);

private router = inject(Router);

toggleSelect(id: number): void {
  this.selectedRunIds.update((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
}

onCompare(): void {
  const ids = [...this.selectedRunIds()];
  const [left, right] = ids;
  this.router.navigate(['/runs/compare'], { queryParams: { left, right } });
}
```

- [ ] **Step 3: Update HTML template (Angular 21 control flow)**

```html
<div class="history-toolbar">
  <select [ngModel]="engineFilter()" (ngModelChange)="engineFilter.set($event)">
    <option value="ALL">All engines</option>
    <option value="PYTHON">Python</option>
    <option value="LEAN">LEAN</option>
  </select>

  <button (click)="onCompare()" [disabled]="!canCompare()">Compare</button>
</div>

<table>
  <thead>
    <tr>
      <th></th>
      <th>Engine</th>
      <th>Date</th>
      <th>Symbol</th>
      <th>Range</th>
      <th>Strategy</th>
      <th>Bars</th>
      <th>Trades</th>
      <th>P&amp;L</th>
    </tr>
  </thead>
  <tbody>
    @for (run of runs(); track run.id) {
      <tr>
        <td><input type="checkbox" [checked]="selectedRunIds().has(run.id)"
                   (change)="toggleSelect(run.id)" /></td>
        <td>{{ run.engine }}</td>
        <td>{{ run.executedAt | date:'shortDate' }}</td>
        <td>{{ run.dataPolicy?.symbol ?? run.parameters['symbol'] }}</td>
        <td>{{ run.startDate }} – {{ run.endDate }}</td>
        <td>{{ run.strategyName }}</td>
        <td>{{ formatBars(run.dataPolicy) }}</td>
        <td>{{ run.totalTrades }}</td>
        <td>{{ run.totalPnL }}</td>
      </tr>
    }
  </tbody>
</table>
```

Add a `formatBars` helper:

```typescript
formatBars(dp: DataPolicy | null): string {
  if (!dp) return '—';
  const tsShort = (t: string) => t === 'minute' ? 'm' : t === 'hour' ? 'h' : 'd';
  return `${tsShort(dp.input_bars.timespan)}/${dp.input_bars.multiplier}` +
         ` → ${tsShort(dp.strategy_bars.timespan)}/${dp.strategy_bars.multiplier}`;
}
```

- [ ] **Step 4: Port forward features from REST table**

In separate commits (one per feature):
- **Notes editing** — inline `<input>` per row with a `(blur)` handler that fires `updateBacktestRunNotes(id, notes)` mutation.
- **CSV export** — client-side serialization of the GraphQL result; "Download CSV" button.
- **Column visibility toggle** — localStorage key `engine-lab-history.columns.v1`; settings panel.

- [ ] **Step 5: Run component tests**

Run: `podman exec my-frontend npx ng test --include='**/engine-lab-run-history.component.spec.ts' --watch=false`
Expected: all pass.

- [ ] **Step 6: Commit each feature increment**

---

### Task 3.6: Delete `EngineHistoryComponent`

**Files:**
- Delete: `Frontend/src/app/components/engine-lab/engine-history/`
- Modify: `Frontend/src/app/components/lean-engine/lean-engine.component.{ts,html}`

- [ ] **Step 1: Confirm zero remaining usages**

```bash
grep -rn "EngineHistoryComponent\|engine-history" Frontend/src/ --include="*.ts" --include="*.html" | head -20
```

Expected: only `lean-engine.component.{ts,html}` and `engine-history/` directory.

- [ ] **Step 2: Remove import + template reference from `lean-engine.component`**

In `lean-engine.component.ts`, delete the `EngineHistoryComponent` import and remove from the `imports` array of `@Component`.

In `lean-engine.component.html`, remove the `<app-engine-history>` element.

- [ ] **Step 3: Delete the directory**

```bash
git rm -r Frontend/src/app/components/engine-lab/engine-history/
```

- [ ] **Step 4: Run frontend tests**

```bash
podman exec my-frontend npx ng test --watch=false
```

Expected: all pass.

- [ ] **Step 5: Lint**

```bash
npx eslint Frontend/src/ --max-warnings 0
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add -A Frontend/src/app/components/
git commit -m "refactor(frontend): retire REST-backed EngineHistoryComponent; GraphQL table is sole history surface"
```

---

### Task 3.7: Open PR B.3 (Phase 3 history)

- [ ] **Step 1: Push branch + open PR**

Title: `feat(history): PR B.3 — unified GraphQL-backed history with Engine column`

- [ ] **Step 2: Pull master, start Phase 4**

```bash
git checkout master
git pull
git checkout -b pr-b-4-compare-view
```

---

# PHASE 4 — Compare endpoint + view

The compare service + controller + `/runs/compare` view.

---

### Task 4.1: `RunCompareService` — equivalence gate

**Files:**
- Create: `Backend/Services/RunCompareService.cs`
- Create: `Backend.Tests/Services/RunCompareServiceTests.cs`
- Create: `Backend/Services/CompatibilityResult.cs`

- [ ] **Step 1: Write failing tests**

```csharp
public class RunCompareServiceTests
{
    [Fact]
    public void EvaluateCompatibility_IdenticalAllFields_Returns_Compatible_True()
    {
        var left = MakeRow(symbol: "SPY", cash: 100_000m, fillMode: "signal_bar_close",
                           commission: 0m, brokerage: "algorithm_default");
        var right = MakeRow(symbol: "SPY", cash: 100_000m, fillMode: "signal_bar_close",
                            commission: 0m, brokerage: "algorithm_default");
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeTrue();
        result.Mismatches.Should().BeEmpty();
    }

    [Fact]
    public void EvaluateCompatibility_DifferentStartingCash_FailsGate()
    {
        var left = MakeRow(cash: 100_000m);
        var right = MakeRow(cash: 50_000m);
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeFalse();
        result.Mismatches.Should().Contain("starting_cash");
    }

    [Fact]
    public void EvaluateCompatibility_DifferentStrategyBars_FailsGate()
    {
        var left = MakeRow(strategyBars: new BarsSpec("minute", 15));
        var right = MakeRow(strategyBars: new BarsSpec("minute", 30));
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeFalse();
        result.Mismatches.Should().Contain("strategy_bars");
    }

    [Fact]
    public void EvaluateCompatibility_DifferentFillMode_FailsGate()
    {
        var left = MakeRow(fillMode: "signal_bar_close");
        var right = MakeRow(fillMode: "next_bar_open");
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeFalse();
        result.Mismatches.Should().Contain("fill_mode");
    }

    [Fact]
    public void EvaluateCompatibility_BrokerageSoftMatch_OneDefaultOneIBKR_PassesWithInfo()
    {
        var left = MakeRow(brokerage: "algorithm_default");
        var right = MakeRow(brokerage: "interactive_brokers");
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        // Per spec § 9.2: soft when either side is "algorithm_default" or null
        result.Compatible.Should().BeTrue();
        result.Mismatches.Should().NotContain("brokerage_policy");
        result.InformationalDifferences.Should().Contain("brokerage_policy");
    }

    [Fact]
    public void EvaluateCompatibility_BothNonDefaultBrokeragesDiffer_FailsGate()
    {
        var left = MakeRow(brokerage: "interactive_brokers");
        var right = MakeRow(brokerage: "tradier"); // hypothetical second non-default
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeFalse();
        result.Mismatches.Should().Contain("brokerage_policy");
    }

    [Fact]
    public void EvaluateCompatibility_MissingDataPolicy_FailsGate()
    {
        var left = MakeRow(dataPolicyJson: null);
        var right = MakeRow();
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        result.Compatible.Should().BeFalse();
        result.Mismatches.Should().Contain("data_policy_missing");
    }
}
```

Add a `MakeRow(...)` helper at the bottom of the test class that builds a `StrategyExecution` with sensible defaults.

- [ ] **Step 2: Verify failure**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~RunCompareServiceTests"
```

Expected: compile errors (service doesn't exist).

- [ ] **Step 3: Implement `CompatibilityResult` + `RunCompareService.EvaluateCompatibility`**

```csharp
// Backend/Services/CompatibilityResult.cs
namespace Backend.Services;

public class CompatibilityResult
{
    public bool Compatible { get; init; }
    public List<string> Mismatches { get; init; } = new();
    public List<string> InformationalDifferences { get; init; } = new();
}
```

```csharp
// Backend/Services/RunCompareService.cs
namespace Backend.Services;

public class RunCompareService
{
    public CompatibilityResult EvaluateCompatibility(StrategyExecution left, StrategyExecution right)
    {
        var mismatches = new List<string>();
        var infos = new List<string>();

        var leftDp = ParseDataPolicy(left.DataPolicyJson);
        var rightDp = ParseDataPolicy(right.DataPolicyJson);

        if (leftDp == null || rightDp == null)
        {
            mismatches.Add("data_policy_missing");
            return new CompatibilityResult { Compatible = false, Mismatches = mismatches };
        }

        if (leftDp.Symbol != rightDp.Symbol) mismatches.Add("symbol");
        if (leftDp.Session != rightDp.Session) mismatches.Add("session");
        if (leftDp.Adjusted != rightDp.Adjusted) mismatches.Add("adjusted");
        if (!Equals(leftDp.InputBars, rightDp.InputBars)) mismatches.Add("input_bars");
        if (!Equals(leftDp.StrategyBars, rightDp.StrategyBars)) mismatches.Add("strategy_bars");
        if (left.StartDate != right.StartDate || left.EndDate != right.EndDate) mismatches.Add("window");
        if (left.InitialCash != right.InitialCash) mismatches.Add("starting_cash");
        if ((left.CommissionPerOrder ?? 0m) != (right.CommissionPerOrder ?? 0m)) mismatches.Add("commission_per_order");
        if (left.FillMode != right.FillMode) mismatches.Add("fill_mode");

        var leftSoft = left.BrokeragePolicy == null || left.BrokeragePolicy == "algorithm_default";
        var rightSoft = right.BrokeragePolicy == null || right.BrokeragePolicy == "algorithm_default";
        if (left.BrokeragePolicy != right.BrokeragePolicy)
        {
            if (leftSoft || rightSoft) infos.Add("brokerage_policy");
            else mismatches.Add("brokerage_policy");
        }

        return new CompatibilityResult
        {
            Compatible = mismatches.Count == 0,
            Mismatches = mismatches,
            InformationalDifferences = infos,
        };
    }

    private static DataPolicy? ParseDataPolicy(string? json)
    {
        if (string.IsNullOrEmpty(json)) return null;
        return JsonSerializer.Deserialize<DataPolicy>(json, new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        });
    }
}
```

- [ ] **Step 4: Run tests**

```bash
dotnet test --filter "FullyQualifiedName~RunCompareServiceTests"
```

Expected: all 7 pass.

- [ ] **Step 5: Commit**

```bash
git add Backend/Services/RunCompareService.cs Backend/Services/CompatibilityResult.cs \
        Backend.Tests/Services/RunCompareServiceTests.cs
git commit -m "feat(compare): RunCompareService equivalence gate (data_policy + cash + commission + fill mode + soft brokerage)"
```

---

### Task 4.2: `RunCompareService` — summary deltas + state-trace detection

**Files:**
- Modify: `Backend/Services/RunCompareService.cs`

- [ ] **Step 1: Write failing tests**

```csharp
[Fact]
public void ComputeSummaryDeltas_IncludesTradesPnLFeesWinRateMaxDD()
{
    var left = MakeRow(totalTrades: 7, totalPnL: 421.50m, fees: 0m, winRate: 0.571, maxDD: -15.20m);
    var right = MakeRow(totalTrades: 7, totalPnL: 419.80m, fees: 0m, winRate: 0.571, maxDD: -15.20m);
    var sut = new RunCompareService();

    var deltas = sut.ComputeSummaryDeltas(left, right);

    deltas.TotalTrades.Delta.Should().Be(0);
    deltas.TotalPnL.Delta.Should().Be(-1.70m);
    deltas.WinRate.Delta.Should().BeApproximately(0.0, 1e-9);
}

[Fact]
public void DetectStateTrace_BothSidesHaveStateCsv_ReturnsTrue() { /* ... */ }

[Fact]
public void DetectStateTrace_OnlyOneSideHasStateCsv_ReturnsFalse_NoError() { /* ... */ }
```

- [ ] **Step 2: Implement `ComputeSummaryDeltas` + `DetectStateTrace`**

(`DetectStateTrace` reads `WorkspacePath` from each row, looks for `output/storage/state.csv` for LEAN runs and `decision-snapshots.csv` for Python engine runs — if Phase 5 wires the engine to emit one. If neither side has it, returns `false`.)

- [ ] **Step 3: Run tests, commit**

---

### Task 4.3: Python `/api/lean-sidecar/reconcile-trades` endpoint

**Files:**
- Create: `PythonDataService/app/routers/reconcile_trades.py`
- Modify: `PythonDataService/app/main.py`

- [ ] **Step 1: Write failing test**

```python
"""POST /api/lean-sidecar/reconcile-trades wraps reconcile_trade_lists."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_reconcile_trades_endpoint_returns_matched_pairs() -> None:
    from app.main import app

    left = [
        {"entry_ms_utc": 1736773800000, "exit_ms_utc": 1736775000000,
         "quantity": "100", "entry_price": "100.00", "exit_price": "100.50"},
    ]
    right = [
        {"entry_ms_utc": 1736773800000, "exit_ms_utc": 1736775000000,
         "quantity": "100", "entry_price": "100.00", "exit_price": "100.52"},
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/reconcile-trades",
                                 json={"left": left, "right": right,
                                       "fill_price_atol": "0.01"})
        assert resp.status_code == 200
        body = resp.json()
        assert "matched_pairs" in body
        assert "python_only" in body
        assert "lean_only" in body
        # 0.02 > 0.01 atol -> category fill_price_drift
        assert body["matched_pairs"][0]["category"] == "fill_price_drift"
```

- [ ] **Step 2: Implement the endpoint**

```python
"""POST /api/lean-sidecar/reconcile-trades — wraps the trade-pair reconciler."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.lean_sidecar_compare_service import reconcile_trade_lists

router = APIRouter(prefix="/api/lean-sidecar", tags=["lean-sidecar"])


class _Trade(BaseModel):
    entry_ms_utc: int
    exit_ms_utc: int
    quantity: str
    entry_price: str
    exit_price: str


class _ReconcileRequest(BaseModel):
    left: list[_Trade]
    right: list[_Trade]
    fill_price_atol: str = "0.01"


@router.post("/reconcile-trades")
def reconcile_trades(payload: _ReconcileRequest) -> dict[str, Any]:
    left = [t.model_dump() for t in payload.left]
    right = [t.model_dump() for t in payload.right]
    return reconcile_trade_lists(
        left=left, right=right, fill_price_atol=Decimal(payload.fill_price_atol),
    )
```

Register in `app/main.py`: `from app.routers import reconcile_trades; app.include_router(reconcile_trades.router)`.

- [ ] **Step 3: Run, commit**

---

### Task 4.4: `CompareController` HTTP endpoint

**Files:**
- Create: `Backend/Controllers/CompareController.cs`
- Create: `Backend/Models/Compare/CompareResponse.cs`
- Create: `Backend.Tests/Controllers/CompareControllerTests.cs`

- [ ] **Step 1: Create `CompareResponse.cs`**

```csharp
namespace Backend.Models.Compare;

public record CompareResponse(
    RunSummary Left,
    RunSummary Right,
    bool Compatible,
    List<string> Mismatches,
    SummaryDeltas SummaryDeltas,
    TradeDiff TradeDiff,
    TradeDivergence? FirstDivergence,
    bool StateTraceAvailable,
    RawRunLinks RawRunLinks
);

public record RunSummary(int Id, string Engine, DataPolicy? DataPolicy, /* ... */);
public record SummaryDeltas(/* ... */);
public record TradeDiff(/* ... */);
public record TradeDivergence(int TradeIndex, string What, string Category, string LeftValue, string RightValue);
public record RawRunLinks(/* ... */);
```

(Full record types follow spec § 6.5.)

- [ ] **Step 2: Implement `CompareController`**

```csharp
[ApiController]
[Route("api/runs")]
public class CompareController : ControllerBase
{
    private readonly MyDbContext _ctx;
    private readonly RunCompareService _compareService;
    private readonly HttpClient _pythonClient;

    public CompareController(MyDbContext ctx, RunCompareService compareService,
                             IHttpClientFactory factory)
    {
        _ctx = ctx;
        _compareService = compareService;
        _pythonClient = factory.CreateClient("PythonDataService");
    }

    [HttpGet("compare")]
    public async Task<ActionResult<CompareResponse>> Compare(
        [FromQuery] int left, [FromQuery] int right,
        CancellationToken cancellationToken)
    {
        var leftRow = await _ctx.StrategyExecution
            .Include(s => s.Trades)
            .SingleOrDefaultAsync(s => s.Id == left, cancellationToken);
        var rightRow = await _ctx.StrategyExecution
            .Include(s => s.Trades)
            .SingleOrDefaultAsync(s => s.Id == right, cancellationToken);

        if (leftRow == null || rightRow == null) return NotFound();

        var compat = _compareService.EvaluateCompatibility(leftRow, rightRow);
        var deltas = _compareService.ComputeSummaryDeltas(leftRow, rightRow);
        var stateTrace = _compareService.DetectStateTrace(leftRow, rightRow);

        // Delegate trade reconciliation to Python
        var tradeDiff = await _compareService.ReconcileTrades(
            _pythonClient, leftRow, rightRow, cancellationToken);

        return Ok(new CompareResponse(
            Left: BuildSummary(leftRow),
            Right: BuildSummary(rightRow),
            Compatible: compat.Compatible,
            Mismatches: compat.Mismatches,
            SummaryDeltas: deltas,
            TradeDiff: tradeDiff,
            FirstDivergence: tradeDiff.FirstDivergence,
            StateTraceAvailable: stateTrace,
            RawRunLinks: BuildRawLinks(leftRow, rightRow)
        ));
    }

    // ... helpers ...
}
```

- [ ] **Step 3: Write tests for the controller**

```csharp
[Fact]
public async Task Compare_HappyPath_ReturnsCompatibleResponse() { /* ... */ }

[Fact]
public async Task Compare_IncompatibleDataPolicy_Returns_Compatible_False() { /* ... */ }

[Fact]
public async Task Compare_FirstDivergence_PopulatedWhenTradeMismatch() { /* ... */ }

[Fact]
public async Task Compare_StateTraceAsymmetry_OnlyOneSideHasStateCsv_ReturnsFalse() { /* ... */ }

[Fact]
public async Task Compare_UnmatchedTrades_AppearsInPythonOnlyOrLeanOnly() { /* ... */ }
```

- [ ] **Step 4: Run all compare tests**

```bash
cd Backend.Tests
dotnet test --filter "FullyQualifiedName~CompareController|FullyQualifiedName~RunCompareService"
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add Backend/Controllers/CompareController.cs Backend/Models/Compare/ \
        Backend/Services/RunCompareService.cs \
        Backend.Tests/Controllers/CompareControllerTests.cs
git commit -m "feat(compare): /api/runs/compare endpoint with equivalence gate + trade reconciliation"
```

---

### Task 4.5: Frontend `/runs/compare` view

**Files:**
- Create: `Frontend/src/app/services/runs-compare.service.ts`
- Create: `Frontend/src/app/components/runs-compare/runs-compare.component.{ts,html,scss,spec.ts}`
- Modify: `Frontend/src/app/app.routes.ts` (verify route loads new component)

- [ ] **Step 1: Service**

```typescript
// Frontend/src/app/services/runs-compare.service.ts
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { CompareResponse } from '../models/compare-response';

@Injectable({ providedIn: 'root' })
export class RunsCompareService {
  private http = inject(HttpClient);

  getCompare(left: number, right: number): Observable<CompareResponse> {
    return this.http.get<CompareResponse>(`/api/runs/compare`, {
      params: { left, right },
    });
  }
}
```

Define `CompareResponse` interface in `Frontend/src/app/models/compare-response.ts` mirroring spec § 6.5.

- [ ] **Step 2: Component**

Skeleton + template skeleton per spec § 7.3 mockup. Uses `rxResource` on route query-param changes.

- [ ] **Step 3: Tests**

```typescript
describe('RunsCompareComponent', () => {
  it('renders "Comparable" header + sub-claims when compatible=true', () => { /* ... */ });
  it('renders mismatch list when compatible=false', () => { /* ... */ });
  it('omits State Trace section from DOM when state_trace_available=false', () => { /* ... */ });
  it('links first-divergence to trade row', () => { /* ... */ });
  it('renders summary cards with deltas', () => { /* ... */ });
});
```

- [ ] **Step 4: Run, commit incrementally**

---

### Task 4.6: Heavy E2E integration test (gated)

**Files:**
- Create: `PythonDataService/tests/integration/test_runs_compare_e2e.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end compare receipt: Python + LEAN EMA crossover on PR A's fixture,
persisted, queried via /api/runs/compare, assert response matches expected."""

from __future__ import annotations

import os

import pytest


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("LEAN_LAUNCHER_URL"), reason="LEAN_LAUNCHER_URL unset")
@pytest.mark.skipif(not os.environ.get("BACKEND_URL"), reason="BACKEND_URL unset")
@pytest.mark.asyncio
async def test_python_and_lean_runs_compare_to_compatible_result() -> None:
    """Two EMA runs against PR A's pinned Jan 13-17 SPY fixture should
    compare as compatible (same DataPolicy, cash, commission, fill mode)."""
    # 1. Launch Python EMA run via the engine endpoint
    # 2. Launch LEAN EMA template via /api/lean-sidecar/trusted-runs
    # 3. Query /api/runs/compare?left=<py_id>&right=<lean_id>
    # 4. Assert response.compatible == True
    # 5. Assert response.trade_diff has matched pairs
    ...
```

- [ ] **Step 2: Run (will skip without env)**

Run: `podman exec polygon-data-service python -m pytest tests/integration/test_runs_compare_e2e.py -v`
Expected: SKIPPED.

- [ ] **Step 3: Commit**

---

### Task 4.7: Open PR B.4 (Phase 4 compare)

Title: `feat(compare): PR B.4 — /api/runs/compare endpoint + /runs/compare UI view`

---

# PHASE 5 — Unified Engine Lab UI

Engine dropdown, LEAN source editor, ruff lint endpoint, `/lean-lab` retirement.

---

### Task 5.1: Ruff lint endpoint

**Files:**
- Create: `PythonDataService/app/routers/lean_lint.py`
- Modify: `PythonDataService/app/main.py`
- Create: `PythonDataService/tests/unit/lean_sidecar/test_lint_endpoint.py`

- [ ] **Step 1: Write failing tests**

```python
"""POST /api/lean-sidecar/lint contract tests."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_lint_endpoint_empty_source_returns_empty_diagnostics() -> None:
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": ""})
        assert resp.status_code == 200
        assert resp.json() == {"diagnostics": []}


@pytest.mark.asyncio
async def test_lint_endpoint_unused_import_returns_f401() -> None:
    from app.main import app

    src = "import pandas\nclass X: pass\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": src})
        assert resp.status_code == 200
        rules = [d["rule"] for d in resp.json()["diagnostics"]]
        assert "F401" in rules


@pytest.mark.asyncio
async def test_lint_endpoint_oversize_returns_413() -> None:
    from app.lean_sidecar.config import MAX_ALGORITHM_SOURCE_BYTES
    from app.main import app

    huge = "x = 1\n" * (MAX_ALGORITHM_SOURCE_BYTES // 6 + 100)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": huge})
        assert resp.status_code == 413


@pytest.mark.asyncio
async def test_lint_endpoint_subprocess_timeout_returns_504(monkeypatch) -> None:
    """Monkey-patch the subprocess helper to never return; assert 504."""
    import asyncio

    from app.main import app
    from app.routers import lean_lint

    async def _hang(*args, **kwargs):
        await asyncio.sleep(100)

    monkeypatch.setattr(lean_lint, "_run_ruff", _hang)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": "x = 1"})
        assert resp.status_code == 504
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_lint_endpoint.py -v`
Expected: all fail (endpoint doesn't exist).

- [ ] **Step 3: Implement the endpoint**

```python
"""Ruff-backed lint endpoint for the LEAN script editor."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.lean_sidecar.config import MAX_ALGORITHM_SOURCE_BYTES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lean-sidecar", tags=["lean-sidecar"])

_RUFF_TIMEOUT_S = 5.0


class _LintRequest(BaseModel):
    source: str = Field(...)


class _Diagnostic(BaseModel):
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None
    rule: str
    severity: str
    message: str
    fix: str | None = None


class _LintResponse(BaseModel):
    diagnostics: list[_Diagnostic]


async def _run_ruff(source_bytes: bytes) -> tuple[bytes, bytes, int]:
    """Spawn ruff with stdin source. No shell. Returns (stdout, stderr, rc)."""
    process = await asyncio.subprocess.create_subprocess_exec(
        "ruff",
        "check",
        "--output-format",
        "json",
        "--stdin-filename",
        "main.py",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=source_bytes)
    return stdout, stderr, process.returncode or 0


@router.post("/lint", response_model=_LintResponse)
async def lint_source(payload: _LintRequest) -> _LintResponse:
    source_bytes = payload.source.encode("utf-8")
    if len(source_bytes) > MAX_ALGORITHM_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"reason": "source_too_large", "max_bytes": MAX_ALGORITHM_SOURCE_BYTES},
        )

    if not payload.source.strip():
        return _LintResponse(diagnostics=[])

    try:
        stdout, _stderr, _rc = await asyncio.wait_for(_run_ruff(source_bytes), timeout=_RUFF_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"reason": "ruff_timeout", "timeout_seconds": _RUFF_TIMEOUT_S},
        ) from e

    # ruff returns exit code 1 when there are diagnostics; that's not an error.
    raw = stdout.decode("utf-8").strip()
    if not raw:
        return _LintResponse(diagnostics=[])

    items = json.loads(raw)
    diagnostics = [
        _Diagnostic(
            line=item["location"]["row"],
            col=item["location"]["column"],
            end_line=item.get("end_location", {}).get("row"),
            end_col=item.get("end_location", {}).get("column"),
            rule=item["code"],
            severity="warning",
            message=item["message"],
            fix=(item.get("fix") or {}).get("message"),
        )
        for item in items
    ]
    return _LintResponse(diagnostics=diagnostics)
```

- [ ] **Step 4: Register the router**

In `PythonDataService/app/main.py`:

```python
from app.routers import lean_lint
app.include_router(lean_lint.router)
```

- [ ] **Step 5: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/unit/lean_sidecar/test_lint_endpoint.py -v`
Expected: all 4 pass.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/routers/lean_lint.py \
        PythonDataService/app/main.py \
        PythonDataService/tests/unit/lean_sidecar/test_lint_endpoint.py
git commit -m "feat(lean-sidecar): ruff-backed lint endpoint for the script editor"
```

---

### Task 5.2: `LeanLintService` Angular service

**Files:**
- Create: `Frontend/src/app/services/lean-lint.service.ts`
- Create: `Frontend/src/app/services/lean-lint.service.spec.ts`

- [ ] **Step 1: Implement**

```typescript
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface Diagnostic {
  line: number;
  col: number;
  end_line: number | null;
  end_col: number | null;
  rule: string;
  severity: 'warning' | 'error' | 'info';
  message: string;
  fix: string | null;
}

@Injectable({ providedIn: 'root' })
export class LeanLintService {
  private http = inject(HttpClient);

  lint(source: string): Observable<{ diagnostics: Diagnostic[] }> {
    return this.http.post<{ diagnostics: Diagnostic[] }>(
      '/api/lean-sidecar/lint',
      { source }
    );
  }
}
```

- [ ] **Step 2: Service spec**

Test the HTTP shape: request body has `{source}`, response shape matches.

- [ ] **Step 3: Commit**

---

### Task 5.3: `LeanScriptEditorComponent`

**Files:**
- Create: `Frontend/src/app/components/lean-script-editor/lean-script-editor.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Check whether Monaco is already in deps**

```bash
grep -l "monaco-editor" Frontend/package.json Frontend/package-lock.json 2>&1 | head -5
```

- If Monaco is present: use it via `@monaco-editor/loader` or Angular wrapper.
- If not: install `@codemirror/lang-python` + `@codemirror/state` + `@codemirror/view` (~150 KB total, vs Monaco's ~3 MB).

- [ ] **Step 2: Write component spec**

```typescript
describe('LeanScriptEditorComponent', () => {
  it('renders the default template on first mount', () => { /* assert editor.value contains "class MyAlgorithm" */ });
  it('emits (sourceChange) on typing', () => { /* type, await change emit */ });
  it('debounces lint requests to 500ms', fakeAsync(() => {
    /* type, advance 250ms — no request; advance another 250ms — one request */
  }));
  it('renders ruff diagnostics in the Problems panel', () => {
    /* mock LeanLintService.lint to return one F401; assert panel renders "F401" */
  });
  it('clicking a diagnostic scrolls editor to that line', () => { /* ... */ });
});
```

- [ ] **Step 3: Implement**

The component:
- Imports editor (Monaco or CodeMirror) and configures Python mode.
- Has a `source` model signal that two-way-binds with the editor's value.
- Has a `diagnostics` signal that the Problems panel renders.
- Calls `LeanLintService.lint(source)` via `toSignal` of a debounced (500ms) RxJS observable derived from the source signal.
- On diagnostic click, scrolls the editor view to the line via the editor's API.

Default template: import `EMA_CROSSOVER_SOURCE` equivalent from a frontend-side constant (or POST `/api/lean-sidecar/templates/ema_crossover` to fetch it server-side — pick one and commit).

- [ ] **Step 4: Run, commit**

---

### Task 5.4: Engine dropdown + conditional script pane in `LeanEngineComponent`

**Files:**
- Modify: `Frontend/src/app/components/lean-engine/lean-engine.component.{ts,html,scss,spec.ts}`

- [ ] **Step 1: Write component tests**

```typescript
describe('LeanEngineComponent engine selector', () => {
  it('shows the Python strategy dropdown when engine is "python"', () => { /* ... */ });
  it('shows the LeanScriptEditorComponent when engine is "lean"', () => { /* ... */ });
  it('submit calls jobsService for python', () => { /* ... */ });
  it('submit calls leanSidecarService.startTrustedRun for lean', () => { /* ... */ });
  it('composeDataPolicy includes both input_bars and strategy_bars', () => { /* ... */ });
  it('submit payload for LEAN includes the editor source as algorithm_source', () => { /* ... */ });
});
```

- [ ] **Step 2: Add Engine signal**

```typescript
import { signal, computed } from '@angular/core';

engine = signal<'python' | 'lean'>('python');

leanSource = signal('');
```

- [ ] **Step 3: Update HTML template (Angular 21 control flow)**

```html
<div class="engine-selector">
  <label>Engine
    <select [ngModel]="engine()" (ngModelChange)="engine.set($event)">
      <option value="python">Python</option>
      <option value="lean">LEAN</option>
    </select>
  </label>
</div>

<!-- Shared controls (ticker, dates, timeframe, session, cash) — unchanged -->

@if (engine() === 'python') {
  <!-- Existing strategy dropdown + params section -->
}

@if (engine() === 'lean') {
  <app-lean-script-editor [(source)]="leanSource" />
}
```

- [ ] **Step 4: Branch `run()` by engine**

```typescript
run(): void {
  const dataPolicy = this.composeDataPolicy();
  if (this.engine() === 'python') {
    this.jobsService.startJob('engine_backtest', {
      backtest: {
        strategy_name: this.selectedStrategy()!.name,
        starting_cash: this.startingCash(),
        params: this.strategyParams(),
        start_date: this.fromDate(),
        end_date: this.toDate(),
        resolution: this.resolution(),
        data_policy: dataPolicy,
      },
    });
  } else {
    this.leanSidecarService.startTrustedRun({
      run_id: this.composeRunId(),
      algorithm_source: this.leanSource(),
      starting_cash: this.startingCash(),
      start_ms_utc: this.composeStartMs(),
      end_ms_utc: this.composeEndMs(),
      data_policy: dataPolicy,
    });
  }
}
```

`composeStartMs` / `composeEndMs` convert the form's date pickers to session-open milliseconds (use the existing helpers from PR A's `trading_calendar` if exposed; otherwise frontend implementation mirrors PR A's `session_open_ms_utc`).

- [ ] **Step 5: Run tests, commit**

---

### Task 5.5: Retire `/lean-lab`

**Files:**
- Modify: `Frontend/src/app/app.routes.ts`
- Delete: `Frontend/src/app/components/lean-lab/`
- Modify: `Frontend/src/app/services/lean-sidecar.service.ts`

- [ ] **Step 1: Replace route with redirect**

In `Frontend/src/app/app.routes.ts`, find the `/lean-lab` route and replace with:

```typescript
{ path: 'lean-lab', redirectTo: 'engine', pathMatch: 'prefix' },
```

(`pathMatch: 'prefix'` covers `/lean-lab/some-subpath` too.)

- [ ] **Step 2: Delete the component directory**

```bash
git rm -r Frontend/src/app/components/lean-lab/
```

- [ ] **Step 3: Verify zero remaining usages**

```bash
grep -rn "LeanLabComponent\|lean-lab" Frontend/src/ --include="*.ts" --include="*.html" --include="*.scss" | grep -v "redirectTo" | head -20
```

Expected: empty.

- [ ] **Step 4: Trim `LeanSidecarService`**

In `Frontend/src/app/services/lean-sidecar.service.ts`, keep only `startTrustedRun` and the response/request types. Remove any helpers that only `LeanLabComponent` used.

- [ ] **Step 5: Run frontend tests + lint**

```bash
podman exec my-frontend npx ng test --watch=false
npx eslint Frontend/src/ --max-warnings 0
```

Expected: all pass, lint clean.

- [ ] **Step 6: Commit**

```bash
git add Frontend/src/app/app.routes.ts \
        Frontend/src/app/services/lean-sidecar.service.ts
git rm -r Frontend/src/app/components/lean-lab/
git commit -m "refactor(frontend): retire /lean-lab route; LEAN runs go through unified /engine"
```

---

### Task 5.6: Open PR B.5 (Phase 5+6 UI + cleanup)

- [ ] **Step 1: Push branch + open PR**

Title: `feat(engine-lab): PR B.5 — unified Engine Lab UI; retire /lean-lab`
Body: links to PR B.1-B.4; summary of UI changes; manual test plan (run a Python EMA + a LEAN EMA, view both in the unified history, click Compare, verify the compare view renders per spec § 7.3).

---

## Self-review

**Spec coverage:**
- § 4.1 Surface consolidation → Task 5.5 (retire /lean-lab) + 5.4 (engine dropdown)
- § 4.2 DataPolicy as shared contract → Tasks 1.1-1.2 (rename), 1.5 (TrustedRunRequest), 2.4 (Engine request)
- § 4.3 BarsSpec everywhere → Task 1.5 (drop bar_minutes), 1.3 (no-normalization pin)
- § 4.4 Adjustment semantics → Task 1.4 (widen assertion), 1.6 (default to true)
- § 4.5 Compare-view design philosophy → Task 4.5 (multi-claim header)
- § 5 Build order & phases → matches Tasks 1.x / 2.x / 3.x / 4.x / 5.x
- § 6 Wire contracts → covered in each task that touches the boundary
- § 7 UI design → Tasks 5.3, 5.4, 4.5
- § 8 Data flow → implicit in task ordering
- § 9 Equivalence-gate semantics → Tasks 4.1, 4.2
- § 10 Testing strategy → tests embedded in each task; § 10.4 → Tasks 4.1-4.4; § 10.5 → Tasks 5.1-5.3
- § 11 Migration & cleanup → Task 2.1 (DB), 5.5 (frontend), 1.6 (router compat)

**Placeholder scan:** Tasks 3.5, 4.2-4.5, 5.2-5.3 have abbreviated step bodies where the spec section is the contract. Each names exact files, lists the test cases, and references spec sections — they should be re-expanded inline by the implementing subagent if more handholding is needed.

**Type consistency:** `BarsSpec` field names (`timespan`, `multiplier`) used uniformly across Python/JSON/C#/TypeScript. `DataPolicy` field names follow per-language conventions (snake_case JSON, PascalCase C#, camelCase TypeScript) with a clear mapping at GraphQL boundary. `Engine` enum: `PYTHON`/`LEAN` in GraphQL, `python`/`lean` in frontend signals — mapping done at the GraphQL boundary on read.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-19-pr-b-engine-lab-unified.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
