---
id: F-0005
severity: P1
status: fixed-verified
area: inventory
canonical_file: PythonDataService/app/engine/options/pricer.py
reference: docs/architecture/engine-authority-map.md (line 18, generic)
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`PythonDataService/app/engine/options/pricer.py` is a "Unified option pricer for the backtest engine" that wraps QuantLib (and market data) into a single interface, exposing `PricingMode` (`QUANTLIB_ONLY` / `MARKET_PREFERRED` / `MARKET_REQUIRED`), `OptionGreeks`, and `SpreadType`. It is the in-engine pricing entry point for the options backtest path. **It has no row in `docs/math-sources-of-truth.md`.**

The registry's options-pricing rows name `bs_greeks.py` and `quantlib_pricer.py` as canonical. `engine/options/pricer.py` is the **dispatcher** that decides between them at backtest time and that exposes a third pricing mode (`MARKET_REQUIRED`, only-real-market-data). Dispatchers are themselves a math-authority surface — choosing which engine answers a query is a numerical decision.

## Where

- File: `PythonDataService/app/engine/options/pricer.py` (44+ lines of dispatch logic visible in the head; full file likely longer)
- Authority map cite: `docs/architecture/engine-authority-map.md:18` (generic "options pricing routes through `bs_greeks.py` / `quantlib_pricer.py`" — does not name the dispatcher)
- Registry: no row

## Why this severity

P1 — Undocumented dispatch layer for options pricing. A reviewer asking "where does the engine decide whether to use QuantLib vs. market data for this backtest?" cannot find a registry pointer; they have to reverse it from the strategy code. The three `PricingMode` values represent three distinct numerical regimes, and the choice is consequential (mid vs. analytical theoretical produces materially different results in low-liquidity contracts).

## Reproduction

```
test -f PythonDataService/app/engine/options/pricer.py    # exit 0
grep -c "engine/options/pricer" docs/math-sources-of-truth.md   # 0
```

## Suggested resolution (NOT auto-applied)

Add a row in the options section of `math-sources-of-truth.md`:

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| In-engine option pricing dispatch (QuantLib-only / market-preferred / market-required) | `PythonDataService/app/engine/options/pricer.py` | — | Composes `bs_greeks.py` and `quantlib_pricer.py`; market path uses observed mid where present | (existing engine tests under `app/engine/tests/`, or `NONE — pending`) | canonical |

The provenance block on the file should also cite which path is preferred when both market data and QuantLib analytical agree, and what the divergence-tolerance is when they don't (this is a Phase-4 concern; recorded here for completeness).

## Provenance of the finding itself

Phase 1 / cursor: code-side scan of `PythonDataService/app/engine/**`. File header read with `Read` tool.

## Closure (2026-05-06)

Row added to `docs/math-sources-of-truth.md` § Options pricing and Greeks: "In-engine option pricing dispatch (QuantLib-only / market-preferred / market-required)" canonical = `app/engine/options/pricer.py`. Phase 4 follow-up tracked in F-0027.

