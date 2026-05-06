---
id: F-0025
severity: P2
status: open
area: tolerance
canonical_file: cross-cutting (PythonDataService + Backend.Tests)
reference: .claude/rules/numerical-rigor.md (Tolerance rules)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 6
---

## What

Phase 6 sweep across `PythonDataService/` and `Backend.Tests/` for `np.allclose(`, `np.isclose(`, `assertAlmostEqual`, and `Assert.Equal(.., .., delta:)` returned **surprisingly few hits** — the codebase is mostly clean of bare-default float comparisons. Two real items found:

### `app/engine/edge/edge_score.py:82` — bare `np.isclose`

```python
if not np.isclose(sum(w.values()), 1.0):
```

No explicit `atol`/`rtol`. NumPy default (`atol=1e-8, rtol=1e-5`) is being used silently. Per `.claude/rules/numerical-rigor.md`: "No `np.allclose(a, b)` without explicit `atol` and `rtol`. Defaults are a bug." Since this is a weight-sum check (probabilities should sum to 1.0), the rules' "Probabilities" tolerance of `atol=1e-10, rtol=0` would be the fitting choice.

### `Backend.Tests/Unit/Services/PositionEngineTests.cs:332`

```csharp
Assert.Equal(incrementalCost, rebuiltPositions[0].AvgCostBasis, 4);
```

The `4` is a precision argument (4 decimal places). No comment justifies why 4 instead of 6 or higher. For accumulated `decimal`-typed cost-basis arithmetic, 4 dp is a loose tolerance.

### Defensible: `tests/edge/test_regime_clustering.py:41`

```python
assert np.allclose(res.posterior.sum(axis=1), 1.0, atol=1e-6)
```

Has explicit `atol`. Missing `rtol` (default `rtol=1e-5` applies). Per rules, probabilities should be `atol=1e-10, rtol=0` — 1e-6 is loose for a probability sum. Defensible if the posterior comes from a numerically delicate EM iteration; should be commented.

## Why this severity

P2 — Two items, both in low-traffic test/edge code. The repo is clearly aware of the tolerance-hygiene rule and mostly applies it. The two cases are minor enough that fixing them is mechanical.

## Reproduction

```
grep -nE 'np\.allclose\(|np\.isclose\(|assertAlmostEqual' PythonDataService/
grep -nE 'Assert\.Equal\([^,]+,[^,]+,\s*\d' Backend.Tests/
```

## Suggested resolution (NOT auto-applied)

- `edge_score.py:82` — make explicit: `np.isclose(sum(w.values()), 1.0, atol=1e-10, rtol=0)`. Add a one-line comment if 1e-10 is too tight for the use case (e.g., if weights are user-supplied and may have legitimate float noise).
- `PositionEngineTests.cs:332` — either tighten to 6+ dp or add a comment explaining why 4 dp is the right tolerance for cost-basis accumulation.
- `test_regime_clustering.py:41` — add `rtol=0` and a one-line comment about the EM-step error budget.

## Provenance of the finding itself

Phase 6 / cursor: cross-stack grep over `PythonDataService/`, `Backend.Tests/`. Output truncated at 100 lines (Python) / 40 lines (.NET) — the limit was not hit, indicating the actual count is very small.
