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

## Amendment: readiness parity contract v2 → v3 (2026-08-09)

`build_run_verdict_parity_signature` (`app/services/run_verdict_service.py`)
gained two fields, `evidence_action` and `missing_required_evidence`, without
changing anything about the pinned run-95/96 trades, native statistics, or
readiness score/grade/signal. `READINESS_PARITY_CONTRACT_ID` was bumped to
`readiness-core-v3` so `ParityVerdictService.CompareReadiness` (Backend) does
not compare an old-shape and a new-shape signature as if they were a real
evidence divergence.

`expected/pair.json`'s two `readiness.parity_signature` blocks (`lean` and
`python`, which must stay identical per
`test_pinned_pair_has_identical_readiness_signature_and_agree_receipt`) were
updated in place to the new contract's output — same `composite`/`grade`/
`signal`/`required_inputs` as before, contract_id bumped, and the two new
fields added (`evidence_action: "Revise the hypothesis or validation
design"`, `missing_required_evidence: []`). This value is not hand-typed: it
is `build_run_verdict_parity_signature`'s live output for the same pinned
run-95/96 evidence already retained in this fixture, reproducible via:

```text
python -c "
from pathlib import Path
import json
from app.services.lean_sidecar_persistence import build_persist_payload

FIXTURE = Path('tests/fixtures/golden/engine-lab-compatibility-95-96-v1')
source_manifest = json.load(open(FIXTURE / 'source' / 'lean-run-manifest.json'))
payload = build_persist_payload(
    workspace_path=FIXTURE, run_id='companion-pg-d2236694f523410fbb91',
    starting_cash=100_000.0, symbol='SPY', algorithm_name='ema_crossover_signal',
    start_date_ms=1_783_468_800_000, end_date_ms=1_786_060_800_000,
    manifest=source_manifest,
    cleanliness={'is_clean': True, 'is_reconciliation_grade': True, 'error_counts': {}},
    parity_group_id='pg-d2236694f523410fbb91',
)
print(json.loads(payload['run_verdict_json'])['parity_signature'])
"
```

Nothing else in this fixture changed. `manifest.json`'s receipt for
`expected/pair.json` was regenerated to match the new file bytes; every
other artifact's sha256/size is untouched. This does not require a new
versioned fixture directory because the underlying Independent Oracle
evidence (trades, native statistics, dimensions/sub-scores) is unchanged —
only our own downstream signature-serialization code changed shape.
