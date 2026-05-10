# QuantConnect precomputed-predictions parity (Phase 1)

**Reference source:** QuantConnect "Precomputed ML Predictions" tutorial — `https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/precomputed-ml-predictions` (URL pinned in the captured fixture's `attribution.md`).

**Spec:** `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`

**Plan:** `docs/superpowers/plans/2026-05-10-quantconnect-precomputed-predictions-parity.md`

## Status

- §A — schema extension + importer + synthetic-fixture tests: **landed** (this PR).
- §B — real QC fixture capture: **pending Tim's QC Cloud run**.
- §C — pinned hashes + parity tests: **gated on §B**.

## Tolerances

| Comparison | Tolerance | Justification |
|---|---|---|
| QC published prediction value vs. importer output value | `atol=1e-9, rtol=0` | QC's export is deterministic; predictions are static numbers. Anything looser is a smell — see spec D8. |
| `prediction_set_hash` reproduction | bit-exact | Hash is a function of canonical row JSON; pyarrow / pandas drift cannot affect it (v0.5 invariant). |
| `RunLedger.prediction_set_hash`, `result_hash` | bit-exact | Same reasoning. |

## What §A established

- `GeneratorMeta` is a discriminated union (`deterministic_rule | quantconnect_precomputed_fixture`); manifest schema stays at `1.0`.
- `app/research/ml/generators/quantconnect_fixture.py` reads a closed `qc_export.json`, filters to one symbol, converts tz-aware ISO 8601 dates to `int64 ms UTC` at the ingestion boundary, and emits a v0.5-compliant `manifest.json` plus a single chunk parquet.
- Determinism is enforced by a re-run test: same input must produce byte-identical manifest and identical `prediction_set_hash`.

The synthetic `qc_export.json` shape used by §A tests is a **strawman** for the real QC export schema. §B will either confirm the strawman or force adjustments to the closed Pydantic model in `quantconnect_fixture.py`.

## Captured fixture provenance (filled in at §B)

- QC tutorial commit / version: TBD at §B
- QC dataset id: TBD
- Calendar window: TBD (pinned start/end, no `datetime.now()`)
- Symbol(s) in export: TBD
- QC sklearn / LEAN / numpy versions: TBD
- Exported at (UTC): TBD
- Pinned `prediction_set_hash`: TBD at §C
- Pinned `RunLedger.prediction_set_hash`: TBD at §C
- Pinned `result_hash`: TBD at §C
