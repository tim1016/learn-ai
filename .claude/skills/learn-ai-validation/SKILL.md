---
name: learn-ai-validation
description: Enforce the Math Provenance Contract across learn-ai's three layers. Use when adding or touching any mathematical function (indicator, Greek, pricing, backtest statistic, valuation arithmetic), when writing or reviewing a parity test, when adding a new canonical implementation, or when a reviewer asks "how do I know this number is right?" Also use when working in Frontend/Backend/PythonDataService and you need the layer-specific non-negotiables without reading every rule file. Auto-trigger on: `np.allclose`, `decimal.Decimal` in a computation, new `*.py` in `PythonDataService/app/engine/`, `bs_`/`black_scholes`/`greek`/`iv_` identifiers, new backtest or strategy file, new resolver or service computing a scalar. Escape: do not auto-trigger on UI formatting, display rounding, `toFixed`, or `DatePipe`.
---

# learn-ai validation skill

## Purpose (verbatim, do not drift)

Prevent drift between mathematical claims, their implementations, their tests, and their external references across the three-layer learn-ai stack. Produce a codebase that a quant reviewer or scientific reader can audit without talking to the author.

## The bedrock rule — Math Provenance Contract

**Every mathematical function in this repo must carry a provenance block with four fields.** No block = not merged. The fields are grep-able; CI can enforce them later.

```
Formula              — the math it computes (one line, symbolic or named)
Reference            — paper section, textbook, or authoritative URL (no "various sources")
Canonical implementation — this file, OR a pointer to the canonical file elsewhere
Validated against    — the test file that proves equivalence to the Reference
```

"Mathematical function" means: anything that returns a number a user will compare against another number. Display formatting, `toFixed`, `DatePipe`, axis labels, and pass-through DTOs are **not** math functions and do not need a block.

### Format per language

**Python** (module or class docstring — on the definition that a user can grep for):

```python
"""SimpleMovingAverage.

Formula: SMA(n) = (1/n) · Σ x_{t-i}, i ∈ [0, n-1]
Reference: LEAN Indicators/SimpleMovingAverage.cs (commit pinned in
  references/lean-engine/COMMIT.txt)
Canonical implementation: this file.
Validated against: tests/test_indicators.py::test_sma_matches_lean_golden
"""
```

**TypeScript** (JSDoc above the exported symbol):

```typescript
/**
 * Black-Scholes call price (legacy UI-only pricer).
 *
 * Formula: C = S·N(d1) − K·e^(−rT)·N(d2)
 * Reference: Hull, Options Futures and Other Derivatives (10e), §15.8
 * Canonical implementation: PythonDataService/app/services/quantlib_pricer.py
 *   (this file is a UI-side legacy implementation retained for render speed)
 * Validated against: Frontend/src/app/utils/black-scholes.spec.ts — parity
 *   test against QuantLib via GraphQL `priceOption` resolver
 */
```

**C#** (XML doc on the method, not the class, unless the class has one arithmetic method):

```csharp
/// <summary>
/// Formula: MaxDrawdown = max_t(running_peak_t − equity_t) / running_peak_t
/// Reference: Bacon, Practical Portfolio Performance Measurement (2e), §8.2
/// Canonical implementation: this file (no other layer computes drawdown).
///   Tracked in docs/math-sources-of-truth.md.
/// Validated against: Backend.Tests/Unit/Services/BacktestServiceTests.cs
///   (service-level only, no parity fixture yet).
/// </summary>
```

### Scope of enforcement

- **New math**: block required on first commit. Missing block = block merge.
- **Touched math**: if you edit a math function and the block is missing or stale, add/update it. Touching = changing the numerical behavior OR the signature.
- **Untouched legacy**: do NOT backfill every existing math file today. The registry (`docs/math-sources-of-truth.md`) tracks legacy debt; burn it down as you touch it.

### Single source of truth

There must be **one** canonical implementation per math concept — duplicate implementations across layers create silent drift. The provenance block makes this explicit:

- If you're computing a number that already exists elsewhere, your `Canonical implementation` field either (a) points at the existing file (and your file calls it via a service), or (b) names this file as the new canonical and the prior implementation is replaced or marked as a parity-tested mirror.
- A "parity-tested mirror" is acceptable when latency, layer-locality, or vendor parity genuinely demands a copy (e.g., a UI-side Black-Scholes for instant payoff curves; a vendor-shape DTO that re-derives a derived field). It must carry a parity test naming the canonical file in `Validated against`, so drift is provable.
- Math may live in Python, .NET, Frontend, or anywhere else that fits the use case — pick the layer that makes the system simpler. The single-source-of-truth principle is independent of where that source lives.

## Layer detection

When work touches a file under:

- `PythonDataService/` → Python layer. Historical home for shared math (indicators, Greeks, backtest stats), exposed via FastAPI. Most legacy canonicals live here, and adding new shared math here keeps the call graph simple.
- `Backend/` → .NET layer. Hosts GraphQL, auth, persistence; computes numbers when latency or layer-locality demands it (e.g., simple aggregations close to the DB, or fast paths that avoid a Python round-trip). If a number you're adding already exists in another layer, default to calling the canonical instead of duplicating.
- `Frontend/` → Angular layer. Visualization plus interactive math when round-trip latency would hurt UX (e.g., live payoff curves, what-if scenarios). Duplicates of math that lives elsewhere should carry a parity test.

## Inline critical rules (Desktop-portable — Claude Code can also read the full rule files)

These are the rules most likely to be violated; full text is in `.claude/rules/`. When editing in that layer, obey these first; consult the full rule file for anything that's unclear.

### Numerical (applies to every layer that computes a number)

1. **No `np.allclose(a, b)` without explicit `atol` and `rtol`.** Defaults are a bug. Standard: `atol=1e-9, rtol=0` for indicators; `atol=1e-6` for accumulated PnL; `atol=1e-6, rtol=1e-6` for Greeks.
2. **Golden fixtures live in `PythonDataService/tests/fixtures/golden/<name>/`** with `input`, `output`, and an attribution file citing reference source + commit SHA + regeneration command. Never hand-edit a fixture.
3. **Regenerating a golden fixture needs a commit-message justification** (e.g., "reference upgraded from commit abc123 to def456, new fixture captures upstream bug fix"). If you're regenerating to make a test pass, stop and diagnose instead.
4. **Do not loosen tolerances to make a test pass.** Classify the divergence (`reconcile-backtest` taxonomy), fix the root cause. Loosening is acceptable only for floating-point-accumulation divergence that's small relative to the meaningful range, and must be documented in the test and in `docs/references/<name>.md`.

### Timestamps (applies everywhere, cross-layer)

5. **`int64 ms UTC` is the only wire and storage format.** ISO strings, `DateTime`, `pd.Timestamp`, `Date` are allowed only as in-function locals; convert back to `int64 ms` before returning, persisting, or serializing.
6. **Bans**: `datetime.utcnow`, `datetime.now()` without `tz=`, `pd.to_datetime(...)` without `utc=True`, `DateTime.Parse(...)` in any ingestion path, `new Date(stringThatIsntAFullISOWithTZ)`. Enforce by grep. Full reasoning: `.claude/rules/numerical-rigor.md` → "Timestamp rigor → Ban list".
7. **Fail-fast ingestion**: duplicate timestamps and non-monotonic sequences reject with a descriptive error. No silent `drop_duplicates`, no forward-fill.

### Python (PythonDataService/)

8. **Type hints on every signature.** `from __future__ import annotations` at the top of every module.
9. **Pydantic v2 only**: `model_validator`, `field_validator`. No `@validator`, no inner `Config` class.
10. **Async endpoints + `httpx.AsyncClient` + `ASGITransport(app=app)` for FastAPI tests.** Not `TestClient` for async.
11. **`from decimal import Decimal`** when the reference does, or when accumulation precision matters (see `app/engine/indicators/*.py`). Don't mix `Decimal` and `float` silently.

### .NET (Backend/)

12. **Always `[GraphQLName("fieldName")]`** on resolvers. HC v15 strips `Get`; don't rely on inference.
13. **`JsonNamingPolicy.SnakeCaseLower`** when deserializing Python responses.
14. **No math in resolvers or services.** Services are transport (like `TechnicalAnalysisService.cs`). If you're writing arithmetic in a `Backend/Services/`, justify it in the PR or move it to Python.
15. **`CancellationToken` threaded through every async call chain.** Never create and ignore one.

### Angular (Frontend/)

16. **Signals for state, zoneless is the default, `ChangeDetectionStrategy.OnPush` on every component.** Never `mutate()`; use `set()` / `update()`.
17. **Modern control flow only**: `@if`, `@for` (with `track`), `@switch`. Never `*ngIf`, `*ngFor`, `ngClass`, `ngStyle`.
18. **`input()` / `output()` functions, `inject()` for DI.** Never the legacy decorators.
19. **No heavy math in the view layer.** Downsampling and formatting are fine; strategy signals, P&L, and statistics are not.
20. **No `new Date(string)` parsing** for a field that came over the wire as `number` (ms). The type is `number`, not `string`.

## Workflow — when you're asked to add or touch math

1. **Identify the concept** (EMA, implied volatility, max drawdown, portfolio valuation, ...).
2. **Look it up in `docs/math-sources-of-truth.md`.** Three outcomes:
   - Listed and canonical file matches the layer you're in → add the provenance block and proceed.
   - Listed and canonical file is elsewhere → stop. Either call the canonical service (preferred) or, if you must duplicate for a justified reason, write a parity test and name the canonical file in `Validated against`.
   - Not listed → add a new row to the registry, same PR.
3. **Check for a reference**: `references/` (vendored), cited paper, or authoritative URL. If none exists, say so explicitly — "external: Polygon, not independently validated" is an acceptable provenance *once*.
4. **Write the test before or with the function**, not after. Fixture in `PythonDataService/tests/fixtures/golden/<name>/` for ported math; `Backend.Tests/` or `*.spec.ts` for cross-layer parity.
5. **Link test to contract**: the test file name goes in the `Validated against` field.

## Escape hatches (where the contract does NOT apply)

- Display-only code: `DatePipe`, `toFixed`, color scales, `@let` chart formatters, axis labels.
- Pass-through DTOs and mappers with no arithmetic.
- Logging and telemetry.
- Container orchestration, migrations, and schema definitions.
- Type-narrowing getters and `computed()` signals that reshape but don't compute.

## Pointers

- **Full rule files** (Claude Code auto-loads; Claude Desktop follow on demand):
  - `.claude/rules/numerical-rigor.md` — tolerances, fixtures, timestamps, reconciliation
  - `.claude/rules/python.md`, `.claude/rules/dotnet.md`, `.claude/rules/angular.md` — per-stack conventions
  - `.claude/rules/testing.md` — per-stack testing
- **Registry**: `docs/math-sources-of-truth.md` — one row per math concept, names the canonical file
- **Per-port notes**: `docs/references/<name>.md` — one file per reconciled port (currently empty; first entries populate as legacy math is touched)
- **Playbook skills** that do the heavy lifting, invoke by name:
  - `port-indicator` — porting math from a reference into `PythonDataService/`
  - `reconcile-backtest` — diffing two backtest runs, divergence taxonomy
  - `extract-math-from-paper` — turning a PDF paper into testable Python
  - `trading-domain` — auto-loads; defines bar/timestamp/signal vocabulary

## Anti-patterns to reject on sight

- Math function with no provenance block after you've touched it.
- `Canonical implementation: this file` in a `.NET` or `Frontend` math function without a parity test naming the Python canonical.
- `np.allclose(a, b)` or `isclose()` with defaults.
- Regenerating a golden fixture as the "fix" for a failing test.
- New arithmetic in `Backend/Services/` that could live in Python.
- A duplicate implementation of a math concept introduced without updating `docs/math-sources-of-truth.md` to explain why.
- `Validated against: manually checked` or `Validated against: looks right`. Name a test or leave the field honest: `Validated against: NONE — pending fixture`.
