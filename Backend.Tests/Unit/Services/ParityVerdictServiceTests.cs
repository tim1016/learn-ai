using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;

namespace Backend.Tests.Unit.Services;

public class ParityVerdictServiceTests
{
    private const string MatchingRunVerdict = """
        {
          "verdict_version": 2,
          "status": "complete",
          "composite": 76,
          "grade": "A",
          "signal": "Paper-trade",
          "red_flags": [],
          "dimensions": [{"key":"return_quality","score":70,"sub_scores":[]}],
          "missing_metrics": [],
          "missing_required_metrics": [],
          "available_required_metrics": 17,
          "required_metrics": 17,
          "normalized_weights": false,
          "parity_signature": {
            "contract_id": "readiness-core-v2",
            "absolute_tolerance": "0.000000000001",
            "status": "complete",
            "composite": 76,
            "grade": "A",
            "signal": "Paper-trade",
            "red_flags": [],
            "missing_required_metrics": [],
            "available_required_metrics": 17,
            "required_metrics": 17,
            "normalized_weights": false,
            "required_inputs": []
          }
        }
        """;

    private const string MatchingNativeStatistics = """
        {
          "namespaces": {
            "status": "match",
            "contract_id": "lean-native-statistics-commit",
            "source_commit": "commit",
            "absolute_tolerance": 0.0000500001,
            "native_metric_count": 66,
            "formatted_metric_count": 25,
            "divergences": []
          }
        }
        """;

    private const string MatchingDataPolicy = """
        {
          "source": "polygon",
          "symbol": "SPY",
          "adjusted": false,
          "session": "regular",
          "input_bars": {"timespan":"minute","multiplier":1},
          "strategy_bars": {"timespan":"minute","multiplier":15},
          "timestamp_policy": "bar_close_ms_utc",
          "timezone": "America/New_York",
          "provider_kind": "fixture",
          "fixture_id": "bar-store-v1-exact",
          "fixture_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }
        """;

    private static ParityVerdictService CreateService(
        Backend.Data.AppDbContext db,
        CompareTradesResponse comparison)
    {
        var comparisonService = new Mock<IComparisonService>();
        comparisonService
            .Setup(c => c.CompareTradesAsync(It.IsAny<CompareTradesRequest>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(comparison);
        return new ParityVerdictService(db, comparisonService.Object, NullLogger<ParityVerdictService>.Instance);
    }

    private static async Task<(int leftId, int rightId)> SeedGroupAsync(
        Backend.Data.AppDbContext db,
        string group,
        string? pendingStatus = "pending")
    {
        var ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" };
        var left = new StrategyExecution
        {
            Ticker = ticker,
            Source = "engine",
            StrategyName = "spy_ema_crossover",
            ParityGroupId = group,
            RunVerdictJson = MatchingRunVerdict,
            DataPolicyJson = MatchingDataPolicy,
            InitialCash = 100_000m,
            StartDate = "2026-01-05",
            EndDate = "2026-01-06",
            FillMode = "signal_bar_close",
        };
        var right = new StrategyExecution
        {
            Ticker = ticker,
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanRunId = $"companion-{group}",
            ParityGroupId = group,
            RunVerdictJson = MatchingRunVerdict,
            LeanStatisticsJson = MatchingNativeStatistics,
            DataPolicyJson = MatchingDataPolicy,
            InitialCash = 100_000m,
            StartDate = "2026-01-05",
            EndDate = "2026-01-06",
            FillMode = "signal_bar_close",
        };
        db.StrategyExecutions.AddRange(left, right);
        await db.SaveChangesAsync();

        if (pendingStatus is not null)
        {
            db.ParityVerdicts.Add(new ParityVerdict
            {
                LeftExecutionId = left.Id,
                RightExecutionId = null,
                ParityGroupId = group,
                VerdictVersion = ParityVerdictService.VerdictVersion,
                Status = pendingStatus,
                VerdictJson = "{}",
            });
            await db.SaveChangesAsync();
        }

        return (left.Id, right.Id);
    }

    private static CompareTradesResponse NoDivergences() => new([], null);

    private static CompareTradesResponse WithDivergence() => new(
        [new TradeDivergenceRecord("FILL_PRICE_DRIFT", 1, 1_700_000_000_000, "fill differs")],
        1_700_000_000_000);

    [Fact]
    public async Task Compute_NoDivergences_FreezesAgreeOntoPendingRow()
    {
        var db = TestDbContextFactory.Create();
        var (leftId, rightId) = await SeedGroupAsync(db, "pg-agree");
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-agree", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-agree");
        Assert.Equal("agree", row.Status);
        Assert.Equal(rightId, row.RightExecutionId);
        Assert.Equal(leftId, row.LeftExecutionId);
        Assert.Contains("\"status\":\"agree\"", row.VerdictJson);
        Assert.Contains("\"native_metric_parity\":{\"status\":\"match\"", row.VerdictJson);
        Assert.Contains("\"readiness_parity\":{\"status\":\"match\"", row.VerdictJson);
        Assert.Contains("\"compared_field_count\":17", row.VerdictJson);
        Assert.Contains("\"input_parity\":{\"status\":\"match\"", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_WithDivergences_FreezesDivergedWithCategoryCounts()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-div");
        var service = CreateService(db, WithDivergence());

        await service.ComputeForLeanRunAsync(rightId, "pg-div", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-div");
        Assert.Equal("diverged", row.Status);
        Assert.Contains("FILL_PRICE_DRIFT", row.VerdictJson);
        Assert.Contains("counts_by_category", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_TerminalRow_IsNeverOverwritten()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-final", pendingStatus: "run_failed");
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-final", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-final");
        Assert.Equal("run_failed", row.Status);
    }

    [Fact]
    public async Task Compute_ReadinessMismatch_FreezesDivergedWithExactFieldReceipt()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-readiness");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.RunVerdictJson = MatchingRunVerdict.Replace("\"grade\": \"A\"", "\"grade\": \"B\"");
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-readiness", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-readiness");
        Assert.Equal("diverged", row.Status);
        Assert.Contains("production_readiness_mismatch", row.VerdictJson);
        Assert.Contains("\"mismatched_fields\":[\"parity_signature\"]", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_ReadinessContractVersionDiffers_FreezesUnavailableNotMismatch()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-readiness-version");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.RunVerdictJson = MatchingRunVerdict.Replace(
            "\"contract_id\": \"readiness-core-v2\"",
            "\"contract_id\": \"readiness-core-v3\"");
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-readiness-version", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-readiness-version");
        Assert.Equal("unavailable", row.Status);
        Assert.Contains("\"readiness_parity\":{\"status\":\"unavailable\"", row.VerdictJson);
        Assert.Contains("readiness_contract_version_differs", row.VerdictJson);
        Assert.DoesNotContain("production_readiness_mismatch", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_ReadinessReceipt_IgnoresNonCanonicalPresentationNoise()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-readiness-noise");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.RunVerdictJson = MatchingRunVerdict.Replace("\"score\":70", "\"score\":71");
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-readiness-noise", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-readiness-noise");
        Assert.Equal("agree", row.Status);
        Assert.Contains("\"readiness_parity\":{\"status\":\"match\"", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_NativeMetricMismatch_FreezesDiverged()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-native");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.LeanStatisticsJson = MatchingNativeStatistics.Replace("\"status\": \"match\"", "\"status\": \"mismatch\"");
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-native", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-native");
        Assert.Equal("diverged", row.Status);
        Assert.Contains("lean_native_metric_mismatch", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_MissingMetricReceipt_FreezesUnavailableInsteadOfFalseAgreement()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-unavailable");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.LeanStatisticsJson = "{}";
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-unavailable", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-unavailable");
        Assert.Equal("unavailable", row.Status);
        Assert.Contains("lean_native_metric_parity_unavailable", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_SharedFixtureMismatch_FreezesDiverged()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-input");
        var right = await db.StrategyExecutions.SingleAsync(e => e.Id == rightId);
        right.DataPolicyJson = MatchingDataPolicy.Replace("bar-store-v1-exact", "bar-store-v1-changed");
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-input", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-input");
        Assert.Equal("diverged", row.Status);
        Assert.Contains("compatibility_input_mismatch", row.VerdictJson);
        Assert.Contains("fixture_id", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_MissingPendingRow_InsertsTerminalVerdict()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-lost", pendingStatus: null);
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-lost", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-lost");
        Assert.Equal("agree", row.Status);
    }

    [Fact]
    public async Task Compute_MissingLeftRun_LeavesVerdictPending()
    {
        var db = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" };
        var right = new StrategyExecution
        {
            Ticker = ticker,
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanRunId = "companion-pg-orphan",
            ParityGroupId = "pg-orphan",
        };
        db.StrategyExecutions.Add(right);
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(right.Id, "pg-orphan", CancellationToken.None);

        Assert.False(await db.ParityVerdicts.AnyAsync(v => v.ParityGroupId == "pg-orphan"));
    }
}
