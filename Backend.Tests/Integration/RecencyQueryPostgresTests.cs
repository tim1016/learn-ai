using Backend.Data;
using Backend.GraphQL;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Integration;

/// <summary>
/// Proves RecencyQuery's LINQ actually translates to SQL on the real
/// Npgsql provider — InMemory (RecencyQueryTests) is far more permissive
/// than Npgsql's translator and would not have caught the grouped
/// top-1-per-group query this resolver deliberately avoids (see
/// GetRecencyHero's comment). Skips when
/// BACKEND_TEST_POSTGRES_CONNECTION_STRING is unset, matching every other
/// Postgres integration test in this suite.
/// </summary>
public class RecencyQueryPostgresTests
{
    [Fact]
    public async Task GetRecencyTrades_And_GetRecencyHero_TranslateAndExecuteOnPostgres()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();

        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseNpgsql(database.ConnectionString)
            .Options;
        await using var db = new AppDbContext(options);

        db.RecencyLaunches.Add(new RecencyLaunch { Id = "l1", ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1 });
        await db.SaveChangesAsync();

        var runLow = new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h-low",
            TotalPnl = 5m,
            CreatedAtMs = 1,
        };
        var runHigh = new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h-high",
            TotalPnl = 50m,
            Sharpe = 1.2m,
            StudyId = 7,
            CreatedAtMs = 1,
        };
        db.RecencyRuns.AddRange(runLow, runHigh);
        await db.SaveChangesAsync();

        var trade = new RecencyTrade
        {
            RecencyRunId = runHigh.Id,
            Fingerprint = "fp1",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = 2m,
            PnlPct = 0.02m,
            Quantity = 10m,
            Pnl = 20m,
            HoldingSessions = 1,
        };
        db.RecencyTrades.Add(trade);
        await db.SaveChangesAsync();
        db.RecencyTradeMemberships.Add(new RecencyTradeMembership { RecencyTradeId = trade.Id, RecencyRunId = runHigh.Id });
        await db.SaveChangesAsync();

        var query = new RecencyQuery();

        var trades = await query.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: new List<string> { "SPY" }, strategies: null, CancellationToken.None);
        Assert.Single(trades);
        Assert.Equal("fp1", trades[0].Fingerprint);
        Assert.Equal(1.2m, trades[0].Sharpe);
        Assert.Equal(7, trades[0].StudyId);

        var hero = await query.GetRecencyHero(db, symbols: null, strategies: null, fromMs: null, toMs: null, CancellationToken.None);
        Assert.Single(hero);
        Assert.Equal("h-high", hero[0].ParamsHash);
        Assert.Equal(runHigh.Id, hero[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyHero_WithAWindow_TranslatesTheCorrelatedSumSubqueryOnPostgres()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();

        var options = new DbContextOptionsBuilder<AppDbContext>().UseNpgsql(database.ConnectionString).Options;
        await using var db = new AppDbContext(options);

        db.RecencyLaunches.Add(new RecencyLaunch { Id = "l1", ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1 });
        await db.SaveChangesAsync();

        // All-time winner (large TotalPnl from its own generation window)
        // whose only trade actually inside the requested window is a
        // loser — the windowed query must NOT pick it.
        var allTimeWinner = new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h-alltime",
            TotalPnl = 500m,
            CreatedAtMs = 1,
        };
        var windowWinner = new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h-window",
            TotalPnl = 50m,
            CreatedAtMs = 1,
        };
        db.RecencyRuns.AddRange(allTimeWinner, windowWinner);
        await db.SaveChangesAsync();

        db.RecencyTrades.Add(new RecencyTrade
        {
            RecencyRunId = allTimeWinner.Id,
            Fingerprint = "fp-alltime-in-window",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = -1m,
            PnlPct = -0.01m,
            Quantity = 10m,
            Pnl = -10m,
            HoldingSessions = 1,
        });
        db.RecencyTrades.Add(new RecencyTrade
        {
            RecencyRunId = windowWinner.Id,
            Fingerprint = "fp-window-in-window",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = 20m,
            PnlPct = 0.2m,
            Quantity = 10m,
            Pnl = 200m,
            HoldingSessions = 1,
        });
        await db.SaveChangesAsync();

        var query = new RecencyQuery();
        var hero = await query.GetRecencyHero(db, symbols: null, strategies: null, fromMs: 100, toMs: 1000, CancellationToken.None);

        Assert.Single(hero);
        Assert.Equal("h-window", hero[0].ParamsHash);
        Assert.Equal(200m, hero[0].TotalPnl);
    }
}
