# Indicator convergence measurements

Generated 2026-10-22 for issue #2857 and ADR 0083. This fixture measures two
copies of the existing canonical streaming indicators; it is empirical output
evidence for the new memory-decay policy, not an independent LEAN value oracle.
Existing indicator golden fixtures remain the value/readiness authority.
`test_convergence.py` independently proves the weight counts with Decimal
recurrences from the pinned LEAN EMA/Wilder formulas.

Inputs: the first 16,000 OHLCV minute rows of
`../../cross-engine-studies/cells/SPY_W12mo_2025-05-01_to_2026-04-30/lean/observations.csv`
(relative to this attribution file); that cell's manifest
and attribution retain its raw-data/reference capture provenance. File SHA256:
`677c3915d016b5ad821f90488baf912370b01888171cba36bde656960e7e5f7f`.
The synthetic path uses Python `random.Random(2857)`, Gaussian close increments
rounded to four decimals, and high/low one unit beyond the bar body. Timestamps
are distinct int64 UTC milliseconds; no calendar assumption enters the count.
The test names all periods and builds both paths. No external provider is read.

Each path has 125 cold starts at rows 2000, 2100, ..., 14400. The long-running
copy sees every preceding row. The ratio is mean absolute output disagreement
at the fixed count divided by mean disagreement at readiness, not an average
of unstable per-start ratios. MACD records line, signal, and histogram in that
order. SMA's initial and final errors are both zero. Comparisons pin every
measured value at absolute tolerance 1e-12, relative tolerance zero.

The real RSI(14) ratio is 0.0011096340871951176 at 109 bars, which exceeds a
0.001 output promise even though Wilder state memory is within the policy.
At Supertrend(10,3)'s ATR-only 76 bars, direction disagrees on 12/125 synthetic
starts and 0/125 real starts. The approved fixed 100-bar margin leaves 0/125
at 176 on both. `supertrend_atr_only` retains the original observation. Neither
sample proves direction agreement on every path, period, or multiplier.

Regenerate from PythonDataService (then inspect the measured changes):

```sh
POLYGON_API_KEY=test DATA_PLANE_CONTROL_SECRET='' .venv/bin/python - <<'PY'
import json
from tests.engine.indicators.test_convergence_twins import FACTORIES, RECEIPT, measure_twins
measurements = {source: {key: measure_twins(source, key) for key in FACTORIES} for source in ('real', 'synthetic')}
measurements['supertrend_atr_only'] = {source: measure_twins(source, 'supertrend10_3', count=76) for source in ('real', 'synthetic')}
RECEIPT.write_text(json.dumps(measurements, indent=2) + '\n')
PY
```
