# QC precomputed-predictions fixture — attribution

- **Tutorial URL**: https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
- **Captured by**: Tim (QC Cloud account on file)
- **Captured at (UTC ms)**: `1778469503771`
- **Validation window** (the `[validation_start, validation_end]` the notebook predicted on):
  - Start: 2026-02-10 — 16:00 America/New_York → `1770757200000` ms UTC
  - End:   2026-03-12 — 16:00 America/New_York → `1773345600000` ms UTC
- **Train window**: 90 trading days preceding `validation_start` (per QC tutorial's `train_start = validation_start - timedelta(90)`)
- **Daily anchor**: `(America/New_York, 16:00)` — NYSE close, used by the importer to convert each `"YYYY-MM-DD"` date string to `int64 ms UTC`.
- **QC dataset id**: `QuantConnect/USEquity-Daily` (verbatim — QC Cloud doesn't expose a stable internal id at the notebook level)
- **Symbol**: AAPL (parity anchor — long-tenured S&P 500 constituent, present in `qb.universe.etf(spy)` across the full validation window)
- **Universe in the export**: S&P 500 constituents (`qb.universe.etf(spy)`), ~500 symbols per record; the importer filters to AAPL.
- **QC versions**:
  - sklearn: 1.6.1
  - numpy:   1.26.4
  - pandas:  2.3.3
  - lean:    (record from QC Cloud "About" footer next time)
- **Model**: `sklearn.ensemble.GradientBoostingRegressor(n_estimators=100, learning_rate=0.1, max_depth=3, random_state=42)` — QC's published precomputed-ML-predictions tutorial code, verbatim. Features: 10-day momentum, 20-day daily-return volatility, relative volume. Label: open-to-open return from `T+1` to `T+2`.
- **Symbol-key normalization**: QC stringifies `Symbol` objects with security-id suffix (e.g. `"AAPL R735QTJ8XC9X"`). A post-processing cell in the notebook stripped the suffix via `str(s).split(' ', 1)[0]` so the saved file has bare ticker keys. The importer reads bare tickers.

## Reproducibility

Re-importing this `qc_export.json` via `app/research/ml/generators/quantconnect_fixture.import_qc_fixture` with the provenance values above produces:

- `prediction_set_hash` = `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e`
- 22 rows, first `timestamp_ms` = `1770757200000` (2026-02-10 16:00 ET), last = `1773345600000` (2026-03-12 16:00 ET).

Pinned in `tests/research/ml/fixtures/qc_known_hashes.json`.

## Provenance gotchas (from the capture session)

- The provenance-printing cell originally had hardcoded `pd.Timestamp("2024-01-02 ...")` literals from an earlier LinearRegression fixture; the `window_start_ms` / `window_end_ms` values were corrected to match the GBM notebook's actual `validation_start` / `validation_end` (2026-02-10 / 2026-03-12) before pinning.
- Symbol-key normalization is required: without the post-processing strip, the importer's `symbol="AAPL"` lookup would fail because QC's keys carry the security-id suffix.
