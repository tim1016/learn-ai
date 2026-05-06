# Portfolio reconciliation

**Concept**: Persistence-layer reconciliation — verifying that stored position records match the reconstructed position state from the trade log.

**Reference**: Internal — no external paper. This is event-sourcing-style determinism: replay all trades in chronological order, compare against the materialized position records.

**Canonical implementation**: `Backend/Services/Implementation/PortfolioReconciliationService.cs` (registry: § Portfolio / valuation).

**Status note**: `pending rule-5 review`. Same reasoning as portfolio valuation — persistence-layer logic over EF entities; .NET-resident is justified.

**Validation**: `Backend.Tests/Unit/Services/PortfolioReconciliationServiceTests.cs`; runtime suite `PortfolioValidationService.cs::Test2_RebuildDeterminism`.
