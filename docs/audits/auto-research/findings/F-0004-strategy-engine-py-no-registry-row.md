---
id: F-0004
severity: P1
status: fixed-verified
area: inventory
canonical_file: PythonDataService/app/services/strategy_engine.py
reference: docs/architecture/engine-authority-map.md (line 22)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`docs/architecture/engine-authority-map.md` line 22 names `app/services/strategy_engine.py::AnalyzeOptionsStrategy` as the canonical engine for **"Options strategy analysis (payoff, POP, Greeks for a hypothetical strategy)"**, with status `canonical — scheduled for payload extension in Phase 1.1` (which `numerical-authority-migration-plan.md` confirms shipped as commit `451394d`). **There is no concept-level row for this in `docs/math-sources-of-truth.md`.**

The migration plan describes its outputs in detail (current-time PnL curve, what-if curves, Greek curves per spot, leg diagnostics) — these are *the* values the Frontend's Options Strategy Lab consumes. The only nearby registry rows are for Black-Scholes price, Greeks, and the portfolio scenario / live-Greeks endpoints — none cover "options strategy analysis" as a concept.

## Where

- Authority-map cite: `docs/architecture/engine-authority-map.md:22`
- Migration plan cite: `docs/architecture/numerical-authority-migration-plan.md:111-128` (Phase 1.1/1.2)
- Actual file: `PythonDataService/app/services/strategy_engine.py` exists
- Frontend consumer (per migration plan): `Frontend/src/app/components/.../options-strategy-lab/` consumes `currentCurve` / `greekCurves` / `legDiagnostics` from this file's response
- Registry: no row mentioning `strategy_engine.py` or "options strategy analysis"

## Why this severity

P1 — Math-authority documented in the architecture map but missing from the concept registry. Outputs are **rendered to the user as authoritative** (PnL curve, Greek curves are the core of the Options Strategy Lab UI), so a Phase-8 wire-fidelity audit will trip on the absence of a concept row. The numbers themselves likely flow from `bs_greeks.py`/`quantlib_pricer.py` (which are registry-tracked), but the *aggregation, scenario shaping, and per-leg diagnostic computation* in this file is its own math layer that needs provenance.

## Reproduction

```
grep -c "strategy_engine.py" docs/math-sources-of-truth.md         # 0
grep -c "strategy_engine.py" docs/architecture/engine-authority-map.md   # 1+
grep -c "AnalyzeOptionsStrategy" docs/math-sources-of-truth.md     # 0
```

## Suggested resolution (NOT auto-applied)

Add a row to `docs/math-sources-of-truth.md` under a new or existing options section:

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Options strategy analysis (payoff, POP, current-time PnL curve, Greek curves, per-leg diagnostics) | `PythonDataService/app/services/strategy_engine.py::AnalyzeOptionsStrategy` (+ `app/routers/strategy.py`) | — (Frontend `OptionsStrategyLabComponent` is now a passthrough since `451394d`) | Hull §11–12 (payoff diagrams), §19 (Greeks); composes `bs_greeks.py` + `quantlib_pricer.py` + `volatility/solver.py` | (existing tests, or `NONE — pending`) | canonical |

Once the row exists, Phase 4 (provenance) will check whether `strategy_engine.py` itself carries the 4-field block.

## Provenance of the finding itself

Phase 1 / cursor: cross-check of authority-map canonical engines against the registry.

## Closure (2026-05-06)

Row added to `docs/math-sources-of-truth.md` § Options pricing and Greeks: "Options strategy analysis (payoff, POP, current-time PnL curve, Greek curves, per-leg diagnostics)" canonical = `app/services/strategy_engine.py::AnalyzeOptionsStrategy`. Phase 4 follow-up (4-field provenance block on the file itself) is tracked in F-0027.

