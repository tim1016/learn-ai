# Engine Lab compatibility runs 95 / 96 golden fixture

## Independent Oracle

- Python Engine Lab source row: run 95.
- QuantConnect LEAN source row: run 96, retained artifact `companion-pg-d2236694f523410fbb91`.
- LEAN source commit: `261366a7e26ae942df858ab20df4fef8fa07de67`.
- LEAN image: `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`.
- Compatibility contract: `us-equity-raw-ibkr-v1`.

## Inputs

- SPY, raw one-minute trade bars, regular session.
- Requested run dates: 2026-07-08 through 2026-08-07.
- Strategy cadence: 15 minutes; starting cash: USD 100,000.
- Shared fixture: `bar-store-v1-6e96f0f2c5383e2d` / `6e96f0f2c5383e2d1ad40f7308433aacc862bbfd6a9828f0b263cade70f0460e`.
- Every staged ZIP and supporting LEAN data file is committed under `workspace/data/`
  and SHA-256 covered by `manifest.json`; tests never call Polygon.

## Pinned result

- Five closed trades with no timestamp, direction, quantity, fill-price, fee,
  P&L, or order-type divergence.
- Production Readiness v2: 17/17 inputs, C / 44 / Rework on both engines.
- LEAN native statistics: 66 numeric values and
  25 formatted dashboard values matched.
- All three LEAN analysis findings are retained in `expected/pair.json` and
  the raw LEAN result.
- Final paired verdict: `AGREE`.

## Regeneration

Generated once from the retained run-96 artifact and live run-95/96 database
rows with:

```text
python scripts/pin_engine_lab_compatibility_golden.py \
  /path/to/companion-pg-d2236694f523410fbb91 \
  --graphql-url http://localhost:5050/graphql
```

The exporter refuses any different artifact hash, run IDs, group ID, bar
fixture, LEAN source/image, incomplete readiness receipt, or non-AGREE verdict.
A future re-baseline must use a new versioned fixture directory.

## Validation

```text
python -m pytest tests/integration/reconciliation/test_engine_lab_compatibility_golden.py
```

Tolerance contracts are fixed at $0.01 for fill prices, `rtol=0`,
`atol=1e-6` for accumulated P&L, `atol=0` for quantities/timestamps, and
`atol=0.0000500001` for LEAN native displayed-precision statistics.
