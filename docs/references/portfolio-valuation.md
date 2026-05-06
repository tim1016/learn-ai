# Portfolio valuation

**Concept**: Position mark-to-market valuation — aggregating position quantity × current price across an account's open positions.

**Reference**: Internal — no external paper. Elementary accounting arithmetic (Σ qty_i × price_i, with FX conversion when multi-currency, and option theoretical when applicable).

**Canonical implementation**: `Backend/Services/Implementation/PortfolioValuationService.cs::ComputeValuationInternal` (registry: § Portfolio / valuation).

**Status note**: Currently `pending rule-5 review` per `docs/math-sources-of-truth.md` because the implementation lives in .NET on EF-tracked entities. The registry rule is: if/when this method computes FX, option theoretical from cost-basis lots, or any non-trivial math beyond pure aggregation, the canonical moves to Python and this path becomes a passthrough. Until then, .NET-resident is acceptable per the contract's "math may live in any layer that fits the use case" rule.

**Cross-references**:
- `Backend/Services/Implementation/PositionEngine.cs` (FIFO lot accounting that produces the position records this method aggregates over) — see `docs/references/fifo-accounting.md`
- `PortfolioRiskService.cs::RunScenarioAsync` (Phase 2 — what-if scenario math now in Python passthrough)
- Live Greeks via `IPolygonService.PortfolioLiveGreeksAsync`

**Validation**: `Backend.Tests/Unit/Services/PortfolioValuationServiceTests.cs`; runtime suite `PortfolioValidationService.cs::Test4_UnrealizedPnLValuation`.
