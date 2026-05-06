# FIFO lot-level accounting

**Concept**: Allocating fills to positions using First-In-First-Out lot ordering. Each open lot tracks (qty, cost, timestamp); on a closing fill, allocate against the oldest lot first; realized PnL = (exit - entry) × qty per closed lot; weighted-average cost basis = Σ(lot_qty × lot_cost) / Σ(lot_qty); net position = sum of remaining lot quantities.

**Reference**: Standard FIFO inventory method per **GAAP** (Generally Accepted Accounting Principles) and **IFRS** (International Financial Reporting Standards). No external software port — this is well-known accounting arithmetic with one canonical correct interpretation. Citations are textbook-level (any introductory accounting text — e.g., Kieso, Weygandt & Warfield, *Intermediate Accounting*).

**Canonical implementation**: `Backend/Services/Implementation/PositionEngine.cs::{RebuildPositionsAsync, ApplyTradeInternal}` (registry: § Portfolio / valuation; status `canonical-in-dotnet-justified` per finding F-0010).

**Why .NET-resident** (the contract's "math may live in any layer that fits the use case" rule):
1. Lot records are EF-tracked entities persisted in Postgres. The natural transaction boundary is `DbContext.SaveChangesAsync`.
2. Trade replay (`RebuildPositionsAsync`) reads all trades for an account and writes the resulting lots + positions back. This is a database transaction shape, not a math computation that wants to live in a numerical sandbox.
3. Round-tripping every trade through Python would mean: serialize trade to JSON → HTTP POST to Python → Python computes lot allocation → HTTP response → .NET writes lots to DB. Five hops to do one accounting operation. No simplification benefit.
4. There is no upstream Python implementation to delegate to (FIFO is not in `app/engine/` — that engine deals with bars and orders, not portfolio lots).

**Validation**:
- `Backend.Tests/Unit/Services/PositionEngineTests.cs` — unit tests for FIFO determinism, mark-to-market, partial-fill cost basis.
- `Backend/Services/Implementation/PortfolioValidationService.cs::Test1_FifoAccounting` — runtime validation suite asserting realized PnL and lot closures.

**What would change this classification**: If lot allocation ever needed to compute FX conversion, option-on-stock cost basis with multiplier, or any non-trivial math beyond plain FIFO arithmetic, the math layer of this would move to Python and the .NET side would become a passthrough that aggregates Python results into EF entities.
