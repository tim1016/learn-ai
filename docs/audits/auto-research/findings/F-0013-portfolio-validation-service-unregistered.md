---
id: F-0013
severity: P2
status: open
area: inventory
canonical_file: Backend/Services/Implementation/PortfolioValidationService.cs
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`Backend/Services/Implementation/PortfolioValidationService.cs` runs an in-process "validation suite" of accounting tests (FIFO accounting, position rebuild determinism, cash accounting integrity, unrealized PnL valuation, etc.). It is invoked at runtime (not as a unit test) and produces a `ValidationSuiteResult`. **It is not in the registry.**

This is a borderline classification: it asserts on numerical math (the FIFO PnL of F-0010, the mark-to-market of `PortfolioValuationService.cs`) but it is itself test infrastructure rather than canonical math. Per the contract, it's "validation-only" in the sense `engine-authority-map.md` uses for `StrategyAttributionService.cs` — present and not deprecated, with a clean separate purpose.

## Where

- `Backend/Services/Implementation/PortfolioValidationService.cs` — the suite (Test1..Test4 visible in head; full test list longer)
- `engine-authority-map.md` § "Validation-only paths" — does not list this service

## Why this severity

P2 — The service exists and works; what's missing is its classification. A reviewer asking "what does PortfolioValidationService do?" needs to read the code; one row in the authority map would answer it.

## Reproduction

Static.

## Suggested resolution (NOT auto-applied)

Add a row to `engine-authority-map.md` § "Validation-only paths (not deprecated; have a clean separate purpose)":

| Path | Why it stays | What it must NOT do |
|---|---|---|
| `Backend/Services/Implementation/PortfolioValidationService.cs` | Runtime validation suite that asserts FIFO accounting, position rebuild determinism, cash accounting, unrealized PnL valuation | Be the canonical implementation of any of those quantities — it's a checker, not a producer. |

Optionally add a parallel row to `math-sources-of-truth.md` § "Data / transport".

## Provenance of the finding itself

Phase 1 / cursor: `Backend/Services/Implementation/PortfolioValidationService.cs` head read.
