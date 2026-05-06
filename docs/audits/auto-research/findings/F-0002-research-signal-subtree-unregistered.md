---
id: F-0002
severity: P1
status: open
area: inventory
canonical_file: PythonDataService/app/research/signal/
reference: docs/architecture/engine-authority-map.md (row "Research signal scoring")
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

The `PythonDataService/app/research/signal/` subtree is named in `docs/architecture/engine-authority-map.md` as the canonical engine for **"Research signal scoring (IC, walk-forward, diagnostics)"** (line 20), citing `app/research/signal/backtest.py` (+ `engine.py`, `walk_forward.py`, `diagnostics.py`). None of the modules in this subtree have concept-level rows in `docs/math-sources-of-truth.md`.

## Where

Files present:

- `backtest.py`, `engine.py` — research backtest orchestration / scoring
- `walk_forward.py` — walk-forward analysis (cross-validation over time)
- `diagnostics.py` — diagnostic statistics
- `standardize.py` — feature standardization / scoring
- `regime.py` — regime-conditioned scoring
- `graduation.py` — signal graduation criteria
- `config.py` — config (likely not math)

Cross-references: `docs/architecture/engine-authority-map.md:20`; `docs/math-sources-of-truth.md` (no `research/signal/` entries).

Adjacent: `app/research/validation/{ic.py, quantile.py, robustness.py}` — see F-0008.

## Why this severity

P1 — Unregistered canonical math for a research engine the authority map declares canonical. Consumers presumably include any downstream feature/strategy graduation pipeline. Information coefficient (IC), walk-forward statistics, and regime conditioning are quantitative claims that need provenance. Without registry rows: no single source of truth for "what IC formula are we using", no parity reference, no test pin.

## Reproduction

```
git ls-files PythonDataService/app/research/signal/ | grep -v __init__ | grep -v test
grep -c "research/signal" docs/math-sources-of-truth.md   # 0
grep -n "research/signal" docs/architecture/engine-authority-map.md   # 1 (line 20)
```

## Suggested resolution (NOT auto-applied)

Add a new section in `math-sources-of-truth.md`, e.g., `### Research signal scoring`, with rows for: signal IC computation, walk-forward statistics, regime-conditioned scoring, graduation criteria. Each row should cite an external reference if one exists (Lopez de Prado for fractional differentiation / IC interpretation, or "internal" if not), and pin to a fixture or `NONE — pending`.

## Provenance of the finding itself

Phase 1 / cursor: code-side scan of `PythonDataService/app/research/**` cross-checked against `docs/math-sources-of-truth.md`. Reference consulted: `docs/architecture/engine-authority-map.md`.
