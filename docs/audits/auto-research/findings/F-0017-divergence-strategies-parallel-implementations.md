---
id: F-0017
severity: P2
status: fixed-verified
area: inventory
canonical_file: PythonDataService/app/research/divergence/strategies/{s1,s2,s3}_*.py
reference: docs/math-sources-of-truth.md (Strategies section, canonical engine algorithms)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/research/divergence/strategies/` contains three vectorized pandas implementations that explicitly mirror engine-canonical strategies:

- `s1_ema_crossover.py` — module docstring: *"Mirrors the rule-set of `app.engine.strategy.algorithms.spy_ema_crossover` but in a vectorized pandas form so we can drive it against TV, native, and engine indicator columns without touching the LEAN streaming loop."*
- `s2_rsi_mean_reversion.py` — RSI(14) mean reversion, parallels `rsi_mean_reversion.py`.
- `s3_sma_crossover.py` — SMA(50)/SMA(200), parallels `sma_crossover.py`.

These are **second implementations** of canonical strategies — exactly the rule-5 pattern that requires a registry row in the duplicates column with a parity test.

## Where

- `PythonDataService/app/research/divergence/strategies/s1_ema_crossover.py`
- `PythonDataService/app/research/divergence/strategies/s2_rsi_mean_reversion.py`
- `PythonDataService/app/research/divergence/strategies/s3_sma_crossover.py`
- `PythonDataService/app/research/divergence/strategies/common.py` (shared `Trade`, `TradeList`)

## Why this severity

P2 — The files are honest about their relationship (s1's docstring explicitly states it mirrors the canonical). They serve a research-divergence-checker purpose distinct from the engine. But they should be in the registry as `legacy-ok` (with parity test asserting they reproduce the engine canonical for the same input) or as `divergence-research-only` (with an explicit note that they are not authority and aren't expected to match exactly).

## Reproduction

```
head -10 PythonDataService/app/research/divergence/strategies/s1_ema_crossover.py    # docstring acknowledges parallel
grep -c 'divergence/strategies' docs/math-sources-of-truth.md                         # 0
```

## Suggested resolution (NOT auto-applied)

Update the three Strategies rows in `math-sources-of-truth.md` to add the s1/s2/s3 files as legacy-ok or divergence-research-only:

- SPY EMA Crossover row: add `app/research/divergence/strategies/s1_ema_crossover.py` to "Legacy / duplicates" with status `divergence-research-only` (per file docstring, this is a vectorized parallel for divergence checking, not the authority).
- RSI Mean Reversion row: same for `s2_rsi_mean_reversion.py`.
- SMA Crossover row: same for `s3_sma_crossover.py`.

Add a note to each: parity tests, if any, live in `app/research/divergence/`. If no parity test exists today, mark `Validated against: NONE — pending`.

## Provenance of the finding itself

Phase 1 / cursor: `app/research/divergence/strategies/s{1,2,3}_*.py` head reads.

## Closure (2026-05-06)

Three existing strategy rows in `docs/math-sources-of-truth.md` § Strategies updated to add the divergence-research parallels in their Legacy column with status `divergence-research-only`:

- SPY EMA Crossover row → adds `s1_ema_crossover.py`
- RSI Mean Reversion row → adds `s2_rsi_mean_reversion.py`
- SMA Crossover row → adds `s3_sma_crossover.py`

Each notes the file is "vectorized parallel for divergence checking; not the authority and not expected to match exactly." Parity tests (if any) live separately under `app/research/divergence/`.

