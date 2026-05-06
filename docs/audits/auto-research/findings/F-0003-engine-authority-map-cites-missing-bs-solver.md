---
id: F-0003
severity: P1
status: open
area: inventory
canonical_file: PythonDataService/app/research/options/bs_solver.py
reference: docs/architecture/engine-authority-map.md (line 27)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`docs/architecture/engine-authority-map.md` line 27 names `app/research/options/bs_solver.py` as a canonical Python options authority for **"BS / Greeks / IV math"**, alongside `app/services/quantlib_pricer.py`, `app/services/bs_greeks.py`, `app/volatility/solver.py`, `app/volatility/fitting.py`. **The file `app/research/options/bs_solver.py` does not exist.** The directory `app/research/options/` exists and contains `contract_finder.py`, `diagnostics.py`, `iv_builder.py`, `__init__.py` — no `bs_solver.py`.

## Where

- Cited at: `docs/architecture/engine-authority-map.md:27`
- Cited at: `docs/architecture/numerical-authority-migration-plan.md:51` (in the "options-math-authorities" reference list)
- Actual contents of the directory:
  ```
  PythonDataService/app/research/options/contract_finder.py
  PythonDataService/app/research/options/diagnostics.py
  PythonDataService/app/research/options/iv_builder.py
  PythonDataService/app/research/options/__init__.py
  ```

## Why this severity

P1 — Authority map cites a canonical math file that doesn't exist. Either the file was renamed/moved without updating the map, or the map was wrong on creation. A new contributor reading the authority map to find IV-solver math would land on a 404; this is exactly the drift the Phase-0 governance work was meant to eliminate.

Note: the registry (`docs/math-sources-of-truth.md`) does *not* cite this file. Its IV row points only at `app/volatility/solver.py` (Newton + Brent fallback) and `app/services/quantlib_pricer.py::implied_volatility` (QuantLib bisection companion). So the registry is consistent with reality; the **authority map is the drift source**.

## Reproduction

```
test -f PythonDataService/app/research/options/bs_solver.py  # exit 1
grep -n "bs_solver" docs/architecture/engine-authority-map.md   # line 27
grep -n "bs_solver" docs/math-sources-of-truth.md               # no match
```

## Suggested resolution (NOT auto-applied)

In `docs/architecture/engine-authority-map.md` line 27, replace `app/research/options/bs_solver.py (IV solver)` with the actual canonical IV-solver path. Per the registry, that is `app/volatility/solver.py::implied_volatility` — which is **already** named on line 27 immediately before the missing reference. Likely the line had a duplicate / leftover reference to a renamed file. Trim it.

Also check whether the file ever existed (`git log --all --diff-filter=D -- "*bs_solver.py"`) and if so, capture the rename in the cleanup commit.

## Provenance of the finding itself

Phase 1 / cursor: cross-check of authority-map cited paths against actual filesystem. Verified by `Glob("PythonDataService/app/research/options/bs_solver.py")` returning no results.
