---
id: F-0026
severity: P1
status: open
area: fixture
canonical_file: PythonDataService/tests/fixtures/golden/
reference: .claude/rules/numerical-rigor.md (Golden fixtures); docs/math-sources-of-truth.md
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 5
---

## What

`PythonDataService/tests/fixtures/golden/` contains exactly **3 fixture directories**:

```
tests/fixtures/golden/bs-price-cross-engine/   { attribution.md, cases.json }
tests/fixtures/golden/iv30/                    { spy-2024-12-20-chain.meta.json, *.parquet }
tests/fixtures/golden/portfolio-scenario-3leg/ { attribution.md, cases.json }
```

`docs/math-sources-of-truth.md` lists **dozens** of canonical math concepts. Cross-checking the registry against the fixture directory shows that the overwhelming majority of canonical math has **no golden fixture under `tests/fixtures/golden/`**. Per `.claude/rules/numerical-rigor.md`: "Every port from a reference source ships with (a) a golden fixture test, (b) a `docs/references/` note, (c) the tolerance used and why."

## Where

### Concepts with NO fixture (sample, not exhaustive)

- **Indicators (LEAN-ported):** SMA, EMA, RSI (Wilders), MACD, Bollinger Bands, ADX, Supertrend
  - Registry says `Validated against: PythonDataService/tests/test_indicator_parity.py`. That test exists but produces parity *programmatically* (not via fixture). No `tests/fixtures/golden/sma_*/` etc.
- **Greeks cross-engine parity** (registry: `pending-fixture`)
- **IV solver cross-engine parity** (registry: `pending-fixture`)
- **IV term-structure interpolation** (registry: `pending-fixture`)
- **Bar consolidation, event replay, fill models** (registry: `pending-migration` with no fixture pointer)
- **Max drawdown, Sharpe ratio** (registry: `pending-migration` with `Backend.Tests` only)
- **Strategies:** SPY EMA Crossover, SPY ORB, RSI Mean Reversion, SMA Crossover (none have a fixture under `tests/fixtures/golden/`; tests call into the engine directly)
- **Trade divergence** (registry: `pending-fixture`)
- **Dividend adjustment** (registry: `pending-fixture` + reference is "CRSP placeholder")

### Concepts with fixture but missing attribution

- **`iv30/`** — has `meta.json` and `.parquet` but no `attribution.md`. Per `.claude/rules/numerical-rigor.md` → Golden fixtures → Required contents: each fixture must include reference source, generation date, command used to regenerate, and parameters.

## Why this severity

P1 — The Math Provenance Contract requires golden fixtures for ported math. The registry already openly tracks 5 `pending-fixture` rows. The actual coverage is worse: **most canonical implementations** (indicators, strategies, statistics) have no fixture at all, even when the registry doesn't flag them as `pending-fixture`. The registry implies "validated against test X" but those tests don't follow the golden-fixture pattern — they assert programmatically.

This is the **largest single finding** in the baseline by remediation cost. Building golden fixtures for every canonical math is weeks of work. Closing it is a precondition for the §6 hardening gate.

Bumping to P0 is defensible because the registry says some canonicals are NOT pending-fixture (e.g., SMA, EMA, RSI marked "canonical") but on inspection they have no fixture either. That is the registry lying about a fixture that doesn't exist — the Phase-5 P0 trigger.

I'm holding at P1 because the underlying parity tests (`test_indicator_parity.py`) likely cover the math correctly even without a fixture file on disk; the *contract* failure is documentation, not correctness. A human should rule on whether to escalate.

## Reproduction

```
ls PythonDataService/tests/fixtures/golden/
# Compare to canonical-math rows in:
grep -E '^\| [A-Z]' docs/math-sources-of-truth.md
```

## Suggested resolution (NOT auto-applied)

This is a **multi-week remediation**, not a one-PR fix. Recommended sequencing:

1. **Add `attribution.md` to `iv30/`** (immediate; trivial).
2. **Audit `test_indicator_parity.py`** — confirm whether it loads golden fixtures or computes parity programmatically. If the latter, decide whether the parity-test pattern is "fixture-equivalent" (and update the registry's "fixture" requirement to reflect that), or whether to backfill golden fixtures.
3. **Backfill golden fixtures one canonical at a time on touch** (consistent with the registry's "Legacy-debt burn-down rule (not a backfill mandate)" — don't open one PR that creates 30 fixtures, accept the debt and burn it down as files are touched).
4. **For each `pending-fixture` registry row, write a follow-up issue** (or finding doc) with the specific reference source, generation command, and target tolerance. The `pending-fixture` status without any of those metadata is itself thin.

## Provenance of the finding itself

Phase 5 / cursor: `Glob("PythonDataService/tests/fixtures/golden/**/*")` returned 6 paths in 3 directories. Cross-checked against the dozens of canonical-math rows in `docs/math-sources-of-truth.md`.
