---
id: F-0007
severity: P1
status: fixed-verified
area: inventory
canonical_file: PythonDataService/app/volatility/
reference: docs/architecture/engine-authority-map.md (line 27, partial)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/volatility/` contains 14 modules of options-volatility math. The registry (`docs/math-sources-of-truth.md`) has only one row pointing into this subtree (`solver.py::implied_volatility`). The authority map adds `fitting.py` (line 27). **Twelve of the fourteen files have no concept-level provenance.**

## Where

Files in the subtree (excluding `__init__.py`, `example.py`):

- `solver.py` — IV root-finding ✓ registered
- `fitting.py` — surface fits ✓ map-cited (registry doesn't have a row)
- `surface.py` — surface representation/operations
- `analytics.py` — IV/realized analytics
- `basis.py` — IV-RV basis (cross-references `docs/references/iv-rv-basis-alignment.md`?)
- `cache.py` — caching layer (probably not math)
- `conventions.py` — day-count / annualization conventions (math semantics, even if simple)
- `data_loader.py` — likely transport
- `iv30_health.py` — IV30 health checks
- `iv_provenance.py` — IV provenance metadata (interesting — overlap with this audit)
- `models.py` — IV models (parametric? no-arb constraints?)
- `price_normalization.py` — price normalization (anything dividing by spot or strike is math)
- `vix_replication.py` — VIX replication formula (Demeterfi 1999 or similar — definitely a math reference required)

## Why this severity

P1 — Volatility subtree is the most provenance-sensitive area in the repo (ratios, log-moneyness, T-conventions, no-arb constraints all combine in subtle ways). Multiple files have explicit math semantics in their names (`vix_replication`, `basis`, `fitting`, `surface`, `iv30_health`) and zero registry provenance. A `vix_replication` implementation without a paper citation in the registry is an audit failure waiting to happen.

`fitting.py` is doubly notable: the authority map cites it as canonical, but the registry has no row. Map ↔ registry drift on an actively-used module.

## Reproduction

```
git ls-files PythonDataService/app/volatility/ | grep -v __init__ | grep -v example
grep -n "app/volatility/" docs/math-sources-of-truth.md      # only solver.py
grep -n "app/volatility/" docs/architecture/engine-authority-map.md   # solver.py + fitting.py
```

## Suggested resolution (NOT auto-applied)

Add a new section to `math-sources-of-truth.md`, e.g., `### Volatility surface and analytics`, with rows for:

- IV surface fitting — canonical: `volatility/fitting.py`. Reference: SVI / SABR / whichever parameterization is in use (read the file to confirm). **Critical to cite externally.**
- IV30 construction & health — canonical: `volatility/iv30_health.py`. Reference: matches `app/research/options/iv_builder.py` row (variance-time interpolation per `docs/math-rigor.md` Upgrade 1).
- VIX replication — canonical: `volatility/vix_replication.py`. Reference: Demeterfi-Derman-Kamal-Zou (1999) "More Than You Ever Wanted to Know About Volatility Swaps" (or whatever is actually in the file).
- IV-RV basis — canonical: `volatility/basis.py`. Reference: cross-link to `docs/references/iv-rv-basis-alignment.md`.
- Price normalization — canonical: `volatility/price_normalization.py`.
- Day-count / annualization conventions — canonical: `volatility/conventions.py`.
- Surface model representation — canonical: `volatility/surface.py` and `volatility/models.py` (likely paired).
- IV provenance (metadata) — canonical: `volatility/iv_provenance.py` — note: this may itself be infrastructure for the math-rigor effort and worth understanding before this audit recommends anything.

Each row needs a `Reference`, `Validated against`, and `Status`. Where the reference is a paper, it should be added to `docs/references/papers/` or cited in `docs/references/<name>.md`.

## Provenance of the finding itself

Phase 1 / cursor: code-side scan of `PythonDataService/app/volatility/**`. Cross-checked against registry.
