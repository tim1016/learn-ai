---
id: F-0010
severity: P1
status: awaiting-human
area: python-authority
canonical_file: Backend/Services/Implementation/PositionEngine.cs
reference: missing
first_seen: 2026-05-05
last_seen: 2026-05-05
phase: 1
---

## What

`Backend/Services/Implementation/PositionEngine.cs` implements **FIFO lot-level accounting**: replays trades in chronological order, builds positions, computes realized PnL per lot closure, and per `PortfolioValidationService.cs` line 47 the test suite asserts "Verify FIFO lot engine produces correct realized PnL and lot closures" against this engine. **This is canonical math** (FIFO lot allocation, realized PnL, weighted-average cost basis) and is **not in `docs/math-sources-of-truth.md`**.

The registry has rows for "Position mark-to-market valuation" (PortfolioValuationService.cs, marked compliant) and "Portfolio reconciliation" (PortfolioReconciliationService.cs, marked pending rule-5 review), but no row for the lot-engine itself. Yet PositionEngine is what produces the realized PnL numbers everything else depends on.

## Where

- `Backend/Services/Implementation/PositionEngine.cs` (full file — `RebuildPositionsAsync`, `ApplyTradeInternal`)
- Validation suite that asserts on its outputs: `Backend/Services/Implementation/PortfolioValidationService.cs:47-49` ("FIFO Accounting Correctness")
- Registry: no row

## Why this severity

P1 — Per rule 5 (Python owns canonical math), an engine doing FIFO accounting in .NET is either a `legacy-ok` exception with a parity test naming a Python canonical, OR it should move to Python. Currently it's neither. The registry shows `PortfolioValuationService.cs::ComputeValuationInternal` as "compliant — pure aggregation" but PositionEngine is doing more than aggregation: it's doing lot-by-lot allocation under FIFO rules, which is a numerical decision rule.

Whether this is a "rule 5 violation" or a "well-justified .NET-resident persistence engine" is a judgment call. P1 captures "missing row in registry" regardless.

## Reproduction

```
grep -n 'PositionEngine' docs/math-sources-of-truth.md         # 0 matches
grep -n 'FIFO' docs/math-sources-of-truth.md                   # 0 matches
grep -nE 'class PositionEngine|RebuildPositions|ApplyTrade' Backend/Services/Implementation/PositionEngine.cs
```

## Suggested resolution (NOT auto-applied)

Add a row to the Portfolio / valuation section of `math-sources-of-truth.md`:

| Concept | Canonical | Legacy / duplicates | Reference | Validated against | Status |
|---|---|---|---|---|---|
| Lot-level FIFO accounting (realized PnL, cost-basis allocation) | `Backend/Services/Implementation/PositionEngine.cs` | — | Internal — standard FIFO accounting (cite GAAP / IFRS reference if used as audit defense) | `Backend.Tests/Unit/Services/PortfolioValidationServiceTests.cs::Test1_FifoAccounting` (or wherever the FIFO test lives) | **pending rule-5 review** |

Once classified, decide: stays in .NET (legacy-ok with parity test naming a Python lot engine), moves to Python (PythonDataService gets a `lot_engine.py` and .NET becomes a passthrough), or stays as the documented exception (rule-5-justified-in-.NET because the data lives in Postgres and round-tripping through Python on every trade replay is gratuitous).

## Provenance of the finding itself

Phase 1 / cursor: `Backend/Services/Implementation/PositionEngine.cs` head read. Cross-checked against registry.
