---
id: F-0015
severity: P2
status: open
area: inventory
canonical_file: PythonDataService/app/research/features/{options_features,ta_features}.py
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/research/features/options_features.py` and `ta_features.py` define feature-engineering math used by the research signal pipeline (which itself is unregistered per F-0002).

`options_features.py` has e.g. `compute_iv_rank` (rolling-window IV rank with constants `MIN_VOLUME_SKEW = 50`, `MIN_OI_SKEW = 100`). `ta_features.py` has `compute_momentum_5m`, `compute_rsi_14`, `compute_realized_vol_30`, `compute_volume_zscore` etc.

Neither is registered.

## Where

- `PythonDataService/app/research/features/options_features.py`
- `PythonDataService/app/research/features/ta_features.py`
- `PythonDataService/app/research/features/registry.py` — feature-name registry; may document these internally but is not the registry of canonical math we audit.

## Why this severity

P2 — Feature engineering for research, not user-facing authoritative numbers. But the formulas matter (which RSI? Wilders or pandas-ta? what window? what handles ties in zscore?) and currently live without external citation.

`ta_features.py::compute_rsi_14` uses pandas-ta — interesting overlap with the registry's RSI row which says canonical is `app/engine/indicators/rsi.py` (Wilders) and notes that `ta_service.py` (pandas-ta) is a duplicate. **`ta_features.py` is a third RSI consumer using pandas-ta.** Worth flagging in the RSI row's duplicates list.

## Reproduction

```
grep -nE 'compute_rsi|compute_iv_rank|compute_realized_vol' PythonDataService/app/research/features/ta_features.py PythonDataService/app/research/features/options_features.py
grep -c 'research/features' docs/math-sources-of-truth.md       # 0
```

## Suggested resolution (NOT auto-applied)

Add a section to `math-sources-of-truth.md`, e.g., `### Research feature engineering`, with rows for each feature. For overlapping concepts (e.g., RSI), add `app/research/features/ta_features.py::compute_rsi_14` as an additional duplicate in the RSI row, with parity-test status.

For IV rank, cite the standard definition (rolling min/max window) and the chosen window (`60` days per the file).

## Provenance of the finding itself

Phase 1 / cursor: `app/research/features/{options_features,ta_features}.py` head reads.
