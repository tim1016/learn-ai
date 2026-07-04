# LEAN vs Python SPY EMA 6-day cross-engine reconciliation

**Date:** 2026-06-10

**Purpose:** Prove the Phase 5g.3 cross-engine reconciliation endpoint is wired end to end on the known-clean AppleHV arm64 LEAN baseline, after the wider-window LEAN path remained blocked by the AppleHV/CoreCLR SIGILL.

## Inputs

- LEAN sidecar run: `arm64_postpatch_6day_v2`
- LEAN image: `localhost/learn-ai/lean-sandbox@sha256:e2186f2e3e3e2c1ffb579c8cdbd4f74211a9c453893cb8273685555031b8187e`
- LEAN template: `ema_crossover`
- Python strategy: `SpyEmaCrossoverAlgorithm`
- Symbol: `SPY`
- Window: 2026-06-02 through 2026-06-08, regular session
- Starting cash: `100000.0`
- Data source: staged LEAN workspace data from `PythonDataService/artifacts/lean-sidecar/arm64_postpatch_6day_v2/workspace/data`
- Fee assertion: `assert_fees=false`

The LEAN run was clean before reconciliation:

- `exit_code=0`
- `is_clean=True`
- `bars_consumed_by_symbol.SPY=1950`
- normalized parser: `phase-3a-r1`

## Command

Executed through the FastAPI route in-process:

```bash
cd PythonDataService
PYTHONPATH=. ./.venv/bin/python - <<'PY'
import asyncio, json
from httpx import ASGITransport, AsyncClient
from app.main import app

async def main():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        r = await client.post('/api/lean-sidecar/runs/arm64_postpatch_6day_v2/cross-reconcile', json={
            'engine_lab_strategy_class': 'SpyEmaCrossoverAlgorithm',
            'assert_fees': False,
        })
        print('status', r.status_code)
        print(json.dumps(r.json(), indent=2))

asyncio.run(main())
PY
```

## Result

```json
{
  "schema_version": 1,
  "run_id": "arm64_postpatch_6day_v2",
  "engine_lab_strategy_class": "SpyEmaCrossoverAlgorithm",
  "assert_fees": false,
  "lean_total_fills": 0,
  "engine_total_fills": 0,
  "matched_count": 0,
  "divergent_count": 0,
  "gating_divergent_count": 0,
  "passed": true,
  "counts_by_category": {},
  "divergences": []
}
```

## Interpretation

The endpoint is live and the shared-staged-data cross-run path succeeds:

- The router loaded `normalized/result.json`.
- The router extracted `symbol`, `start_date`, `end_date`, and `starting_cash` from `manifest.json`.
- `run_engine_lab_on_workspace` ran `SpyEmaCrossoverAlgorithm` against the same staged LEAN minute zips.
- `compare_cross_engine` returned a versioned report using the eight-category `DivergenceCategory` taxonomy.

The report passes because neither engine emitted fills in this 6-day EMA window. This proves the endpoint plumbing and no-trade decision parity for the known-clean AppleHV baseline, but it does not prove fill-level parity for a window with actual EMA trades. That remains blocked locally by the arm64 wide-window SIGILL and the amd64/Rosetta workload segfault documented in the 2026-06-09 AppleHV SIGILL investigation handoff (pruned 2026-07-04; git history).

## Tests

Focused cross-engine tests passed locally:

```bash
cd PythonDataService
./.venv/bin/python -m pytest \
  tests/lean_sidecar/test_cross_reconciler.py \
  tests/lean_sidecar/test_router_lean_sidecar.py \
  -q -k 'cross'
```

Result: `32 passed, 80 deselected`.
