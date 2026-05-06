---
id: F-0008
severity: P1
status: open
area: inventory
canonical_file: PythonDataService/app/research/validation/
reference: missing (no map or registry citation)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/research/validation/` contains math primitives — `ic.py` (information coefficient), `quantile.py` (quantile-based statistics), `robustness.py` (robustness statistics) — and is **neither named in the engine-authority-map nor in `math-sources-of-truth.md`**. Unlike F-0001 / F-0002 / F-0007, there isn't even an authority-map row to drift from; the subtree exists silently.

The companion subtree `app/research/signal/` (F-0002) is at least authority-map-cited. `app/research/validation/` is more orphaned.

## Where

- Files: `app/research/validation/ic.py`, `app/research/validation/quantile.py`, `app/research/validation/robustness.py`
- Authority map: not mentioned (lines 19–28 don't cite it)
- Registry: not mentioned
- Likely consumers: `app/research/signal/*.py` (information coefficient is core to signal scoring)

## Why this severity

P1 — Information coefficient is THE quantitative measure of signal predictivity in this repo's research pipeline. An IC implementation without a paper citation in the registry is an audit failure: which IC is it (Pearson, Spearman, rank, regression-residual)? What handles ties? What's the warmup behavior? What's the parity reference? All unknown without provenance.

Quantile-based statistics and robustness measures have similar concerns: trimmed-mean vs winsorized, bootstrap confidence intervals (sample size? reps?), block-bootstrap parameters (block length?). All numerical choices that need pinning.

## Reproduction

```
git ls-files PythonDataService/app/research/validation/
grep -c "research/validation" docs/math-sources-of-truth.md         # 0
grep -c "research/validation" docs/architecture/engine-authority-map.md   # 0
```

## Suggested resolution (NOT auto-applied)

Add either as a new section in `math-sources-of-truth.md` or under the new `### Research signal scoring` section recommended in F-0002:

- IC — canonical: `app/research/validation/ic.py`. Reference: Lopez de Prado, Advances in Financial Machine Learning, §8 (or whichever is actually used). Validated against: existing tests under `app/research/` or `NONE — pending`.
- Quantile statistics — canonical: `app/research/validation/quantile.py`. Reference: standard statistical reference (Conover, Wasserman, etc.).
- Robustness — canonical: `app/research/validation/robustness.py`. Reference: Politis-Romano (block bootstrap) or whichever method is implemented.

Also add the subtree to `engine-authority-map.md` as a row under research engines.

## Provenance of the finding itself

Phase 1 / cursor: code-side scan of `PythonDataService/app/research/**`. Subtree found in glob, absent from both governance docs.
