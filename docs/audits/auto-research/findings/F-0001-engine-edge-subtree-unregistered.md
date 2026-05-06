---
id: F-0001
severity: P1
status: open
area: inventory
canonical_file: PythonDataService/app/engine/edge/
reference: docs/architecture/engine-authority-map.md (row "Volatility / edge research")
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

The entire `PythonDataService/app/engine/edge/` subtree (≈25 Python files) is named in `docs/architecture/engine-authority-map.md` as the canonical engine for **"Volatility / edge research"** (line 21), but there is **not a single concept-level row** for any of its modules in `docs/math-sources-of-truth.md`. The map says it's canonical; the registry doesn't know it exists.

## Where

Files present in the subtree, grouped by likely math role:

- **Edge scoring / aggregation:** `edge_score.py`, `confidence.py`, `portfolio_aggregator.py`, `period_splitter.py`, `cross_asset_runner.py`, `threshold_events.py`
- **Volatility / VRP:** `vrp.py`, `spread_model.py`
- **Regime:** `regime_clustering.py`, `regime_drift.py`, `regime_strategy_eval.py`
- **Robustness:** `robustness_stats.py`
- **Real-time features:** `features_realtime/{delta_inversion,hf_realized_vol,iv30_constructor,realized_vol,regime_features}.py`
- **Forward-RV labels:** `labels_oracle/{forward_rv,hf_forward_rv}.py`
- **Trade simulation:** `trade_simulator.py`
- **Calibration:** `calibration/confidence.py`

Cross-references: `docs/architecture/engine-authority-map.md:21`; `docs/math-sources-of-truth.md` § Backtesting engine and statistics / § Research / divergence pipeline (no edge entries).

## Why this severity

P1 — Unregistered canonical math doing live computation per the Phase 1 severity heuristics in the skill. The engine-authority-map documents this as canonical; downstream consumers (research routers, frontend Edge surfaces) assume the math is provenance-tracked. Without registry rows, a future contributor cannot determine: which file owns realized vol, whether `hf_realized_vol.py` and `realized_vol.py` are duplicates, or whether VRP has a parity reference. Promotes to P0 if any of these outputs are rendered to a user as authoritative; defer that judgment to Phase 8 (wire fidelity).

## Reproduction

Static — no test run needed.

```
git ls-files PythonDataService/app/engine/edge/ | grep -v __init__ | grep -v test | wc -l   # ~25 files
grep -c "engine/edge" docs/math-sources-of-truth.md                                          # 0
grep -n "engine/edge" docs/architecture/engine-authority-map.md                              # 1 (line 21)
```

## Suggested resolution (NOT auto-applied)

Per registry's "Adding a new entry" rules: one concept row per math concept under a new section, e.g., `### Edge research / volatility surface`. Suggested rows:

- Realized volatility (and HF realized volatility) — canonical, reference, validating test
- Variance risk premium (VRP) — canonical: `vrp.py`
- IV30 construction — canonical: `features_realtime/iv30_constructor.py`
- Edge score — canonical: `edge_score.py`
- Regime clustering — canonical: `regime_clustering.py`
- Forward realized-vol labels — canonical: `labels_oracle/forward_rv.py` (and HF variant)
- Spread model — canonical: `spread_model.py`
- Trade simulator — canonical or external-unvalidated, depending on whether external reference exists

Each row needs a `Reference` (paper, vendored snippet, or "internal — no external reference"), a `Validated against` (existing tests under `app/engine/tests/` or `NONE — pending`), and a `Status` (`canonical`, `pending-fixture`, `external-unvalidated`).

## Provenance of the finding itself

Phase 1 / cursor: code-side scan of `PythonDataService/app/engine/**` cross-checked against `docs/math-sources-of-truth.md`. Reference consulted: `docs/architecture/engine-authority-map.md` (current HEAD on master, post-PR-#106 merge).
