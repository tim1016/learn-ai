---
id: F-0014
severity: P2
status: open
area: inventory
canonical_file: PythonDataService/app/services/{data_quality_service,validation_service}.py
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

Two Python services compute math but have no registry rows:

- `data_quality_service.py` — 7-step quality pipeline, computes per-day/per-bar quality metrics (zero-volume rate, flat-bar count, OHLC violations, fractional-volume rate, VWAP-out-of-range count). These are defined quantities used in QC reports.
- `validation_service.py` — generates a markdown report comparing pandas-ta output against TradingView CSV exports. Has classification thresholds `_EXACT = 0.001`, `_CLOSE = 0.01`, `_OK = 0.1` (numerical tolerances baked into the file).

## Where

- `PythonDataService/app/services/data_quality_service.py` — full file
- `PythonDataService/app/services/validation_service.py` — `_EXACT`, `_CLOSE`, `_OK` thresholds at lines 14–17

## Why this severity

P2 — The math is real but not user-facing as authoritative numbers. `validation_service.py` is producing internal reports; `data_quality_service.py` is producing QC summaries. Still, the tolerance thresholds in `validation_service.py` are exactly the kind of "loosened tolerance without justification" that Phase 6 would flag — they should be cited (which prior audit told us this should match? what's the unit?).

## Reproduction

```
grep -nE '_EXACT|_CLOSE|_OK' PythonDataService/app/services/validation_service.py
grep -c 'data_quality_service' docs/math-sources-of-truth.md       # 0
grep -c 'validation_service' docs/math-sources-of-truth.md         # 0
```

## Suggested resolution (NOT auto-applied)

Add rows to `math-sources-of-truth.md`:

- **Data quality metrics** — canonical: `app/services/data_quality_service.py`. Reference: internal (cite the QC playbook if one exists). Status: canonical.
- **Pandas-ta vs TradingView validation report** — canonical: `app/services/validation_service.py`. Reference: internal. Note the `_EXACT/_CLOSE/_OK` tolerances and where they came from. Status: validation-only (it's a comparator, not a producer of numbers users compare).

## Provenance of the finding itself

Phase 1 / cursor: `PythonDataService/app/services/{data_quality_service,validation_service}.py` head reads.
