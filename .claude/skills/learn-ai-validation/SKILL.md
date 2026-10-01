---
name: learn-ai-validation
description: Enforce the Math Provenance Contract across learn-ai's three layers. Use when adding or touching any mathematical function (indicator, Greek, pricing, backtest statistic, valuation arithmetic), when writing or reviewing a parity test, when adding a new canonical implementation, or when a reviewer asks "how do I know this number is right?" Auto-trigger on: `np.allclose`, `decimal.Decimal` in a computation, new `*.py` in `PythonDataService/app/engine/`, `bs_`/`black_scholes`/`greek`/`iv_` identifiers, new backtest or strategy file, new resolver or service computing a scalar. Escape: do not auto-trigger on UI formatting, display rounding, `toFixed`, or `DatePipe`.
---

# learn-ai validation skill

## Purpose (verbatim, do not drift)

Prevent drift between mathematical claims, their implementations, their tests, and their external references across the three-layer learn-ai stack. Produce a codebase that a quant reviewer or scientific reader can audit without talking to the author.

## The bedrock rule — Math Provenance Contract

**Every mathematical function in this repo must carry a provenance block with four fields.** No block = not merged. The fields are grep-able; CI can enforce them later. The block is the only record of where a concept's canonical implementation lives — there is no separate registry.

```
Formula              — the math it computes (one line, symbolic or named)
Reference            — the primary source: paper section, textbook, vendored file, or authoritative URL (no "various sources")
Canonical implementation — this file, OR a pointer to the canonical file elsewhere
Validated against    — the test file that proves equivalence to the Reference
```

"Mathematical function" means: anything that returns a number a user will compare against another number. Display formatting, `toFixed`, `DatePipe`, axis labels, and pass-through DTOs are **not** math functions and do not need a block.

`Reference:` names the primary source. It names a `docs/references/` note only when that note alone holds the fact (an accepted divergence, a fixture-less tolerance).

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
 * Foo metric (UI-side copy for a live chart).
 *
 * Formula: foo(x) = <the math, one line>
 * Reference: <paper section, textbook, or authoritative URL>
 * Canonical implementation: PythonDataService/app/<path>/foo_math.py
 *   (this copy is a named exception in ADR 0068: latency)
 * Validated against: Frontend/src/app/<path>/foo-math.parity.spec.ts —
 *   parity fixture generated from the Python canonical
 */
```

**C#** (XML doc on the method, not the class, unless the class has one arithmetic method):

```csharp
/// <summary>
/// Formula: MaxDrawdown = max_t(running_peak_t − equity_t) / running_peak_t
/// Reference: Bacon, Practical Portfolio Performance Measurement (2e), §8.2
/// Canonical implementation: PythonDataService/app/engine/results/statistics.py
///   (a .NET copy needs an entry in ADR 0068 naming its reason)
/// Validated against: Backend.Tests/Unit/Services/<Name>ParityTests.cs — parity
///   fixture generated from the Python canonical.
/// </summary>
```

### Scope of enforcement

- **New math**: block required on first commit. Missing block = block merge.
- **Touched math**: if you edit a math function and the block is missing or stale, add/update it. Touching = changing the numerical behavior OR the signature.
- **Untouched legacy**: do NOT backfill every existing math file today; burn it down as you touch it. Until then an untested block says `Validated against: NONE — pending fixture`.

### Single source of truth

Python owns the math (`AGENTS.md` principle 5): every math concept has **one** canonical implementation, in `PythonDataService/`.

- If you're computing a number that already exists, your `Canonical implementation` field points at the existing file and your code calls it.
- A .NET or Angular copy is allowed only as a named exception: a stated reason (latency, layer-locality) and a parity test naming the Python file in `Validated against`. Every exception is listed in [ADR 0068](../../../docs/architecture/adrs/0068-python-owns-the-canonical-math.md); a copy that can't justify itself is not one.

## Layer detection

When work touches a file under:

- `PythonDataService/` → Python layer. The home of every canonical math implementation, exposed via FastAPI.
- `Backend/` → .NET layer. GraphQL, auth, persistence; it passes Python's numbers through and computes none of its own unless ADR 0068 names the exception.
- `Frontend/` → Angular layer. Visualization; it formats and downsamples. Interactive math is allowed only as an ADR 0068 exception with a parity test.

## Workflow — when you're asked to add or touch math

1. **Identify the concept** (EMA, implied volatility, max drawdown, portfolio valuation, ...).
2. **Search for an existing canonical** (`grep -rn "Canonical implementation"`). If the concept lives elsewhere, call it. A copy outside Python needs an exception in the canonical-math ADR (ADR 0068) and a parity test.
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

- **Full rule files**:
  - `.claude/rules/numerical-rigor.md` — tolerances, fixtures, reconciliation taxonomy
  - `.claude/rules/temporal-rigor.md` — timestamps, the calendar, display
  - `.claude/rules/python.md`, `.claude/rules/dotnet.md`, `.claude/rules/angular.md` — per-stack conventions
  - `.claude/rules/testing.md` — per-stack testing
- **Playbook skills** that do the heavy lifting, invoke by name:
  - `port-indicator` — porting math from a reference into `PythonDataService/`
  - `reconcile-backtest` — diffing two backtest runs, divergence taxonomy
  - `extract-math-from-paper` — turning a PDF paper into testable Python

## Anti-patterns to reject on sight

- Math function with no provenance block after you've touched it.
- `Canonical implementation: this file` in a `.NET` or `Frontend` math function without a parity test naming the Python canonical.
- `np.allclose(a, b)` or `isclose()` with defaults.
- Regenerating a golden fixture as the "fix" for a failing test.
- New arithmetic in `Backend/Services/` that could live in Python.
- A copy outside Python with no entry in the canonical-math ADR (ADR 0068).
- `Validated against: manually checked` or `Validated against: looks right`. Name a test or leave the field honest: `Validated against: NONE — pending fixture`.
