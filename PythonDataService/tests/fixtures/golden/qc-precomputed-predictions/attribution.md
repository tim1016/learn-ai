# QC precomputed-predictions fixture — attribution

- **Tutorial URL**: https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
- **Captured by**: Tim (QC Cloud account: `inkant awasthi`)
- **Captured at (UTC ms)**: `1778443824165` (≈ 2026-05-10 17:30 UTC)
- **Calendar window** (the `START` / `END` passed to `qb.history(...)` in the notebook):
  - Start: 2025-01-02 — 16:00 America/New_York → `1735851600000` ms UTC
  - End:   2025-12-31 — 16:00 America/New_York → `1767214800000` ms UTC
- **First emitted prediction**: 2025-01-13 (after 5-day lag warmup + 1 `pct_change` = 6 trading days from 2025-01-02)
- **Last emitted prediction**: 2025-12-30
- **Daily anchor**: `(America/New_York, 16:00)` — NYSE close, used by the importer to convert each `"YYYY-MM-DD"` date string to `int64 ms UTC`.
- **QC dataset id**: `QuantConnect/USEquity-Daily` (verbatim recorded — placeholder-style; QC Cloud doesn't expose a stable internal id at the notebook level)
- **Symbol**: SPY (single-symbol notebook, not the SP500 universe variant from QC's published tutorial code)
- **QC versions**:
  - sklearn: 1.6.1
  - numpy:   1.26.4
  - pandas:  2.3.3
  - lean:    (record from QC Cloud "About" footer next time)
- **Model**: `sklearn.linear_model.LinearRegression` on 5 lagged days of SPY's daily close `pct_change()` — deterministic (no `random_state` needed; LinearRegression is closed-form).
- **Notebook flavor**: simplified single-symbol version, **not** QC's published GradientBoostingRegressor + SP500-constituents-universe tutorial. The export shape matches QC's documented contract; the model and universe differ. Parity claim is therefore: "captured this export → importer reproduces this hash deterministically", not "our predictions equal QC's published values."

## Mismatch flagged at capture time

The version-printing cell in the original notebook used hardcoded `pd.Timestamp("2024-01-02 ...")` literals, so the printed `window_start_ms` / `window_end_ms` showed 2024 dates. The actual notebook used 2025 dates (per the `START`/`END` change in Cell 1). Trusted source of truth = the JSON's actual date range; provenance values above use the corrected 2025 window.

## Reproducibility

Re-importing this `qc_export.json` via `app/research/ml/generators/quantconnect_fixture.import_qc_fixture` with the provenance values above produces:

- `prediction_set_hash` = `5807a23fe16ce790d807df3697fa9c161c1887fdb603a7b1b89593cfc93f0188`
- 243 rows, first `timestamp_ms` = `1736802000000` (2025-01-13 16:00 ET), last = `1767128400000` (2025-12-30 16:00 ET).

Pinned in `tests/research/ml/fixtures/qc_known_hashes.json`.
