---
id: F-0027
severity: P1
status: deferred
area: provenance
canonical_file: cross-cutting (PythonDataService canonical math)
reference: .claude/skills/learn-ai-validation/SKILL.md (Math Provenance Contract)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 4
---

## What

The Math Provenance Contract requires every canonical math file to carry a 4-field block: `Formula` / `Reference` / `Canonical implementation` / `Validated against`. Phase 4 grep across the canonical math directories returns:

| Directory | Files in scope (approx) | Files with any 4-field marker |
|---|---|---|
| `app/engine/indicators/` (sma, ema, rsi, macd, adx, supertrend, base, ...) | 7 | **1** (rsi.py) |
| `app/services/` (bs_greeks, quantlib_pricer, strategy_engine, fred_service, portfolio_scenario, sanitizer, ...) | ~20 | **0** in main services dir; 1 in `app/services/strategies/lean_statistics.py` |
| `app/volatility/` (solver, fitting, surface, basis, vix_replication, iv30_health, ...) | 14 | **0** |

**The provenance block is essentially missing across the canonical math surface.**

## Where

Static-grep evidence:

```
grep -c "Formula:|Reference:|Canonical implementation:|Validated against:" app/engine/indicators/*.py     # only rsi.py: 1
grep -c "Formula:|Reference:|Canonical implementation:|Validated against:" app/services/*.py             # 0
grep -c "Formula:|Reference:|Canonical implementation:|Validated against:" app/volatility/*.py           # 0
```

The skill's own anti-patterns list (`learn-ai-validation/SKILL.md` last section) starts with: "Math function with no provenance block after you've touched it." The repo currently has many of these.

## Why this severity

P1 — Missing-block-on-every-canonical-math is a coverage problem, not a correctness problem. The math itself works. But the skill explicitly says: "No block = not merged. The fields are grep-able; CI can enforce them later." The rule is set up to be enforced, and currently could not pass CI if such enforcement existed.

This is the **largest single hardening-gate item** by *number of files touched*. Closing it requires editing every canonical math file. Practically: per the registry's "Legacy-debt burn-down rule (not a backfill mandate)", these get added on touch — not in a single 30-file PR. But the §6 gate explicitly requires the blocks before nightly cron goes live.

## Reproduction

```
# Indicators
grep -nE 'Formula:|Reference:|Canonical implementation:|Validated against:' PythonDataService/app/engine/indicators/*.py
# Volatility (zero matches expected)
grep -nE 'Formula:|Reference:|Canonical implementation:|Validated against:' PythonDataService/app/volatility/*.py
# Services (only lean_statistics.py expected)
grep -rnE 'Formula:|Reference:|Canonical implementation:|Validated against:' PythonDataService/app/services/
```

## Suggested resolution (NOT auto-applied)

This is the **highest-cardinality remediation in the baseline**. Two strategies, pick one:

**A. Burn-down-on-touch (registry's documented preference).** Each PR that touches a canonical-math file adds the 4-field block in that PR. The §6 hardening gate then becomes satisfied gradually rather than in a single sweep.

**B. One bulk PR per directory.** Add the block to every file in `app/engine/indicators/`, then `app/services/{bs_greeks, quantlib_pricer, strategy_engine, ...}.py`, then `app/volatility/`. Each bulk PR is mechanical (no behavior change) and reviewable.

The repo CLAUDE.md and the skill itself prefer **A**. But the user's stated goal of clearing the §6 gate to enable hardening favors **B**.

Suggest: **B for `app/engine/indicators/` + the 5 named canonical service files** (bs_greeks, quantlib_pricer, strategy_engine, portfolio_scenario, fred_service), **A for the rest**. This produces the most authority-density coverage in the smallest set of PRs.

## Provenance of the finding itself

Phase 4 / cursor: cross-stack grep over canonical-math directories with the contract's 4 field labels. Counts confirm near-universal absence.
