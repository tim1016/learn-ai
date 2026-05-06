---
id: F-0031
severity: P3
status: fixed-verified
area: documentation
canonical_file: PythonDataService/app/engine/indicators/macd.py
reference: .claude/rules/numerical-rigor.md (Warmup rigor)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 10
---

## What

Phase 10 grep for warmup-related text in `app/engine/indicators/`. 5 of 7 indicator files have warmup documentation:

```
adx.py, ema.py, rsi.py, sma.py, supertrend.py    — match warmup keywords
base.py, macd.py                                  — no match
```

`base.py` is the base class — N/A. `macd.py` is a real indicator with a warmup phase (default 26 bars for slow EMA + 9 for signal = 35 bars before MACD signal is meaningful). Per `.claude/rules/numerical-rigor.md` → Warmup rigor: *"Warmup behavior documented in the module docstring of every indicator: 'Emits valid output starting at bar index N-1 (0-indexed), where N is the window length. First value is seeded as the first input.'"*

## Where

- `PythonDataService/app/engine/indicators/macd.py` — module/class docstring missing warmup spec.

## Why this severity

P3 — Documentation gap; math is correct (MACD is heavily-tested via `tests/test_macd.py`). Per the severity heuristic in `baseline-math-rigor.md` §9: "Missing warmup docstring on an indicator → P3 (rollup)."

This finding is the rollup placeholder. As more indicators are written or audited, the same finding can absorb additional warmup-docstring gaps via `last_seen` updates rather than new finding files.

## Reproduction

```
grep -lE 'warmup|Warmup|emits valid output|first.*N.*bars|seed.*first' PythonDataService/app/engine/indicators/*.py
# Expect: 5 of 7 files. Missing: macd.py (and base.py, which is N/A).
```

## Suggested resolution (NOT auto-applied)

Add a warmup section to `macd.py` module docstring matching the format in `rsi.py` or `ema.py`:

```python
"""MACD (Moving Average Convergence/Divergence).

...

Warmup: emits NaN for bars 0..max(slow, fast) - 1; first valid `macd` line
appears at bar index `slow - 1`. The `signal` line additionally requires
`signal_period` bars of valid `macd` values, so emits NaN until bar
index `slow - 1 + signal_period - 1`.
"""
```

Cross-reference: when F-0027 (provenance block) is addressed, the warmup line should be part of that same edit.

## Provenance of the finding itself

Phase 10 / cursor: `Grep("warmup|Warmup|emits valid output|...", path=app/engine/indicators)` returned 5 of 7 files.

## Closure (2026-05-06) — false positive

Inspection of `macd.py` shows the warmup behavior **is** documented in the module docstring, just not using the keyword "warmup". Lines 12–22 read:

```
* MACD line = fast_ema.current - slow_ema.current. Emitted only
  once both EMAs are ready (samples >= slow_period).
* Signal line = EMA(signal_period) of the MACD line. The signal
  EMA starts receiving samples at the first bar the MACD line is defined.
* is_ready when the signal line is ready (i.e.
  samples >= slow_period + signal_period - 1 with default
  parameters 12/26/9 this is sample 34).
```

This is warmup documentation in "ready" semantics rather than "warmup" keyword. The spirit of `.claude/rules/numerical-rigor.md` → Warmup rigor is that warmup behavior is clear from the docstring; macd.py satisfies that.

Closing as **false positive due to grep keyword limitation** rather than `wontfix`. No code change needed. Future warmup audits should grep for `ready`, `samples >=`, or other equivalent phrasings as well as the literal `warmup` keyword.

