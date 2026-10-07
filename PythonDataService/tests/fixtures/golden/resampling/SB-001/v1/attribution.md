# SB-001 — Stationary bootstrap of a paired, serially dependent series

## Source

arch 8.0.0, `arch.bootstrap.StationaryBootstrap`: the stationary bootstrap of Politis & Romano, "The Stationary Bootstrap", Journal of the American Statistical Association 89(428), 1994, 1303-1313. Run under numpy 2.2.6. Generated 2026-10-06.

## What is in it

`input.arrow` holds two synthetic daily return series of 250 observations (seed 20261006), no market data. `a` is an AR(1) series: coefficient 0.3, noise of mean 0.0004 and standard deviation 0.01. `b` is 0.6 times `a` plus its own noise of mean 0.0002 and standard deviation 0.008. The pair is dependent in time and on each other.

`output.arrow` holds, over 200,000 arch resamples at a mean block of 5 (seed 20261006), the 5th, 25th, 50th, 75th and 95th percentile (numpy's linear interpolation) of three statistics of a resample:

- `mean`: the mean of `a`;
- `sharpe`: the annualized Sharpe of `a`, mean over sample standard deviation (n - 1) times the square root of 252;
- `sharpe_difference`: the Sharpe of `a` less the Sharpe of `b` on the same resampled rows.

## Assumptions

- arch's `block_size` is the mean block length. At each step it starts a new block at a uniformly drawn row with probability 1 / `block_size`, and otherwise takes the next row, going from the last row back to the first. Read from arch 8.0.0's `bootstrap/base.py` (`StationaryBootstrap._p`) and `bootstrap/_samplers_python.py`. The canonical draw follows the same rule.
- Both series are passed to arch together, so each resample takes the same rows of both: the pair stays paired.
- Each resample is as long as the series, 250 rows.
- The two sides draw from different random streams. No draw is compared with a draw; only the two distributions are.

## Independent numerical oracle

`reference_kind=external_reference`. The generator imports arch, numpy and pyarrow only. It never imports the canonical draw, and it computes Sharpe with its own formula.

arch is generation-only: it is in no requirements file and nothing under `app/` or `tests/` imports it.

## Tolerance

`atol=0.008, rtol=0` on a share (the owner's decision 4 on issue #2932). This is a Monte Carlo band, not float equality.

The test draws 200,000 resamples with the canonical draw and takes the share of them at or below each of arch's quantiles. That share should be the quantile's level. Each side places a share to sqrt(p(1 - p) / 200,000), at most 0.00112, so their difference has a standard error of at most 0.0016. The tolerance is five of those.

Measured on generation: the largest gap was 0.0024, and 0.0023 to 0.0024 on three other seeds of the canonical draw.

What the fixture catches, measured the same way: the ordinary bootstrap (a gap of 0.064); a mean block of 3 or less, or of 8 or more (0.015 to 0.064); and drawing the two series with different rows (0.17). It catches a mean block of 4 only just (0.009) and does not tell a mean block of 6 from 5 (0.006). The block length itself is held by the restart-share and closed-form variance tests in `tests/research/monte_carlo/test_stationary.py`.

A miss is a finding, never a reason to widen the band: it means the canonical draw and arch disagree about the method.

## Regeneration

From the repo root, in a throwaway environment:

```
python3.12 -m venv "$TMPDIR/sb-001"
"$TMPDIR/sb-001/bin/pip" install arch==8.0.0 numpy==2.2.6 pyarrow==19.0.1
"$TMPDIR/sb-001/bin/python" PythonDataService/scripts/fixture_generators/stationary_bootstrap.py
```

The generator refuses to write from any other arch or numpy, and two runs write the same bytes. Then record the new hashes in `tests/fixtures/golden/manifest.json`. A regeneration commit must say what changed and why (never regenerate to make a test pass).
