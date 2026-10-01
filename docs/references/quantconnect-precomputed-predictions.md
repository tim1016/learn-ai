# QuantConnect precomputed-predictions parity (Phase 1)

**Reference source:** QuantConnect "Precomputed ML Predictions" tutorial — `https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions` (URL pinned in the captured fixture's `attribution.md`).

## Tolerances

| Comparison | Tolerance | Justification |
|---|---|---|
| QC published prediction value vs. importer output value | `atol=1e-9, rtol=0` | QC's export is deterministic; predictions are static numbers. Anything looser is a smell. |
| `prediction_set_hash` reproduction | bit-exact | The hash covers content, not bytes ([ADR 0072](../architecture/adrs/0072-research-run-identity-and-sealed-run-inputs.md) decision 6). |
| `RunLedger.prediction_set_hash`, `result_hash` | bit-exact | Same reasoning. |

## Pinned decisions

| # | Decision | Value | Notes |
|---|---|---|---|
| 1 | Symbol to anchor parity on | **AAPL** | Long-tenured S&P 500 constituent; present in `qb.universe.etf(spy)` continuously across any reasonable validation window. Changed from SPY because SPY itself is **not** a constituent of its own ETF universe — QC's published tutorial uses `qb.universe.etf(spy)` which returns SP500 stocks, so SPY is absent from the export. |
| 2 | Daily anchor `(tz, HH:MM)` for date-only → `int64 ms UTC` | `("America/New_York", "16:00")` (defaults; NYSE close) | |
| 3 | `qc_dataset_id` convention | Use QC's labeled string for the data source (e.g. `"QuantConnect/USEquity-Daily"`); record the verbatim label in `attribution.md`. | |
| 4 | QC `Symbol` key normalization | Strip the security-id suffix at fixture-capture time via `str(s).split(' ', 1)[0]` so saved JSON has bare ticker keys (e.g. `"AAPL"`, not `"AAPL R735QTJ8XC9X"`). | QC stringifies `Symbol` objects with security identifiers. The importer reads bare tickers; the normalization happens in the notebook before save. |

## Captured fixture provenance

(See `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/attribution.md` for the full record.)

- **QC tutorial URL**: <https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions>
- **QC dataset id**: `QuantConnect/USEquity-Daily` (verbatim placeholder; QC Cloud doesn't expose a stable internal id at the notebook level)
- **Validation window**: `2026-02-10` → `2026-03-12` at NYSE close (`1770757200000` → `1773345600000` ms UTC) — the `[validation_start, validation_end]` QC's tutorial predicts on
- **Train window**: 90 trading days preceding `validation_start` (QC tutorial default)
- **Symbol** (parity anchor): AAPL — extracted from a full `qb.universe.etf(spy)` SP500-constituents export (~500 symbols per record)
- **Model**: `sklearn.ensemble.GradientBoostingRegressor(n_estimators=100, learning_rate=0.1, max_depth=3, random_state=42)` — QC's published precomputed-ML-predictions tutorial code, verbatim. Features: 10-day momentum, 20-day daily-return volatility, relative volume. Label: open-to-open return from `T+1` to `T+2`.
- **Versions**: sklearn 1.6.1, numpy 1.26.4, pandas 2.3.3 (lean: record from QC Cloud "About" footer next time)
- **Exported at (UTC ms)**: `1778469503771` (≈ 2026-05-10)
- **Row count**: 22 daily AAPL predictions (one per trading day in the validation window)
- **Pinned `prediction_set_hash`**: `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` (in `tests/research/ml/fixtures/qc_known_hashes.json`)
- **Pinned `RunLedger.prediction_set_hash`**: `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` — equals the manifest hash because the runner threads it through unchanged
- **Pinned `result_hash`**: `b2e1ffc871d9f39a04b6975e08f926b1ed2a635a9066551644fa5f306d778713` — covers the (artifact, spec, synthetic AAPL daily bars, engine config) tuple end-to-end. Repinned on 2026-08-12 after the engine began materializing its deliberate terminal `EndOfAlgorithm` close; inputs are unchanged, but the deterministic result now includes that synthetic exit. The prior `f8074881…450180` pin reflected the same fixture before that behavior.
