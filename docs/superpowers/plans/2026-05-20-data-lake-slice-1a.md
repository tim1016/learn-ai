# Data Lake Slice 1a Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the foundation of the Polygon → LEAN data lake — Postgres catalog schema, typed path policy, Python schema expectation, schema-drift test, and a fixture-backed `ensure_data` skeleton gated behind a feature flag. No real Polygon fetching, no atomic writes, no leases (those land in Slice 1b).

**Architecture:** Backend (.NET) owns the EF Core migrations for `data_lake_artifacts` and `data_lake_runs`. Python's new `app/data_lake/` module declares the schema it expects (`catalog_schema.py`), connects to Postgres via asyncpg (`catalog_client.py`), and exposes a fixture-backed `ensure_data` function returning canned `DataAvailabilityResult` for known inputs. A new FastAPI route `POST /api/data-lake/ensure-data` is wired behind a `DATA_LAKE_ENABLED` env var (defaults off — route returns 404). No code outside `app/data_lake/` knows the new table or route exists; production paths are unchanged.

**Tech Stack:** .NET 10 + Hot Chocolate v15 + EF Core (Backend); FastAPI + Pydantic v2 + asyncpg (Python); Postgres 16; pytest + pytest-asyncio + respx (tests).

**Spec:** [docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md](../specs/2026-05-20-polygon-lean-data-lake-design.md)

---

## File structure

### New files

| File | Responsibility |
|---|---|
| `Backend/Models/MarketData/DataLakeArtifact.cs` | EF Core entity for `data_lake_artifacts` |
| `Backend/Models/MarketData/DataLakeRun.cs` | EF Core entity for `data_lake_runs` |
| `Backend/Migrations/<timestamp>_AddDataLakeArtifactsAndRuns.cs` | EF migration creating both tables, partial indexes, CHECK constraints |
| `PythonDataService/app/data_lake/__init__.py` | Package marker |
| `PythonDataService/app/data_lake/types.py` | Pydantic models: `DataRunSpec`, `ArtifactIdentity`, `ArtifactRecord`, `ArtifactFailure`, `NonSessionRecord`, `DataAvailabilityResult` |
| `PythonDataService/app/data_lake/path_policy.py` | Typed LEAN-path dataclasses + `relative_path_for_artifact` dispatch + `staging_path_for` utility |
| `PythonDataService/app/data_lake/catalog_schema.py` | Typed declaration of the expected Postgres schema (columns, constraints, indexes) |
| `PythonDataService/app/data_lake/catalog_client.py` | asyncpg connection pool + `select_coverage()` query |
| `PythonDataService/app/data_lake/sessions.py` | `trading_sessions_for()` — fixture-backed in 1a (returns weekday non-holiday days from a small US calendar); replaced by LEAN market-hours-database in 1c |
| `PythonDataService/app/data_lake/ensure_data.py` | `expand_required_artifacts()` + `ensure_data()` — fixture-backed canned responses |
| `PythonDataService/app/data_lake/fake_polygon.py` | Test-only canned-response stub used by `ensure_data` skeleton |
| `PythonDataService/app/routers/data_lake.py` | `POST /api/data-lake/ensure-data` route behind `DATA_LAKE_ENABLED` flag |
| `PythonDataService/tests/unit/data_lake/__init__.py` | Test package marker |
| `PythonDataService/tests/unit/data_lake/test_path_policy.py` | Unit tests for path_policy |
| `PythonDataService/tests/unit/data_lake/test_types.py` | Unit tests for Pydantic model validation |
| `PythonDataService/tests/unit/data_lake/test_no_lean_paths_outside_policy.py` | Lint test grep-banning LEAN path substrings outside `path_policy.py` |
| `PythonDataService/tests/unit/data_lake/test_sessions.py` | Unit tests for trading_sessions_for |
| `PythonDataService/tests/unit/data_lake/test_expand_required_artifacts.py` | Unit tests for `expand_required_artifacts` |
| `PythonDataService/tests/unit/data_lake/test_ensure_data.py` | Unit tests for `ensure_data` with the fake-Polygon stub |
| `PythonDataService/tests/integration/data_lake/__init__.py` | Test package marker |
| `PythonDataService/tests/integration/data_lake/test_schema_drift.py` | Live-DB introspection vs `catalog_schema.py` expectation |
| `PythonDataService/tests/integration/data_lake/test_ensure_data_route.py` | Route + ensure_data end-to-end against fixture-backed Polygon |
| `PythonDataService/tests/fixtures/data_lake_skeleton/canned_response.json` | Canned fixture response for the skeleton |

### Modified files

| File | Change |
|---|---|
| `Backend/Data/AppDbContext.cs` | Add `DbSet<DataLakeArtifact>` and `DbSet<DataLakeRun>`; register configuration in `OnModelCreating` |
| `PythonDataService/app/config.py` | Add `POSTGRES_URL` and `DATA_LAKE_ENABLED` settings |
| `PythonDataService/app/main.py` | Conditionally include `data_lake` router when `DATA_LAKE_ENABLED` is true |
| `PythonDataService/requirements-light.txt` | Add `asyncpg` |

---

## Tasks

### Task 1: EF Core entity — `DataLakeArtifact`

**Files:**
- Create: `Backend/Models/MarketData/DataLakeArtifact.cs`

- [ ] **Step 1: Write the failing test**

```csharp
// Backend.Tests/Unit/Models/DataLakeArtifactTests.cs
using Backend.Models.MarketData;
using Xunit;

namespace Backend.Tests.Unit.Models;

public class DataLakeArtifactTests
{
    [Fact]
    public void DataLakeArtifact_Defaults_AreSane()
    {
        var artifact = new DataLakeArtifact
        {
            ArtifactKind = "time_series_bars",
            Symbol = "SPY",
            Market = "usa",
            Resolution = "minute",
            DataType = "trade",
            Provider = "polygon",
            ProviderParams = "{}",
            PriceAdjustmentMode = "raw",
            DataContractHash = new string('a', 64),
            FilePath = "equity/usa/minute/spy/20240520_trade.zip",
            Status = "fetching",
            FetchedAtMs = 1_700_000_000_000L,
        };

        Assert.Equal(0, artifact.AttemptCount);
        Assert.Null(artifact.CompletedAtMs);
        Assert.Equal("time_series_bars", artifact.ArtifactKind);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd Backend.Tests && dotnet test --filter FullyQualifiedName~DataLakeArtifactTests`
Expected: FAIL — `DataLakeArtifact` type not found.

- [ ] **Step 3: Implement the entity class**

```csharp
// Backend/Models/MarketData/DataLakeArtifact.cs
using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

/// <summary>
/// Catalog row for a single physical artifact in the Polygon → LEAN data lake.
/// Written by Python <c>app/data_lake/catalog_client.py</c> via asyncpg; read
/// by both Backend (for coverage queries) and Python.
/// Schema authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.1
/// </summary>
public class DataLakeArtifact
{
    public long Id { get; set; }

    [Required]
    [MaxLength(40)]
    public string ArtifactKind { get; set; } = "";

    [MaxLength(20)]
    public string? Market { get; set; }

    [MaxLength(20)]
    public string? Symbol { get; set; }

    public DateOnly? TradingDate { get; set; }

    [MaxLength(20)]
    public string? Resolution { get; set; }

    [MaxLength(20)]
    public string? DataType { get; set; }

    [Required]
    [MaxLength(40)]
    public string Provider { get; set; } = "";

    [Required]
    [Column(TypeName = "jsonb")]
    public string ProviderParams { get; set; } = "{}";

    [MaxLength(40)]
    public string? PriceAdjustmentMode { get; set; }

    [Required]
    [MaxLength(64)]
    public string DataContractHash { get; set; } = "";

    public int? RowCount { get; set; }

    public long? FirstBarStartMs { get; set; }

    public long? LastBarStartMs { get; set; }

    [MaxLength(64)]
    public string? CorpActionRevision { get; set; }

    [Required]
    public string FilePath { get; set; } = "";

    public long? FileSizeBytes { get; set; }

    [MaxLength(64)]
    public string? FileSha256 { get; set; }

    [Required]
    [MaxLength(20)]
    public string Status { get; set; } = "fetching";

    [MaxLength(128)]
    public string? LeaseOwner { get; set; }

    public long? LeaseExpiresAtMs { get; set; }

    public int AttemptCount { get; set; } = 0;

    public string? LastError { get; set; }

    public string? ErrorMessage { get; set; }

    public long FetchedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd Backend.Tests && dotnet test --filter FullyQualifiedName~DataLakeArtifactTests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Backend/Models/MarketData/DataLakeArtifact.cs Backend.Tests/Unit/Models/DataLakeArtifactTests.cs
git commit -m "feat(data-lake): add DataLakeArtifact EF entity"
```

---

### Task 2: EF Core entity — `DataLakeRun`

**Files:**
- Create: `Backend/Models/MarketData/DataLakeRun.cs`

- [ ] **Step 1: Write the failing test**

```csharp
// Backend.Tests/Unit/Models/DataLakeRunTests.cs
using System;
using Backend.Models.MarketData;
using Xunit;

namespace Backend.Tests.Unit.Models;

public class DataLakeRunTests
{
    [Fact]
    public void DataLakeRun_Defaults_AreSane()
    {
        var run = new DataLakeRun
        {
            Id = Guid.NewGuid(),
            RunType = "python_lab",
            RunSpec = "{}",
            RequestedAtMs = 1_700_000_000_000L,
        };

        Assert.Null(run.StrategyExecutionId);
        Assert.Null(run.EngineRunId);
        Assert.Null(run.StartedAtMs);
        Assert.Equal("python_lab", run.RunType);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd Backend.Tests && dotnet test --filter FullyQualifiedName~DataLakeRunTests`
Expected: FAIL — `DataLakeRun` not found.

- [ ] **Step 3: Implement the entity**

```csharp
// Backend/Models/MarketData/DataLakeRun.cs
using System;
using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

/// <summary>
/// Audit row for a UI-initiated backtest run. Links to <see cref="StrategyExecution"/>
/// when the engine row materializes.
/// Schema authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.2
/// </summary>
public class DataLakeRun
{
    public Guid Id { get; set; }

    public int? StrategyExecutionId { get; set; }
    public StrategyExecution? StrategyExecution { get; set; }

    [MaxLength(128)]
    public string? EngineRunId { get; set; }

    [Required]
    [MaxLength(20)]
    public string RunType { get; set; } = "";

    [Required]
    [Column(TypeName = "jsonb")]
    public string RunSpec { get; set; } = "{}";

    public string? WorkspacePath { get; set; }

    [MaxLength(64)]
    public string? ManifestSha256 { get; set; }

    [MaxLength(64)]
    public string? DataAvailabilityHash { get; set; }

    [MaxLength(20)]
    public string? EnsureDataStatus { get; set; }

    [Column(TypeName = "jsonb")]
    public string? EnsureDataResponse { get; set; }

    [MaxLength(20)]
    public string? EngineStatus { get; set; }

    public long RequestedAtMs { get; set; }

    public long? StartedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd Backend.Tests && dotnet test --filter FullyQualifiedName~DataLakeRunTests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Backend/Models/MarketData/DataLakeRun.cs Backend.Tests/Unit/Models/DataLakeRunTests.cs
git commit -m "feat(data-lake): add DataLakeRun EF entity"
```

---

### Task 3: Wire DbSets and configure schema in `AppDbContext`

**Files:**
- Modify: `Backend/Data/AppDbContext.cs`

- [ ] **Step 1: Add DbSets**

Open `Backend/Data/AppDbContext.cs`. After the `// Backtesting models` block (`public DbSet<BacktestTrade> BacktestTrades => Set<BacktestTrade>();`), add:

```csharp
    // Data lake catalog (Slice 1a)
    public DbSet<DataLakeArtifact> DataLakeArtifacts => Set<DataLakeArtifact>();
    public DbSet<DataLakeRun> DataLakeRuns => Set<DataLakeRun>();
```

- [ ] **Step 2: Add a new configuration method**

After the existing `ConfigureMarketDataModels` method, add a new private static method:

```csharp
    /// <summary>
    /// Configure the data lake catalog tables. The CHECK constraints and
    /// partial unique indexes are declared in raw SQL because EF Core's
    /// fluent API does not express them natively. The migration's Up()
    /// emits the same SQL — this configuration block is the authoritative
    /// EF model state used by the schema-drift test on the Python side.
    /// </summary>
    private static void ConfigureDataLakeModels(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<DataLakeArtifact>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.ArtifactKind).IsRequired().HasMaxLength(40);
            entity.Property(a => a.Provider).IsRequired().HasMaxLength(40);
            entity.Property(a => a.ProviderParams).IsRequired().HasColumnType("jsonb");
            entity.Property(a => a.DataContractHash).IsRequired().HasMaxLength(64).IsFixedLength();
            entity.Property(a => a.FilePath).IsRequired();
            entity.Property(a => a.Status).IsRequired().HasMaxLength(20);
            entity.Property(a => a.FetchedAtMs).IsRequired();
            entity.Property(a => a.AttemptCount).IsRequired().HasDefaultValue(0);

            entity.Property(a => a.FileSha256).HasMaxLength(64).IsFixedLength();
            entity.Property(a => a.CorpActionRevision).HasMaxLength(64).IsFixedLength();

            // Hot-path lookups (partial indexes added via raw SQL in the migration).
            entity.HasIndex(a => new { a.Market, a.Symbol, a.Resolution, a.DataType, a.TradingDate });
        });

        modelBuilder.Entity<DataLakeRun>(entity =>
        {
            entity.HasKey(r => r.Id);
            entity.Property(r => r.RunType).IsRequired().HasMaxLength(20);
            entity.Property(r => r.RunSpec).IsRequired().HasColumnType("jsonb");
            entity.Property(r => r.RequestedAtMs).IsRequired();

            entity.Property(r => r.EnsureDataResponse).HasColumnType("jsonb");
            entity.Property(r => r.ManifestSha256).HasMaxLength(64).IsFixedLength();
            entity.Property(r => r.DataAvailabilityHash).HasMaxLength(64).IsFixedLength();

            entity.HasOne(r => r.StrategyExecution)
                  .WithMany()
                  .HasForeignKey(r => r.StrategyExecutionId)
                  .OnDelete(DeleteBehavior.SetNull);

            entity.HasIndex(r => r.StrategyExecutionId);
        });
    }
```

- [ ] **Step 3: Call the new method from `OnModelCreating`**

Update `OnModelCreating`:

```csharp
    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        ConfigureMarketDataModels(modelBuilder);
        ConfigureDataLabModels(modelBuilder);
        ConfigurePortfolioModels(modelBuilder);
        ConfigureDataLakeModels(modelBuilder);
    }
```

- [ ] **Step 4: Verify the build compiles**

Run: `dotnet build podman.sln`
Expected: build succeeds, no warnings about the new entity.

- [ ] **Step 5: Run all existing Backend tests to confirm nothing regressed**

Run: `cd Backend.Tests && dotnet test`
Expected: PASS (existing baseline; no new failures introduced).

- [ ] **Step 6: Commit**

```bash
git add Backend/Data/AppDbContext.cs
git commit -m "feat(data-lake): register DataLakeArtifact + DataLakeRun in DbContext"
```

---

### Task 4: Generate and finalize the EF migration

**Files:**
- Create: `Backend/Migrations/<timestamp>_AddDataLakeArtifactsAndRuns.cs` (and `.Designer.cs`, and the updated `AppDbContextModelSnapshot.cs`)

- [ ] **Step 1: Run the migration generator**

Run (from repo root):
```bash
podman exec my-backend dotnet ef migrations add AddDataLakeArtifactsAndRuns --output-dir Migrations --project /app
```
Expected: three files created/updated under `Backend/Migrations/`.

- [ ] **Step 2: Open the generated `<timestamp>_AddDataLakeArtifactsAndRuns.cs`**

Verify the `Up()` method contains `CreateTable("DataLakeArtifacts", ...)` and `CreateTable("DataLakeRuns", ...)` calls reflecting the entity configuration from Task 3.

- [ ] **Step 3: Append the partial unique indexes and CHECK constraints via raw SQL**

At the end of `Up()` (after the `CreateTable` calls and the EF-generated index for `(Market, Symbol, Resolution, DataType, TradingDate)`), add:

```csharp
            // Partial unique indexes — one identity scheme per artifact kind.
            // EF Core has no fluent API for partial indexes; declared in raw SQL.
            migrationBuilder.Sql(@"
                CREATE UNIQUE INDEX uq_data_lake_artifacts_minute_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""TradingDate"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" = 'minute';

                CREATE UNIQUE INDEX uq_data_lake_artifacts_aggregated_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""Resolution"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" IN ('hour','daily');

                CREATE UNIQUE INDEX uq_data_lake_artifacts_corp_actions
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""ArtifactKind"",
                                             ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                CREATE UNIQUE INDEX uq_data_lake_artifacts_metadata
                  ON ""DataLakeArtifacts"" (""DataContractHash"")
                  WHERE ""ArtifactKind"" = 'metadata';

                CREATE INDEX ix_data_lake_artifacts_corp_action_lookup
                  ON ""DataLakeArtifacts"" (""Symbol"", ""ArtifactKind"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                CREATE INDEX ix_data_lake_artifacts_incomplete
                  ON ""DataLakeArtifacts"" (""Status"", ""LeaseExpiresAtMs"")
                  WHERE ""Status"" <> 'complete';
            ");

            // CHECK constraints — declared in raw SQL because EF's
            // ToTable(t => t.HasCheckConstraint(...)) is unwieldy for
            // multi-clause invariants like ours.
            migrationBuilder.Sql(@"
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_artifact_kind_fields CHECK (
                    (""ArtifactKind"" = 'time_series_bars'
                       AND ""Market"" IS NOT NULL AND ""Symbol"" IS NOT NULL
                       AND ""Resolution"" IS NOT NULL AND ""DataType"" IS NOT NULL
                       AND ""PriceAdjustmentMode"" IS NOT NULL
                       AND ((""Resolution"" = 'minute' AND ""TradingDate"" IS NOT NULL)
                            OR (""Resolution"" IN ('hour','daily') AND ""TradingDate"" IS NULL)))
                    OR (""ArtifactKind"" IN ('factor_file','map_file')
                       AND ""Market"" IS NOT NULL AND ""Symbol"" IS NOT NULL
                       AND ""PriceAdjustmentMode"" IS NOT NULL
                       AND ""TradingDate"" IS NULL AND ""Resolution"" IS NULL
                       AND ""DataType"" IS NULL)
                    OR (""ArtifactKind"" = 'metadata'
                       AND ""TradingDate"" IS NULL AND ""Resolution"" IS NULL
                       AND ""DataType"" IS NULL)
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_artifact_kind_enum CHECK (
                    ""ArtifactKind"" IN ('time_series_bars','factor_file','map_file','metadata')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_resolution_enum CHECK (
                    ""Resolution"" IS NULL OR ""Resolution"" IN ('minute','hour','daily')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_data_type_enum CHECK (
                    ""DataType"" IS NULL OR ""DataType"" IN ('trade','quote')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_price_adjustment_mode_enum CHECK (
                    ""PriceAdjustmentMode"" IS NULL
                    OR ""PriceAdjustmentMode"" IN ('raw','polygon_split_adjusted','lean_adjusted')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_status_enum CHECK (
                    ""Status"" IN ('fetching','complete','stale','failed')
                );

                -- v1 only: single canonical root → raw bars only.
                -- Relaxed in v2 by adding data_root_id and dropping this constraint.
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata' OR ""PriceAdjustmentMode"" = 'raw'
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_run_type CHECK (
                    ""RunType"" IN ('python_lab','lean_lab')
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_ensure_data_status CHECK (
                    ""EnsureDataStatus"" IS NULL
                    OR ""EnsureDataStatus"" IN ('pending','complete','partial','failed')
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_engine_status CHECK (
                    ""EngineStatus"" IS NULL
                    OR ""EngineStatus"" IN ('not_started','running','complete','failed')
                );
            ");
```

- [ ] **Step 4: Add the corresponding `Down()` to drop everything cleanly**

```csharp
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS uq_data_lake_artifacts_minute_bars;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_aggregated_bars;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_corp_actions;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_metadata;
                DROP INDEX IF EXISTS ix_data_lake_artifacts_corp_action_lookup;
                DROP INDEX IF EXISTS ix_data_lake_artifacts_incomplete;
            ");
            migrationBuilder.DropTable(name: "DataLakeRuns");
            migrationBuilder.DropTable(name: "DataLakeArtifacts");
        }
```

(The CHECK constraints and indexes on the dropped tables disappear with `DROP TABLE`. The standalone partial indexes named above are not on dropped tables — wait, they are; double-check the ordering. Postgres will drop indexes and constraints when their table drops. The explicit `DROP INDEX IF EXISTS` lines above are belt-and-suspenders.)

- [ ] **Step 5: Apply the migration locally**

Run:
```bash
podman exec my-backend dotnet ef database update --project /app
```
Expected: `Applying migration '<timestamp>_AddDataLakeArtifactsAndRuns'. Done.`

- [ ] **Step 6: Verify the tables exist with the expected indexes**

Run:
```bash
podman exec -it my-postgres psql -U postgres -d postgres -c "\d \"DataLakeArtifacts\""
podman exec -it my-postgres psql -U postgres -d postgres -c "\d \"DataLakeRuns\""
```
Expected: both tables print with the columns and partial indexes from Step 3.

- [ ] **Step 7: Run Backend test suite**

Run: `cd Backend.Tests && dotnet test`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add Backend/Migrations/
git commit -m "feat(data-lake): EF migration for data_lake_artifacts and data_lake_runs"
```

---

### Task 5: Python package skeleton + `__init__.py`

**Files:**
- Create: `PythonDataService/app/data_lake/__init__.py`
- Create: `PythonDataService/tests/unit/data_lake/__init__.py`
- Create: `PythonDataService/tests/integration/data_lake/__init__.py`

- [ ] **Step 1: Create the empty package markers**

```python
# PythonDataService/app/data_lake/__init__.py
"""Polygon → LEAN data-lake module.

Authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
This package is the ONLY writer to LEAN_DATA_WRITE_ROOT. No other module
in the Python data service may produce files under that root.
"""
```

```python
# PythonDataService/tests/unit/data_lake/__init__.py
```

```python
# PythonDataService/tests/integration/data_lake/__init__.py
```

- [ ] **Step 2: Verify the package imports without errors**

Run: `podman exec polygon-data-service python -c "import app.data_lake; print(app.data_lake.__doc__[:50])"`
Expected: prints the start of the module docstring; no ImportError.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/app/data_lake/__init__.py \
        PythonDataService/tests/unit/data_lake/__init__.py \
        PythonDataService/tests/integration/data_lake/__init__.py
git commit -m "feat(data-lake): scaffold data_lake package"
```

---

### Task 6: `path_policy.py` — typed LEAN paths

**Files:**
- Create: `PythonDataService/app/data_lake/path_policy.py`
- Create: `PythonDataService/tests/unit/data_lake/test_path_policy.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_path_policy.py
"""Unit tests for app.data_lake.path_policy.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
from uuid import UUID

import pytest

from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
    staging_path_for,
)


class TestLeanMinuteBarPath:
    def test_relative_path_for_spy_trade(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            data_type="trade",
        ).relative_path()
        assert path == PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

    def test_relative_path_for_spy_quote(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="SPY",
            trading_date=date(2024, 5, 20),
            data_type="quote",
        ).relative_path()
        assert path == PurePosixPath("equity/usa/minute/spy/20240520_quote.zip")

    def test_symbol_lowercased_in_path(self):
        path = LeanMinuteBarPath(
            market="usa",
            symbol="QQQ",
            trading_date=date(2024, 1, 2),
            data_type="trade",
        ).relative_path()
        # Symbol portion of the path is lowercased per LEAN convention.
        assert "qqq" in str(path)
        assert "QQQ" not in str(path)


class TestLeanDailyBarPath:
    def test_relative_path_for_spy(self):
        path = LeanDailyBarPath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/daily/spy.zip")


class TestLeanFactorFilePath:
    def test_relative_path_for_spy(self):
        path = LeanFactorFilePath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/factor_files/spy.csv")


class TestLeanMapFilePath:
    def test_relative_path_for_spy(self):
        path = LeanMapFilePath(market="usa", symbol="SPY").relative_path()
        assert path == PurePosixPath("equity/usa/map_files/spy.csv")


class TestLeanMetadataPath:
    def test_market_hours(self):
        path = LeanMetadataPath(kind="market_hours").relative_path()
        assert path == PurePosixPath("market-hours/market-hours-database.json")

    def test_symbol_properties(self):
        path = LeanMetadataPath(kind="symbol_properties").relative_path()
        assert path == PurePosixPath("symbol-properties/symbol-properties-database.csv")


class TestStagingPathFor:
    def test_staging_path_isolation(self):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        worker_id = "worker-7"
        attempt = 2
        staged = staging_path_for(rel, request_id, worker_id, attempt)
        assert staged == PurePosixPath(
            "staging/12345678-1234-5678-1234-567812345678/worker-7/attempt_2/"
            "equity/usa/minute/spy/20240520_trade.zip.tmp"
        )

    def test_two_attempts_produce_distinct_paths(self):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        a1 = staging_path_for(rel, request_id, "worker-1", 1)
        a2 = staging_path_for(rel, request_id, "worker-1", 2)
        assert a1 != a2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_path_policy.py -v`
Expected: FAIL with ModuleNotFoundError (the module doesn't exist yet).

- [ ] **Step 3: Implement `path_policy.py`**

```python
# PythonDataService/app/data_lake/path_policy.py
"""Typed LEAN-path policy.

Sole authority for constructing LEAN on-disk paths. No string concatenation
of LEAN paths is permitted anywhere else in the codebase; a lint test enforces
that the substrings ``equity/usa/``, ``market-hours/``, ``symbol-properties/``
appear only in this module and its tests.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

Market = Literal["usa"]
Resolution = Literal["minute", "hour", "daily"]
DataType = Literal["trade", "quote"]
MetadataKind = Literal["market_hours", "symbol_properties"]


@dataclass(frozen=True)
class LeanMinuteBarPath:
    market: Market
    symbol: str
    trading_date: date
    data_type: DataType

    def relative_path(self) -> PurePosixPath:
        return (
            PurePosixPath("equity") / self.market / "minute" / self.symbol.lower()
            / f"{self.trading_date.strftime('%Y%m%d')}_{self.data_type}.zip"
        )


@dataclass(frozen=True)
class LeanDailyBarPath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return PurePosixPath("equity") / self.market / "daily" / f"{self.symbol.lower()}.zip"


@dataclass(frozen=True)
class LeanFactorFilePath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return (
            PurePosixPath("equity") / self.market / "factor_files"
            / f"{self.symbol.lower()}.csv"
        )


@dataclass(frozen=True)
class LeanMapFilePath:
    market: Market
    symbol: str

    def relative_path(self) -> PurePosixPath:
        return (
            PurePosixPath("equity") / self.market / "map_files"
            / f"{self.symbol.lower()}.csv"
        )


@dataclass(frozen=True)
class LeanMetadataPath:
    kind: MetadataKind

    def relative_path(self) -> PurePosixPath:
        if self.kind == "market_hours":
            return PurePosixPath("market-hours") / "market-hours-database.json"
        if self.kind == "symbol_properties":
            return PurePosixPath("symbol-properties") / "symbol-properties-database.csv"
        raise ValueError(f"unknown metadata kind: {self.kind!r}")


def staging_path_for(
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> PurePosixPath:
    """Build the per-attempt staging path for a given final relative path.

    Structurally prevents retry/parallel-worker collisions: every attempt
    writes to its own subtree under staging/. The atomic rename promotes
    the .tmp file to its final position in the lake.
    """
    return (
        PurePosixPath("staging") / str(request_id) / worker_id
        / f"attempt_{attempt}"
        / rel_lake_path.with_suffix(rel_lake_path.suffix + ".tmp")
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_path_policy.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/path_policy.py \
        PythonDataService/tests/unit/data_lake/test_path_policy.py
git commit -m "feat(data-lake): add typed path_policy module"
```

---

### Task 7: Lint test — ban raw LEAN path strings outside `path_policy.py`

**Files:**
- Create: `PythonDataService/tests/unit/data_lake/test_no_lean_paths_outside_policy.py`

- [ ] **Step 1: Write the test**

```python
# PythonDataService/tests/unit/data_lake/test_no_lean_paths_outside_policy.py
"""Lint test: forbid raw LEAN-path substrings outside path_policy.

LEAN paths must only be constructed by app/data_lake/path_policy.py. Any other
module containing these substrings is a violation — the path should flow
through the typed dataclasses.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.3
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = PROJECT_ROOT / "app"

# Substrings that uniquely identify LEAN on-disk paths.
FORBIDDEN_SUBSTRINGS = (
    "equity/usa/",
    "market-hours/",
    "symbol-properties/",
)

# Files in which the substrings ARE permitted.
ALLOWLISTED = (
    "app/data_lake/path_policy.py",
    "app/lean_sidecar/",     # existing pre-data-lake staging code; retired in Slice 1d
    "app/engine/data/lean_format.py",  # existing reader; replaced in Slice 2
)


def _is_allowlisted(rel_path: Path) -> bool:
    rel_str = str(rel_path).replace("\\", "/")
    return any(rel_str.startswith(prefix) for prefix in ALLOWLISTED)


def test_lean_paths_only_in_path_policy():
    violations: list[tuple[str, int, str]] = []
    for py_file in APP_DIR.rglob("*.py"):
        rel = py_file.relative_to(PROJECT_ROOT)
        if _is_allowlisted(rel):
            continue
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle in line and not re.search(r"^\s*#", line):
                    violations.append((str(rel), lineno, line.strip()))
    assert not violations, (
        "Found LEAN-path string outside path_policy.py:\n"
        + "\n".join(f"  {f}:{ln}: {snippet}" for f, ln, snippet in violations)
    )
```

- [ ] **Step 2: Run the test**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_no_lean_paths_outside_policy.py -v`
Expected: PASS (allowlist accommodates the existing pre-data-lake call sites).

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/tests/unit/data_lake/test_no_lean_paths_outside_policy.py
git commit -m "test(data-lake): lint rule banning LEAN-path strings outside path_policy"
```

---

### Task 8: Pydantic models in `types.py`

**Files:**
- Create: `PythonDataService/app/data_lake/types.py`
- Create: `PythonDataService/tests/unit/data_lake/test_types.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_types.py
"""Validation tests for app.data_lake.types Pydantic models."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.data_lake.types import DataRunSpec


class TestDataRunSpec:
    def _valid_payload(self) -> dict:
        return {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "run_type": "python_lab",
            "symbols": ["SPY"],
            "start_trading_date": "2024-05-20",
            "end_trading_date": "2024-05-24",
        }

    def test_minimal_valid_spec(self):
        spec = DataRunSpec(**self._valid_payload())
        assert spec.market == "usa"
        assert spec.symbols == ["SPY"]
        assert spec.resolution == "minute"
        assert spec.data_types == ["trade"]
        assert spec.price_adjustment_mode == "raw"
        assert spec.provider == "polygon"
        assert spec.include_factor_files is True
        assert spec.fetch_timeout_seconds == 600

    def test_lowercase_symbol_is_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = ["spy"]
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_start_after_end_is_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date"] = "2024-05-24"
        payload["end_trading_date"] = "2024-05-20"
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_empty_symbols_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = []
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_lean_metadata_requires_image_digest(self):
        payload = self._valid_payload()
        payload["include_lean_metadata"] = True
        payload["lean_image_digest"] = None
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_5_year_range_cap(self):
        payload = self._valid_payload()
        payload["start_trading_date"] = "2018-01-01"
        payload["end_trading_date"] = "2024-12-31"  # ~7 years
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)
```

- [ ] **Step 2: Run the failing tests**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_types.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `types.py`**

```python
# PythonDataService/app/data_lake/types.py
"""Pydantic models for the ensure_data contract.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
_MAX_RANGE_YEARS = 5


class DataRunSpec(BaseModel):
    request_id: UUID
    run_type: Literal["python_lab", "lean_lab"]
    requester: str | None = None
    strategy_execution_id: int | None = None

    market: Literal["usa"] = "usa"
    symbols: list[str] = Field(min_length=1)
    start_trading_date: date
    end_trading_date: date

    resolution: Literal["minute"] = "minute"
    data_types: list[Literal["trade", "quote"]] = ["trade"]
    price_adjustment_mode: Literal["raw"] = "raw"
    provider: Literal["polygon"] = "polygon"

    include_factor_files: bool = True
    include_map_files: bool = True
    include_lean_metadata: bool = True
    lean_image_digest: str | None = None

    force_refresh: bool = False
    fetch_timeout_seconds: int = Field(default=600, ge=10, le=7200)

    @model_validator(mode="after")
    def _validate(self) -> "DataRunSpec":
        # Symbols: uppercase canonical.
        for sym in self.symbols:
            if not _SYMBOL_RE.match(sym):
                raise ValueError(f"symbol must match {_SYMBOL_RE.pattern}: {sym!r}")
        # Date ordering.
        if self.start_trading_date > self.end_trading_date:
            raise ValueError(
                f"start_trading_date {self.start_trading_date} > "
                f"end_trading_date {self.end_trading_date}"
            )
        # Range cap.
        delta_days = (self.end_trading_date - self.start_trading_date).days
        if delta_days > _MAX_RANGE_YEARS * 366:
            raise ValueError(
                f"range exceeds {_MAX_RANGE_YEARS}-year cap "
                f"({delta_days} days requested)"
            )
        # LEAN metadata requires an image digest.
        if self.include_lean_metadata and not self.lean_image_digest:
            raise ValueError("lean_image_digest is required when include_lean_metadata=True")
        return self


class ArtifactIdentity(BaseModel):
    """Internal identity tuple — what the catalog claim key looks like."""

    artifact_kind: Literal["time_series_bars", "factor_file", "map_file", "metadata"]
    market: str | None = None
    symbol: str | None = None
    trading_date: date | None = None
    resolution: Literal["minute", "hour", "daily"] | None = None
    data_type: Literal["trade", "quote"] | None = None
    provider: str
    price_adjustment_mode: str | None = None


class ArtifactRecord(BaseModel):
    id: int
    artifact_kind: str
    market: str | None
    symbol: str | None
    trading_date: date | None
    resolution: str | None
    data_type: str | None
    provider: str
    price_adjustment_mode: str | None
    data_contract_hash: str
    file_path: str
    file_sha256: str
    row_count: int | None
    first_bar_start_ms: int | None
    last_bar_start_ms: int | None


class ArtifactFailure(BaseModel):
    artifact_kind: str
    symbol: str | None
    trading_date: date | None
    data_type: str | None
    reason: Literal[
        "provider_auth_error",
        "provider_entitlement_error",
        "provider_rate_limited",
        "provider_api_error",
        "provider_no_data",
        "unknown_symbol",
        "validation_failed",
        "io_error",
        "lease_timeout",
        "fetch_timeout",
        "unsupported_resolution",
        "internal_error",
    ]
    detail: str | None = None
    provider_status_code: int | None = None
    attempt_count: int = 0


class NonSessionRecord(BaseModel):
    market: str
    trading_date: date
    reason: Literal["weekend", "market_holiday"]


class DataAvailabilityResult(BaseModel):
    request_id: UUID
    overall_status: Literal["complete", "partial", "failed"]
    lean_data_root_path: str
    data_availability_hash: str
    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    skipped_non_sessions: list[NonSessionRecord] = []
    fetched_artifact_count: int = 0
    reused_artifact_count: int = 0
    refreshed_artifact_count: int = 0
    completed_at_ms: int
    duration_ms: int
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_types.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/types.py \
        PythonDataService/tests/unit/data_lake/test_types.py
git commit -m "feat(data-lake): add Pydantic types for ensure_data contract"
```

---

### Task 9: Python `catalog_schema.py` — typed expectation

**Files:**
- Create: `PythonDataService/app/data_lake/catalog_schema.py`

- [ ] **Step 1: Implement the typed expectation**

```python
# PythonDataService/app/data_lake/catalog_schema.py
"""Typed declaration of the Postgres schema Python expects.

The drift integration test (tests/integration/data_lake/test_schema_drift.py)
introspects the live database via pg_catalog and asserts equality against
the expectations below. If the EF Core migration changes a column or
constraint, this file must be updated in the same PR or CI will fail.

Authority for the schema itself: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnExpectation:
    name: str
    pg_type: str           # the canonical type string returned by pg_catalog (e.g. 'text', 'bigint', 'jsonb', 'date', 'integer', 'boolean')
    nullable: bool


@dataclass(frozen=True)
class TableExpectation:
    name: str
    columns: tuple[ColumnExpectation, ...]
    primary_key: tuple[str, ...]
    # Partial unique indexes and CHECK constraint names (full SQL not asserted; only names).
    partial_unique_indexes: tuple[str, ...] = ()
    check_constraints: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()


DATA_LAKE_ARTIFACTS = TableExpectation(
    name="DataLakeArtifacts",
    columns=(
        ColumnExpectation("Id", "bigint", nullable=False),
        ColumnExpectation("ArtifactKind", "character varying", nullable=False),
        ColumnExpectation("Market", "character varying", nullable=True),
        ColumnExpectation("Symbol", "character varying", nullable=True),
        ColumnExpectation("TradingDate", "date", nullable=True),
        ColumnExpectation("Resolution", "character varying", nullable=True),
        ColumnExpectation("DataType", "character varying", nullable=True),
        ColumnExpectation("Provider", "character varying", nullable=False),
        ColumnExpectation("ProviderParams", "jsonb", nullable=False),
        ColumnExpectation("PriceAdjustmentMode", "character varying", nullable=True),
        ColumnExpectation("DataContractHash", "character", nullable=False),
        ColumnExpectation("RowCount", "integer", nullable=True),
        ColumnExpectation("FirstBarStartMs", "bigint", nullable=True),
        ColumnExpectation("LastBarStartMs", "bigint", nullable=True),
        ColumnExpectation("CorpActionRevision", "character", nullable=True),
        ColumnExpectation("FilePath", "text", nullable=False),
        ColumnExpectation("FileSizeBytes", "bigint", nullable=True),
        ColumnExpectation("FileSha256", "character", nullable=True),
        ColumnExpectation("Status", "character varying", nullable=False),
        ColumnExpectation("LeaseOwner", "character varying", nullable=True),
        ColumnExpectation("LeaseExpiresAtMs", "bigint", nullable=True),
        ColumnExpectation("AttemptCount", "integer", nullable=False),
        ColumnExpectation("LastError", "text", nullable=True),
        ColumnExpectation("ErrorMessage", "text", nullable=True),
        ColumnExpectation("FetchedAtMs", "bigint", nullable=False),
        ColumnExpectation("CompletedAtMs", "bigint", nullable=True),
    ),
    primary_key=("Id",),
    partial_unique_indexes=(
        "uq_data_lake_artifacts_minute_bars",
        "uq_data_lake_artifacts_aggregated_bars",
        "uq_data_lake_artifacts_corp_actions",
        "uq_data_lake_artifacts_metadata",
    ),
    check_constraints=(
        "ck_artifact_kind_fields",
        "ck_artifact_kind_enum",
        "ck_resolution_enum",
        "ck_data_type_enum",
        "ck_price_adjustment_mode_enum",
        "ck_status_enum",
        "ck_raw_only_for_canonical_data_root",
    ),
    indexes=(
        "ix_data_lake_artifacts_corp_action_lookup",
        "ix_data_lake_artifacts_incomplete",
    ),
)


DATA_LAKE_RUNS = TableExpectation(
    name="DataLakeRuns",
    columns=(
        ColumnExpectation("Id", "uuid", nullable=False),
        ColumnExpectation("StrategyExecutionId", "integer", nullable=True),
        ColumnExpectation("EngineRunId", "character varying", nullable=True),
        ColumnExpectation("RunType", "character varying", nullable=False),
        ColumnExpectation("RunSpec", "jsonb", nullable=False),
        ColumnExpectation("WorkspacePath", "text", nullable=True),
        ColumnExpectation("ManifestSha256", "character", nullable=True),
        ColumnExpectation("DataAvailabilityHash", "character", nullable=True),
        ColumnExpectation("EnsureDataStatus", "character varying", nullable=True),
        ColumnExpectation("EnsureDataResponse", "jsonb", nullable=True),
        ColumnExpectation("EngineStatus", "character varying", nullable=True),
        ColumnExpectation("RequestedAtMs", "bigint", nullable=False),
        ColumnExpectation("StartedAtMs", "bigint", nullable=True),
        ColumnExpectation("CompletedAtMs", "bigint", nullable=True),
    ),
    primary_key=("Id",),
    check_constraints=(
        "ck_data_lake_runs_run_type",
        "ck_data_lake_runs_ensure_data_status",
        "ck_data_lake_runs_engine_status",
    ),
)


ALL_TABLES = (DATA_LAKE_ARTIFACTS, DATA_LAKE_RUNS)
```

- [ ] **Step 2: Verify the module imports**

Run: `podman exec polygon-data-service python -c "from app.data_lake.catalog_schema import ALL_TABLES; print(len(ALL_TABLES))"`
Expected: prints `2`.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_schema.py
git commit -m "feat(data-lake): typed Python expectation of the Postgres schema"
```

---

### Task 10: Add `asyncpg` and Postgres config

**Files:**
- Modify: `PythonDataService/requirements-light.txt` (add asyncpg)
- Modify: `PythonDataService/app/config.py` (add `POSTGRES_URL` + `DATA_LAKE_ENABLED`)

- [ ] **Step 1: Add `asyncpg` to requirements-light.txt**

Append:
```
asyncpg==0.29.0
```

- [ ] **Step 2: Install in the running container**

Run: `podman exec polygon-data-service pip install asyncpg==0.29.0`
Expected: install succeeds.

- [ ] **Step 3: Add `POSTGRES_URL` and `DATA_LAKE_ENABLED` to Settings**

In `PythonDataService/app/config.py`, before the closing `settings = Settings()` line, add inside the `Settings` class:

```python
    # Data lake (Slice 1a)
    # postgres://user:pass@host:5432/dbname — required when DATA_LAKE_ENABLED is true
    POSTGRES_URL: str = ""
    DATA_LAKE_ENABLED: bool = False
```

- [ ] **Step 4: Smoke-test import**

Run: `podman exec polygon-data-service python -c "from app.config import settings; print(settings.DATA_LAKE_ENABLED)"`
Expected: prints `False`.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/requirements-light.txt PythonDataService/app/config.py
git commit -m "feat(data-lake): add asyncpg dep and POSTGRES_URL/DATA_LAKE_ENABLED settings"
```

---

### Task 11: `catalog_client.py` — asyncpg connection + coverage query

**Files:**
- Create: `PythonDataService/app/data_lake/catalog_client.py`

- [ ] **Step 1: Implement the client (no test yet — exercised by Task 13's drift test and Task 17's integration test)**

```python
# PythonDataService/app/data_lake/catalog_client.py
"""Postgres catalog client — asyncpg with parameterized SQL.

Schema-write path: Slice 1b. This module in Slice 1a is read-only:
just a connection pool and a coverage SELECT.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.4
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date

import asyncpg

from app.config import settings
from app.data_lake.types import ArtifactRecord

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the global asyncpg pool. Idempotent."""
    global _pool
    if _pool is not None:
        return
    if not settings.POSTGRES_URL:
        raise RuntimeError(
            "POSTGRES_URL is empty; cannot initialize catalog_client. "
            "Set the env var or disable the data lake (DATA_LAKE_ENABLED=False)."
        )
    _pool = await asyncpg.create_pool(
        settings.POSTGRES_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    logger.info("data_lake.catalog_client: asyncpg pool initialized")


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("data_lake.catalog_client: asyncpg pool closed")


@asynccontextmanager
async def connection():
    if _pool is None:
        raise RuntimeError("asyncpg pool not initialized; call init_pool() first")
    async with _pool.acquire() as conn:
        yield conn


async def select_coverage_minute_bars(
    market: str,
    symbol: str,
    data_type: str,
    start_trading_date: date,
    end_trading_date: date,
) -> list[ArtifactRecord]:
    """Return all complete minute-bar artifacts for the given window.

    Used by ensure_data to compute which dates already exist on disk before
    deciding what to fetch. In Slice 1a there are no rows; this returns an
    empty list and exercises the schema/query end-to-end.
    """
    query = """
        SELECT "Id", "ArtifactKind", "Market", "Symbol", "TradingDate",
               "Resolution", "DataType", "Provider", "PriceAdjustmentMode",
               "DataContractHash", "FilePath",
               COALESCE("FileSha256", '') AS file_sha256,
               "RowCount", "FirstBarStartMs", "LastBarStartMs"
          FROM "DataLakeArtifacts"
         WHERE "ArtifactKind" = 'time_series_bars'
           AND "Resolution" = 'minute'
           AND "Market" = $1
           AND "Symbol" = $2
           AND "DataType" = $3
           AND "TradingDate" BETWEEN $4 AND $5
           AND "Status" = 'complete'
         ORDER BY "TradingDate"
    """
    async with connection() as conn:
        rows = await conn.fetch(query, market, symbol, data_type, start_trading_date, end_trading_date)
    return [
        ArtifactRecord(
            id=r["Id"],
            artifact_kind=r["ArtifactKind"],
            market=r["Market"],
            symbol=r["Symbol"],
            trading_date=r["TradingDate"],
            resolution=r["Resolution"],
            data_type=r["DataType"],
            provider=r["Provider"],
            price_adjustment_mode=r["PriceAdjustmentMode"],
            data_contract_hash=r["DataContractHash"],
            file_path=r["FilePath"],
            file_sha256=r["file_sha256"],
            row_count=r["RowCount"],
            first_bar_start_ms=r["FirstBarStartMs"],
            last_bar_start_ms=r["LastBarStartMs"],
        )
        for r in rows
    ]
```

- [ ] **Step 2: Smoke-test the module imports**

Run: `podman exec polygon-data-service python -c "from app.data_lake import catalog_client; print(catalog_client.select_coverage_minute_bars.__doc__[:30])"`
Expected: prints the start of the docstring.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_client.py
git commit -m "feat(data-lake): asyncpg catalog_client with select_coverage_minute_bars"
```

---

### Task 12: Schema-drift integration test

**Files:**
- Create: `PythonDataService/tests/integration/data_lake/test_schema_drift.py`

- [ ] **Step 1: Write the test**

```python
# PythonDataService/tests/integration/data_lake/test_schema_drift.py
"""Verify the live Postgres schema matches catalog_schema.py.

Authoritative source of the schema: EF Core migrations in Backend/Migrations.
This test asserts that the Python typed expectation is in sync with what
EF actually applied. A failure here means either:
  - the EF migration changed but catalog_schema.py wasn't updated, OR
  - catalog_schema.py was edited without a matching EF migration.

Either way, the PR should not merge until they agree.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from app.config import settings
from app.data_lake.catalog_schema import ALL_TABLES, TableExpectation

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured; skipping live-DB drift test")
    return url


async def _fetch_columns(conn: asyncpg.Connection, table_name: str) -> dict[str, tuple[str, bool]]:
    """Returns {column_name: (data_type, is_nullable)}."""
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1
        """,
        table_name,
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"] == "YES") for r in rows}


async def _fetch_index_names(conn: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = $1
        """,
        table_name,
    )
    return {r["indexname"] for r in rows}


async def _fetch_check_constraint_names(conn: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT conname FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.contype = 'c' AND t.relname = $1
        """,
        table_name,
    )
    return {r["conname"] for r in rows}


@pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
async def test_schema_matches_expectation(table: TableExpectation):
    conn = await asyncpg.connect(_postgres_url())
    try:
        live_columns = await _fetch_columns(conn, table.name)
        live_indexes = await _fetch_index_names(conn, table.name)
        live_checks = await _fetch_check_constraint_names(conn, table.name)
    finally:
        await conn.close()

    # Every expected column exists with the expected nullability.
    missing: list[str] = []
    mismatched: list[str] = []
    for col in table.columns:
        if col.name not in live_columns:
            missing.append(col.name)
            continue
        live_type, live_nullable = live_columns[col.name]
        if live_nullable != col.nullable:
            mismatched.append(
                f"{col.name}: expected nullable={col.nullable}, got {live_nullable}"
            )
        # Type comparison: accept Postgres's information_schema canonical names.
        # Some types have aliases (e.g. EF Core 'character varying(40)' is
        # data_type='character varying'); accept the family-level match.
        if col.pg_type not in live_type:
            mismatched.append(
                f"{col.name}: expected pg_type={col.pg_type!r}, got {live_type!r}"
            )

    assert not missing, f"{table.name}: columns missing from live DB: {missing}"
    assert not mismatched, f"{table.name}: column mismatches: {mismatched}"

    # Every expected partial-unique index, CHECK constraint, and named index exists.
    for ix in (*table.partial_unique_indexes, *table.indexes):
        assert ix in live_indexes, f"{table.name}: missing index {ix!r}"

    for ck in table.check_constraints:
        assert ck in live_checks, f"{table.name}: missing CHECK constraint {ck!r}"
```

- [ ] **Step 2: Ensure Postgres is reachable from the container**

Run: `podman exec polygon-data-service env | grep POSTGRES_URL`
If empty, set it in the container's environment (compose.yaml's python-service.environment section), then restart the container:
```bash
podman compose up -d python-service
```

For local-only development, also export it in your shell:
```bash
export POSTGRES_URL=postgres://postgres:postgres@localhost:5432/postgres
```

- [ ] **Step 3: Run the test**

Run: `podman exec polygon-data-service python -m pytest tests/integration/data_lake/test_schema_drift.py -v`
Expected: 2 tests PASS (one per table).

- [ ] **Step 4: Commit**

```bash
git add PythonDataService/tests/integration/data_lake/test_schema_drift.py
git commit -m "test(data-lake): schema-drift integration test against live Postgres"
```

---

### Task 13: `sessions.py` — fixture-backed `trading_sessions_for`

**Files:**
- Create: `PythonDataService/app/data_lake/sessions.py`
- Create: `PythonDataService/tests/unit/data_lake/test_sessions.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_sessions.py
"""Tests for the fixture-backed trading_sessions_for in Slice 1a.

Slice 1c replaces this with a LEAN market-hours-database-driven implementation;
the public function signature is stable.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import NonSessionRecord


def test_weekday_non_holiday_is_a_session():
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 20), date(2024, 5, 20))
    assert sessions == [date(2024, 5, 20)]  # Mon
    assert non_sessions == []


def test_weekend_is_excluded():
    # 2024-05-25 is a Saturday, 2024-05-26 is a Sunday.
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 25), date(2024, 5, 26))
    assert sessions == []
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 25), reason="weekend") in non_sessions
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 26), reason="weekend") in non_sessions


def test_memorial_day_2024_is_a_market_holiday():
    # 2024-05-27 is Memorial Day; market is closed.
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 27), date(2024, 5, 27))
    assert sessions == []
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 27), reason="market_holiday") in non_sessions


def test_week_spanning_a_holiday():
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 24), date(2024, 5, 31))
    # Fri 5/24 trading, Sat 5/25 weekend, Sun 5/26 weekend,
    # Mon 5/27 Memorial Day, Tue 5/28 trading, ..., Fri 5/31 trading.
    expected_sessions = [
        date(2024, 5, 24),
        date(2024, 5, 28),
        date(2024, 5, 29),
        date(2024, 5, 30),
        date(2024, 5, 31),
    ]
    assert sessions == expected_sessions
    holiday_dates = [n.trading_date for n in non_sessions if n.reason == "market_holiday"]
    assert date(2024, 5, 27) in holiday_dates
```

- [ ] **Step 2: Run the failing tests**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_sessions.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `sessions.py`**

```python
# PythonDataService/app/data_lake/sessions.py
"""Trading-session calendar.

Slice 1a uses a hardcoded US-equity holiday list good enough for the EMA-crossover
smoke test window. Slice 1c replaces this with a parser of the staged LEAN
market-hours-database.json, with the same public signature.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

from datetime import date, timedelta

from app.data_lake.types import NonSessionRecord

# Hardcoded US-equity full-day market holidays for the Slice 1a smoke window.
# Source: NYSE official calendar. Slice 1c replaces this with the LEAN
# market-hours-database to get unlimited range + early-close metadata.
_USA_FULL_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2024
        date(2024, 1, 1),   # New Year's Day
        date(2024, 1, 15),  # MLK Day
        date(2024, 2, 19),  # Presidents Day
        date(2024, 3, 29),  # Good Friday
        date(2024, 5, 27),  # Memorial Day
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),   # Independence Day
        date(2024, 9, 2),   # Labor Day
        date(2024, 11, 28), # Thanksgiving
        date(2024, 12, 25), # Christmas
        # 2025
        date(2025, 1, 1),
        date(2025, 1, 20),  # MLK Day
        date(2025, 2, 17),  # Presidents Day
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 26),  # Memorial Day
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),   # observed
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)


def trading_sessions_for(
    market: str,
    start_trading_date: date,
    end_trading_date: date,
) -> tuple[list[date], list[NonSessionRecord]]:
    """Return (sessions, non_sessions) for the inclusive window.

    Half-day early closes ARE sessions in v1 (full-minute coverage for the
    truncated window); only full closures map to non-sessions.
    """
    if market != "usa":
        raise ValueError(f"market {market!r} not supported in Slice 1a")

    sessions: list[date] = []
    non_sessions: list[NonSessionRecord] = []
    current = start_trading_date
    while current <= end_trading_date:
        if current.weekday() >= 5:
            non_sessions.append(NonSessionRecord(market=market, trading_date=current, reason="weekend"))
        elif current in _USA_FULL_HOLIDAYS:
            non_sessions.append(
                NonSessionRecord(market=market, trading_date=current, reason="market_holiday")
            )
        else:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions, non_sessions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_sessions.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/sessions.py \
        PythonDataService/tests/unit/data_lake/test_sessions.py
git commit -m "feat(data-lake): fixture-backed trading_sessions_for for Slice 1a"
```

---

### Task 14: `ensure_data.py` — `expand_required_artifacts`

**Files:**
- Create: `PythonDataService/app/data_lake/ensure_data.py` (partial; just the expansion function)

- [ ] **Step 1: Add a unit test for `expand_required_artifacts`**

Append to `PythonDataService/tests/unit/data_lake/test_sessions.py` — no, this deserves its own file. Create:

```python
# PythonDataService/tests/unit/data_lake/test_expand_required_artifacts.py
"""Tests for ensure_data.expand_required_artifacts (Slice 1a)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.data_lake.ensure_data import expand_required_artifacts
from app.data_lake.types import DataRunSpec


def _base_spec(**overrides) -> DataRunSpec:
    payload = {
        "request_id": UUID("12345678-1234-5678-1234-567812345678"),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": date(2024, 5, 20),
        "end_trading_date": date(2024, 5, 24),
        "include_lean_metadata": True,
        "lean_image_digest": "sha256:test",
    }
    payload.update(overrides)
    return DataRunSpec(**payload)


def test_single_symbol_one_week_trade_only():
    required, non_sessions = expand_required_artifacts(_base_spec())
    # 5 trading days × (1 minute-trade) + 1 factor + 1 map + 1 daily-trade + 2 metadata
    kinds = [a.artifact_kind for a in required]
    assert kinds.count("time_series_bars") == 6  # 5 minute + 1 daily
    assert kinds.count("factor_file") == 1
    assert kinds.count("map_file") == 1
    assert kinds.count("metadata") == 2
    assert non_sessions == []


def test_quote_inclusion_doubles_minute_artifacts():
    required, _ = expand_required_artifacts(_base_spec(data_types=["trade", "quote"]))
    minute_artifacts = [
        a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "minute"
    ]
    assert len(minute_artifacts) == 10  # 5 trade + 5 quote


def test_holiday_week_produces_non_sessions():
    spec = _base_spec(
        start_trading_date=date(2024, 5, 25),  # Sat
        end_trading_date=date(2024, 5, 27),    # Memorial Day Mon
    )
    required, non_sessions = expand_required_artifacts(spec)
    assert [a for a in required if a.artifact_kind == "time_series_bars"] == []
    assert len(non_sessions) == 3


def test_daily_artifact_has_null_trading_date():
    required, _ = expand_required_artifacts(_base_spec())
    daily = [a for a in required if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
    assert len(daily) == 1
    assert daily[0].trading_date is None
    assert daily[0].provider == "learn_ai_derived"


def test_quote_artifacts_use_learn_ai_derived_provider():
    required, _ = expand_required_artifacts(_base_spec(data_types=["trade", "quote"]))
    quote_artifacts = [
        a for a in required if a.artifact_kind == "time_series_bars" and a.data_type == "quote"
    ]
    assert all(a.provider == "learn_ai_derived" for a in quote_artifacts)
    trade_artifacts = [
        a for a in required if a.artifact_kind == "time_series_bars" and a.data_type == "trade" and a.resolution == "minute"
    ]
    assert all(a.provider == "polygon" for a in trade_artifacts)


def test_factor_and_map_excluded_when_disabled():
    required, _ = expand_required_artifacts(
        _base_spec(include_factor_files=False, include_map_files=False)
    )
    kinds = {a.artifact_kind for a in required}
    assert "factor_file" not in kinds
    assert "map_file" not in kinds
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_expand_required_artifacts.py -v`
Expected: FAIL with ImportError on `expand_required_artifacts`.

- [ ] **Step 3: Implement `expand_required_artifacts` in `ensure_data.py`**

```python
# PythonDataService/app/data_lake/ensure_data.py
"""Polygon → LEAN data lake — ensure_data orchestration.

Slice 1a: fixture-backed canned responses; no real Polygon, no catalog INSERT,
no atomic writes. Sufficient to exercise the HTTP boundary, the Pydantic
contract, and the session-expansion logic end-to-end.

Real Polygon fetching + atomic writes + leases land in Slice 1b.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import (
    ArtifactIdentity,
    ArtifactRecord,
    DataAvailabilityResult,
    DataRunSpec,
    NonSessionRecord,
)

logger = logging.getLogger(__name__)


def expand_required_artifacts(
    spec: DataRunSpec,
) -> tuple[list[ArtifactIdentity], list[NonSessionRecord]]:
    """Compute the list of artifacts the spec requires and the calendar gaps it skips.

    Order of the returned list is deterministic so two ensure_data calls with
    the same spec produce the same data_availability_hash.
    """
    sessions, non_sessions = trading_sessions_for(
        spec.market, spec.start_trading_date, spec.end_trading_date
    )
    required: list[ArtifactIdentity] = []

    for symbol in sorted(spec.symbols):
        # Per-day minute bars.
        for trading_date in sessions:
            for data_type in spec.data_types:
                provider = "polygon" if data_type == "trade" else "learn_ai_derived"
                required.append(
                    ArtifactIdentity(
                        artifact_kind="time_series_bars",
                        market=spec.market,
                        symbol=symbol,
                        trading_date=trading_date,
                        resolution="minute",
                        data_type=data_type,
                        provider=provider,
                        price_adjustment_mode="raw",
                    )
                )

        # Corp-action artifacts.
        if spec.include_factor_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="factor_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode="raw",
                )
            )
        if spec.include_map_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="map_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode="raw",
                )
            )

        # Daily-trade derived artifact (per symbol, null trading_date).
        if "trade" in spec.data_types:
            required.append(
                ArtifactIdentity(
                    artifact_kind="time_series_bars",
                    market=spec.market,
                    symbol=symbol,
                    trading_date=None,
                    resolution="daily",
                    data_type="trade",
                    provider="learn_ai_derived",
                    price_adjustment_mode="raw",
                )
            )

    # LEAN metadata.
    if spec.include_lean_metadata:
        for kind in ("market_hours", "symbol_properties"):
            required.append(
                ArtifactIdentity(
                    artifact_kind="metadata",
                    market=None,
                    symbol=None,
                    trading_date=None,
                    resolution=None,
                    data_type=None,
                    provider="lean_image_extract",
                    price_adjustment_mode=None,
                )
            )
    return required, non_sessions


def _compute_data_availability_hash(artifacts: list[ArtifactRecord]) -> str:
    """sha256 over a sorted byte-AND-contract tuple per artifact."""
    fingerprints: list[tuple] = []
    for a in artifacts:
        fingerprints.append((
            a.artifact_kind,
            a.market,
            a.symbol,
            a.trading_date.isoformat() if a.trading_date else None,
            a.data_type,
            a.file_path,
            a.file_sha256,
            a.row_count,
            a.first_bar_start_ms,
            a.last_bar_start_ms,
        ))
    fingerprints.sort(key=lambda t: tuple("" if v is None else str(v) for v in t))
    blob = json.dumps(fingerprints, default=str, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ensure_data() itself is filled in by Task 16 after fake_polygon is in place.
```

- [ ] **Step 4: Run the tests**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_expand_required_artifacts.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/ensure_data.py \
        PythonDataService/tests/unit/data_lake/test_expand_required_artifacts.py
git commit -m "feat(data-lake): expand_required_artifacts + availability-hash helper"
```

---

### Task 15: `fake_polygon.py` — fixture-backed provider

**Files:**
- Create: `PythonDataService/app/data_lake/fake_polygon.py`
- Create: `PythonDataService/tests/fixtures/data_lake_skeleton/canned_response.json`

- [ ] **Step 1: Create the canned fixture**

```json
// PythonDataService/tests/fixtures/data_lake_skeleton/canned_response.json
{
  "comment": "Skeleton fixture for Slice 1a. Real Polygon responses + bar payloads land in Slice 1b.",
  "known_symbols": ["SPY"],
  "per_artifact_template": {
    "id": 0,
    "file_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "row_count": 390,
    "first_bar_start_ms": 0,
    "last_bar_start_ms": 0
  }
}
```

- [ ] **Step 2: Implement the fake provider**

```python
# PythonDataService/app/data_lake/fake_polygon.py
"""Slice 1a fixture-backed Polygon stub.

ensure_data calls this instead of the real Polygon fetcher in Slice 1a so the
HTTP boundary and Pydantic round-trip can be tested without a Polygon API key
or fixture management overhead. Real fetcher lands in Slice 1b.

ABSOLUTELY NOT used in production. Guarded by DATA_LAKE_ENABLED at the route
layer; even with the flag on, this stub returns synthetic data with
file_sha256='0'*64 — clearly non-real bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
)
from app.data_lake.types import ArtifactIdentity, ArtifactRecord

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests/fixtures/data_lake_skeleton/canned_response.json"
_ZERO_SHA = "0" * 64
_ZERO_HASH = "0" * 64


def known_symbols() -> set[str]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as f:
        return set(json.load(f)["known_symbols"])


def synth_artifact_record(identity: ArtifactIdentity) -> ArtifactRecord:
    """Build a synthetic ArtifactRecord that matches the identity tuple.

    The file_path is computed from path_policy; sha256/row_count are stubbed.
    This is enough for the Slice 1a smoke test to assert that ensure_data
    routes each artifact through the right path policy and yields a
    deterministic data_availability_hash.
    """
    file_path = _path_for(identity)
    return ArtifactRecord(
        id=0,
        artifact_kind=identity.artifact_kind,
        market=identity.market,
        symbol=identity.symbol,
        trading_date=identity.trading_date,
        resolution=identity.resolution,
        data_type=identity.data_type,
        provider=identity.provider,
        price_adjustment_mode=identity.price_adjustment_mode,
        data_contract_hash=_ZERO_HASH,
        file_path=file_path,
        file_sha256=_ZERO_SHA,
        row_count=390 if identity.resolution == "minute" else 1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
    )


def _path_for(identity: ArtifactIdentity) -> str:
    if identity.artifact_kind == "time_series_bars":
        if identity.resolution == "minute":
            return str(
                LeanMinuteBarPath(
                    market=identity.market,  # type: ignore[arg-type]
                    symbol=identity.symbol or "",
                    trading_date=identity.trading_date,  # type: ignore[arg-type]
                    data_type=identity.data_type,  # type: ignore[arg-type]
                ).relative_path()
            )
        if identity.resolution == "daily":
            return str(
                LeanDailyBarPath(
                    market=identity.market,  # type: ignore[arg-type]
                    symbol=identity.symbol or "",
                ).relative_path()
            )
    if identity.artifact_kind == "factor_file":
        return str(
            LeanFactorFilePath(
                market=identity.market, symbol=identity.symbol or ""  # type: ignore[arg-type]
            ).relative_path()
        )
    if identity.artifact_kind == "map_file":
        return str(
            LeanMapFilePath(
                market=identity.market, symbol=identity.symbol or ""  # type: ignore[arg-type]
            ).relative_path()
        )
    if identity.artifact_kind == "metadata":
        # In Slice 1a both metadata artifacts share the same fake path; the
        # data_contract_hash differs in 1c when we extract from a real image.
        return str(LeanMetadataPath(kind="market_hours").relative_path())
    raise ValueError(f"unsupported artifact_kind in fake stub: {identity.artifact_kind!r}")
```

- [ ] **Step 3: Smoke-test the module imports**

Run: `podman exec polygon-data-service python -c "from app.data_lake.fake_polygon import known_symbols; print(sorted(known_symbols()))"`
Expected: prints `['SPY']`.

- [ ] **Step 4: Commit**

```bash
git add PythonDataService/app/data_lake/fake_polygon.py \
        PythonDataService/tests/fixtures/data_lake_skeleton/canned_response.json
git commit -m "feat(data-lake): fixture-backed fake_polygon stub for Slice 1a"
```

---

### Task 16: `ensure_data.py` — main function

**Files:**
- Modify: `PythonDataService/app/data_lake/ensure_data.py` (append the `ensure_data` function)

- [ ] **Step 1: Add a test for ensure_data**

```python
# PythonDataService/tests/unit/data_lake/test_ensure_data.py
"""Unit tests for ensure_data with fixture-backed Polygon."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataRunSpec


def _spec(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        include_lean_metadata=True,
        lean_image_digest="sha256:test",
    )


@pytest.mark.asyncio
async def test_known_symbol_produces_complete_result():
    result = await ensure_data(_spec(["SPY"]))
    assert result.overall_status == "complete"
    assert result.failures == []
    assert len(result.artifacts) > 0
    assert all(a.symbol in {None, "SPY"} for a in result.artifacts)


@pytest.mark.asyncio
async def test_unknown_symbol_produces_partial_with_failures():
    result = await ensure_data(_spec(["UNKNOWN"]))
    assert result.overall_status in {"partial", "failed"}
    assert len(result.failures) > 0
    assert any(f.reason == "unknown_symbol" for f in result.failures)


@pytest.mark.asyncio
async def test_two_identical_calls_produce_same_availability_hash():
    a = await ensure_data(_spec(["SPY"]))
    b = await ensure_data(_spec(["SPY"]))
    assert a.data_availability_hash == b.data_availability_hash
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_ensure_data.py -v`
Expected: FAIL with `ImportError: cannot import name 'ensure_data'`.

- [ ] **Step 3: Add imports and the `ensure_data` function to `ensure_data.py`**

First, add the two new imports near the top of `ensure_data.py` (alongside the existing imports added in Task 14):

```python
from app.data_lake import fake_polygon
from app.data_lake.types import ArtifactFailure
```

(`time`, `json`, `hashlib`, `logging`, and the Pydantic types are already imported from Task 14 — do not re-import them.)

Then append this function at the end of `ensure_data.py` (after `_compute_data_availability_hash`):

```python
# Placeholder lean_data_root_path until Slice 1b wires the LEAN_DATA_ROOT env var.
# Slice 1b replaces this constant with settings.LEAN_DATA_ROOT.
_PLACEHOLDER_LEAN_DATA_ROOT = "/lean-data"


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Fixture-backed ensure_data (Slice 1a).

    Returns a canned DataAvailabilityResult. Known symbols (per fake_polygon)
    produce complete artifacts; unknown symbols produce per-artifact failures
    with reason='unknown_symbol'. No catalog writes, no Polygon calls.
    """
    started_ms = int(time.time() * 1000)
    required, non_sessions = expand_required_artifacts(spec)

    known = fake_polygon.known_symbols()
    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []

    for identity in required:
        if identity.symbol is None or identity.symbol in known:
            artifacts.append(fake_polygon.synth_artifact_record(identity))
        else:
            failures.append(
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=identity.trading_date,
                    data_type=identity.data_type,
                    reason="unknown_symbol",
                    detail=f"symbol {identity.symbol!r} not in Slice 1a stub set",
                    attempt_count=1,
                )
            )

    if failures and artifacts:
        overall_status = "partial"
    elif failures:
        overall_status = "failed"
    else:
        overall_status = "complete"

    completed_ms = int(time.time() * 1000)
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status=overall_status,
        lean_data_root_path=_PLACEHOLDER_LEAN_DATA_ROOT,
        data_availability_hash=_compute_data_availability_hash(artifacts),
        artifacts=artifacts,
        failures=failures,
        skipped_non_sessions=non_sessions,
        fetched_artifact_count=0,
        reused_artifact_count=len(artifacts),
        refreshed_artifact_count=0,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
```

- [ ] **Step 4: Run the tests**

Run: `podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_ensure_data.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/ensure_data.py \
        PythonDataService/tests/unit/data_lake/test_ensure_data.py
git commit -m "feat(data-lake): ensure_data skeleton with fixture-backed flow"
```

---

### Task 17: FastAPI route + feature flag wiring

**Files:**
- Create: `PythonDataService/app/routers/data_lake.py`
- Create: `PythonDataService/tests/integration/data_lake/test_ensure_data_route.py`
- Modify: `PythonDataService/app/main.py` (conditionally register the router)

- [ ] **Step 1: Implement the route**

```python
# PythonDataService/app/routers/data_lake.py
"""HTTP routes for the data lake.

POST /api/data-lake/ensure-data — invokes the in-process ensure_data() function.
Behind the DATA_LAKE_ENABLED feature flag; routes return 404 when the flag is off
(via main.py wiring, not this module).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.3
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataAvailabilityResult, DataRunSpec

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


@router.post("/ensure-data", response_model=DataAvailabilityResult)
async def post_ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    logger.info(
        "[STEP 1] /api/data-lake/ensure-data received: request_id=%s, symbols=%s",
        spec.request_id, spec.symbols,
    )
    return await ensure_data(spec)
```

- [ ] **Step 2: Wire the router conditionally in `main.py`**

In `PythonDataService/app/main.py`, after the existing `app.include_router(...)` calls (search for `app.include_router(engine.router)` or similar), add:

```python
# Data lake (Slice 1a) — gated by DATA_LAKE_ENABLED.
# When disabled, the prefix has no registered routes; clients get 404.
if settings.DATA_LAKE_ENABLED:
    from app.routers import data_lake as data_lake_router
    app.include_router(data_lake_router.router)
    logger.info("data lake routes ENABLED")
else:
    logger.info("data lake routes disabled (set DATA_LAKE_ENABLED=true to enable)")
```

- [ ] **Step 3: Write the integration test**

```python
# PythonDataService/tests/integration/data_lake/test_ensure_data_route.py
"""End-to-end test of POST /api/data-lake/ensure-data with the feature flag on."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

# Force the flag on for this test module BEFORE main is imported.
import os
os.environ["DATA_LAKE_ENABLED"] = "true"

from app.main import app  # noqa: E402

pytestmark = pytest.mark.asyncio


async def test_route_404_when_flag_off(monkeypatch):
    """Sanity check: when flag is off, the route is not registered."""
    # Build a fresh app instance with the flag off.
    from importlib import reload

    monkeypatch.setenv("DATA_LAKE_ENABLED", "false")
    import app.config as config_module
    import app.main as main_module
    reload(config_module)
    reload(main_module)
    fresh_app = main_module.app

    async with AsyncClient(transport=ASGITransport(app=fresh_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json={})
        assert r.status_code == 404

    # Restore the flag for the rest of the module.
    monkeypatch.setenv("DATA_LAKE_ENABLED", "true")
    reload(config_module)
    reload(main_module)


async def test_post_ensure_data_known_symbol():
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "include_lean_metadata": True,
        "lean_image_digest": "sha256:test",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["data_availability_hash"]


async def test_post_ensure_data_422_on_bad_symbol():
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["spy"],  # lowercase — rejected by validator
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 422
```

- [ ] **Step 4: Run the tests**

Run: `podman exec polygon-data-service python -m pytest tests/integration/data_lake/test_ensure_data_route.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/routers/data_lake.py \
        PythonDataService/app/main.py \
        PythonDataService/tests/integration/data_lake/test_ensure_data_route.py
git commit -m "feat(data-lake): POST /api/data-lake/ensure-data behind feature flag"
```

---

### Task 18: Project-scope lint + full test pass + final commit

**Files:**
- None (verification only)

- [ ] **Step 1: Run project-scope ruff**

Run from the host (NOT inside the container — the host has the correct ruff config; per the user's memory):
```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: no warnings or errors. If any, fix them locally before continuing.

- [ ] **Step 2: Run full Python test suite**

Run: `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"`
Expected: all tests pass. New `data_lake` tests should be visible in the output.

- [ ] **Step 3: Run Backend test suite**

Run: `cd Backend.Tests && dotnet test`
Expected: PASS.

- [ ] **Step 4: Run Frontend test suite (sanity)**

Run: `podman exec my-frontend npx ng test --watch=false`
Expected: PASS (no Frontend changes in this slice, but verify nothing regressed indirectly).

- [ ] **Step 5: Verify the gated route is invisible by default**

```bash
podman exec polygon-data-service env | grep DATA_LAKE_ENABLED
```
Expected: blank / not set / `DATA_LAKE_ENABLED=false`.

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/api/data-lake/ensure-data -H 'Content-Type: application/json' -d '{}'
```
Expected: `404`.

- [ ] **Step 6: If anything in Steps 1–5 needs cleanup, fix and commit; otherwise no commit needed**

Final state: all Slice 1a work is in place; CI green; no production path exposed; `DATA_LAKE_ENABLED` flag holds the gate.

---

## Self-review

After completing all 18 tasks, verify against the spec:

1. **Spec coverage** (Slice 1a deliverables):
   - [x] EF Core migrations for `data_lake_artifacts` + `data_lake_runs` (Tasks 1–4)
   - [x] `app/data_lake/{path_policy,catalog_schema,catalog_client}.py` (Tasks 6, 9, 11)
   - [x] Schema-drift integration test (Task 12)
   - [x] Fixture-backed `ensure_data` skeleton (Tasks 14–16)
   - [x] Feature flag gating (Task 17)

2. **Type consistency**:
   - `ArtifactIdentity` (internal in `types.py`) vs `ArtifactRecord` (in `DataAvailabilityResult`) — fields align; `ArtifactRecord` adds `id`, `data_contract_hash`, `file_*`, `row_count`, `first/last_bar_start_ms`.
   - `DataRunSpec.symbols` validator regex matches `^[A-Z][A-Z0-9.]*$` — Task 8 test verifies; Task 17 422-on-bad-symbol test verifies at the route boundary.

3. **Spec features deferred (correctly) to later slices**:
   - Real Polygon fetch → Slice 1b
   - Atomic write protocol + leases → Slice 1b
   - Factor / map files real generation → Slice 1c (Slice 1a stubs them)
   - Derived quote / daily real synthesis → Slice 1c (Slice 1a stubs them)
   - `prepare_run` → Slice 1d
   - Backend GraphQL orchestration → Slice 1d
   - Launcher path-under-root changes → Slice 1d
   - `LeanMinuteDataReader` cutover → Slice 2

4. **Conventions check**:
   - Python: `from __future__ import annotations` everywhere, type hints on all signatures, `async def` for I/O, Pydantic v2 model_validator (not v1 `@validator`).
   - .NET: PascalCase, `[Required]`/`[MaxLength]`, `_camelCase` for private (none in this slice).
   - Tests: `test_<function>_<scenario>` naming.
   - All timestamps as `int64 ms UTC` (`*_ms` columns); only `trading_date` as `date`.

Slice 1a complete when all 18 tasks are checked off and CI is green.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-data-lake-slice-1a.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
