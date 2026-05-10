# QuantConnect precomputed-predictions golden fixture

This directory holds the **immutable** ground truth captured from QuantConnect Cloud
for Phase 1 parity validation. Per `.claude/rules/numerical-rigor.md` § Golden fixtures,
contents are generated once and re-generated only with justification.

## Pending capture (§B)

When §B runs, this directory must contain:

- `qc_export.json` — raw output of the QC precomputed-ML-predictions tutorial,
  pinned to a deterministic calendar window (no `datetime.now()`).
- `qc_price_history.csv` — the OHLCV bars QC's tutorial saw, for any feature
  recomputation audit.
- `attribution.md` — pinned dates, sklearn / LEAN / numpy versions, dataset id,
  tutorial URL, screenshot or text log of the QC notebook output, and the command
  used to regenerate.

## What §A landed (current state)

- Importer: `PythonDataService/app/research/ml/generators/quantconnect_fixture.py`
- Importer tests: `PythonDataService/tests/research/ml/test_quantconnect_fixture_*.py`

The synthetic shape used by tests is a **strawman** for the QC export's actual shape.
Once `qc_export.json` is captured here, the closed Pydantic model in
`quantconnect_fixture.py` is verified or adjusted, and the parity tests in
`test_quantconnect_fixture_parity.py` / `_runtime.py` are unskipped.

## Reference doc

`docs/references/quantconnect-precomputed-predictions.md` carries the parity
claim, tolerances, and captured-fixture provenance fields (filled at §B/§C).
