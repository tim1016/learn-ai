# Day 0 reconciliation — 2026-05-09

**Run:** `dry-run-2026-05-09`  
**Generated:** 2026-05-09T13:02:30.320025+00:00

**Halt triggered for next session:** fill-class breach count=1

## Artifact hashes (SHA-256)

```yaml
artifact_hashes:
  reconcile_json: 04e97a5d060886c850ffd758ffdba272b45aa66fa9bb8cd94f95700246d237a9
  reconcile_parquet: fdbfd28b2d903239f945b0baa90e1100847648745a2c379ea698922f49c16b55
  python_executions_parquet: 9fb4df39f7546e53d8b841b23fb4f977cf31e01753bb53330dbe36907181643d
  python_trades_parquet: ~
  qc_export_trades: ~
  qc_export_indicators: f3b9197c400014a7efd92ca71af9f00e3dca8a5d49e129fe6f7548ebdc0e3178
  run_ledger: b4b736adfc4f9b482aadcd231706cc1c8faa85cea10245b7d5c3b4dc68d3eea8
```

## Tolerances applied

| Dimension | Value |
|---|---|
| EMA atol | `0.1` |
| RSI atol | `2.0` |
| Fill price atol | `0.05` |
| Fill time atol (s) | `5` |
| Fill quantity atol | `0` |

## Counts

| Metric | Value |
|---|---:|
| Bars matched (Python ∩ QC) | 3 |
| Bars Python-only | 0 |
| Bars QC-only | 0 |
| Cross-engine `none` | 3 |
| Cross-engine `data` | 0 |
| Cross-engine `engine` | 0 |
| Fill `none` | 2 |
| Fill `within_tol` | 0 |
| Fill `breach` | 1 |

## Notable rows

(All non-`none` cross-engine rows, all fill breaches, all signal bars.)

| bar_close_ms | python_signal | python_ema5 | python_ema10 | python_rsi | qc_signal | qc_ema5 | qc_ema10 | qc_rsi | cross_engine_class | python_fill_price | python_intended_price | fill_class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1778338800000 | ENTER | 501.0000 | 500.0000 | 62.0000 | ENTER | 501.0400 | 500.0200 | 62.5000 | none |  | 501.0000 | breach |

