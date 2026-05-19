using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Unit.Services;

public class BacktestRunPersistenceServiceTests
{
    private static BacktestRunPersistenceService CreateService(out Backend.Data.AppDbContext db)
    {
        db = TestDbContextFactory.Create();
        return new BacktestRunPersistenceService(db, NullLogger<BacktestRunPersistenceService>.Instance);
    }

    private static PersistLeanRunPayload BuildPayload(
        string leanRunId,
        IReadOnlyList<PersistLeanTradePayload>? trades = null)
    {
        var tradeList = trades ?? Array.Empty<PersistLeanTradePayload>();
        return new PersistLeanRunPayload(
            LeanRunId: leanRunId,
            Source: "lean-sidecar",
            StrategyName: "ema_crossover",
            Symbol: "SPY",
            StartingCash: 100_000m,
            StartDateMs: 1_700_000_000_000,
            EndDateMs: 1_700_000_600_000,
            TotalTrades: tradeList.Count,
            WinningTrades: tradeList.Count(t => t.Pnl > 0m),
            LosingTrades: tradeList.Count(t => t.Pnl < 0m),
            TotalPnl: tradeList.Sum(t => t.Pnl),
            TotalFees: 1m,
            FinalEquity: 100_008m,
            WinRate: tradeList.Count > 0 ? 1.0 : 0.0,
            Trades: tradeList,
            LeanStatistics: new Dictionary<string, object>
            {
                ["parser_version"] = "phase-3a-r1",
            });
    }

    [Fact]
    public async Task PersistAsync_NewRun_WritesStrategyExecutionAndTrades()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(
            leanRunId: "ui_run_new",
            trades: new[]
            {
                new PersistLeanTradePayload(
                    TradeNumber: 1,
                    EntryMsUtc: 1_700_000_060_000,
                    ExitMsUtc: 1_700_000_600_000,
                    EntryPrice: 100m,
                    ExitPrice: 101m,
                    Quantity: 10m,
                    Pnl: 9m,
                    SignalReason: "EMA exit",
                    IsSyntheticExit: false),
            });

        var id = await service.PersistAsync(payload, CancellationToken.None);

        var row = await db.StrategyExecutions.SingleAsync(s => s.Id == id);
        Assert.Equal("lean-sidecar", row.Source);
        Assert.Equal("ui_run_new", row.LeanRunId);
        Assert.Equal("ema_crossover", row.StrategyName);
        Assert.Equal(1, row.TotalTrades);
        Assert.Equal(9m, row.TotalPnL);
        Assert.Equal(100_008m, row.FinalEquity);

        var trade = await db.BacktestTrades.SingleAsync(t => t.StrategyExecutionId == id);
        Assert.Equal(100m, trade.EntryPrice);
        Assert.Equal(101m, trade.ExitPrice);
        Assert.Equal(9m, trade.PnL);
        Assert.False(trade.IsSyntheticExit);
    }

    [Fact]
    public async Task PersistAsync_IdempotentOnLeanRunId()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(leanRunId: "ui_run_idempotent");

        var id1 = await service.PersistAsync(payload, CancellationToken.None);
        var id2 = await service.PersistAsync(payload, CancellationToken.None);

        Assert.Equal(id1, id2);
        var count = await db.StrategyExecutions
            .CountAsync(s => s.LeanRunId == "ui_run_idempotent");
        Assert.Equal(1, count);
    }

    [Fact]
    public async Task PersistAsync_FailedRun_WritesZeroTradeRow()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(leanRunId: "ui_run_failed") with
        {
            TotalTrades = 0,
            TotalPnl = 0m,
            FinalEquity = 100_000m,
            WinRate = 0.0,
            Trades = Array.Empty<PersistLeanTradePayload>(),
            LeanStatistics = new Dictionary<string, object>
            {
                ["error"] = "No normalized/result.json",
            },
        };

        var id = await service.PersistAsync(payload, CancellationToken.None);

        var row = await db.StrategyExecutions.SingleAsync(s => s.Id == id);
        Assert.Equal(0, row.TotalTrades);

        var tradesForRun = await db.BacktestTrades
            .CountAsync(t => t.StrategyExecutionId == id);
        Assert.Equal(0, tradesForRun);
    }

    [Fact]
    public async Task PersistAsync_RejectsUnknownSource()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "ui_run_wrong") with { Source = "strategy-lab" };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("source", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task PersistAsync_EngineSource_AcceptsNullLeanRunId()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(leanRunId: "placeholder") with
        {
            Source = "engine",
            LeanRunId = null,
            StrategyName = "ema_crossover",
            Trades = new[]
            {
                new PersistLeanTradePayload(
                    TradeNumber: 1,
                    EntryMsUtc: 1_700_000_060_000,
                    ExitMsUtc: 1_700_000_120_000,
                    EntryPrice: 400m,
                    ExitPrice: 401m,
                    Quantity: 250m,
                    Pnl: 250m,
                    SignalReason: "EMA exit",
                    IsSyntheticExit: false),
            },
            TotalTrades = 1,
            WinningTrades = 1,
            LosingTrades = 0,
            TotalPnl = 250m,
            FinalEquity = 100_250m,
            WinRate = 1.0,
        };

        var id = await service.PersistAsync(payload, CancellationToken.None);

        var row = await db.StrategyExecutions.SingleAsync(s => s.Id == id);
        Assert.Equal("engine", row.Source);
        Assert.Null(row.LeanRunId);
        Assert.Equal("signal_bar_close", row.FillMode);
        Assert.Equal(1, row.TotalTrades);
    }

    [Fact]
    public async Task PersistAsync_EngineSource_NotIdempotent_CreatesNewRowEachCall()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(leanRunId: "placeholder") with
        {
            Source = "engine",
            LeanRunId = null,
        };

        var id1 = await service.PersistAsync(payload, CancellationToken.None);
        var id2 = await service.PersistAsync(payload, CancellationToken.None);

        Assert.NotEqual(id1, id2);
        Assert.Equal(2, await db.StrategyExecutions.CountAsync(s => s.Source == "engine"));
    }

    [Fact]
    public async Task PersistAsync_LeanSidecarSource_RequiresNonEmptyLeanRunId()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "placeholder") with
        {
            Source = "lean-sidecar",
            LeanRunId = null,
        };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("lean_run_id", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    // Fix 8 — boundary validation tests

    [Fact]
    public async Task PersistAsync_EmptyLeanRunId_ThrowsArgumentException()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "placeholder") with { LeanRunId = "" };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("lean_run_id", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task PersistAsync_WhitespaceLeanRunId_ThrowsArgumentException()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "placeholder") with { LeanRunId = "   " };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("lean_run_id", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task PersistAsync_EmptySymbol_ThrowsArgumentException()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "ui_run_bad_symbol") with { Symbol = "" };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("symbol", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task PersistAsync_NullTrades_ThrowsArgumentException()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "ui_run_null_trades") with { Trades = null! };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("trades", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task PersistAsync_StartDateAfterEndDate_ThrowsArgumentException()
    {
        var service = CreateService(out _);
        var payload = BuildPayload(leanRunId: "ui_run_bad_dates") with
        {
            StartDateMs = 1_700_000_600_000,
            EndDateMs = 1_700_000_000_000,
        };

        var ex = await Assert.ThrowsAsync<ArgumentException>(
            () => service.PersistAsync(payload, CancellationToken.None));

        Assert.Contains("start_date_ms", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    // Fix 6 — transaction happy-path: trades written under the same StrategyExecutionId

    [Fact]
    public async Task PersistAsync_TradesWrittenUnderSameExecutionId()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(
            leanRunId: "ui_run_tx_happy",
            trades: new[]
            {
                new PersistLeanTradePayload(
                    TradeNumber: 1,
                    EntryMsUtc: 1_700_000_060_000,
                    ExitMsUtc: 1_700_000_120_000,
                    EntryPrice: 200m,
                    ExitPrice: 202m,
                    Quantity: 5m,
                    Pnl: 10m,
                    SignalReason: "EMA cross",
                    IsSyntheticExit: false),
                new PersistLeanTradePayload(
                    TradeNumber: 2,
                    EntryMsUtc: 1_700_000_180_000,
                    ExitMsUtc: 1_700_000_600_000,
                    EntryPrice: 202m,
                    ExitPrice: 201m,
                    Quantity: 5m,
                    Pnl: -5m,
                    SignalReason: "EMA cross",
                    IsSyntheticExit: false),
            });

        var id = await service.PersistAsync(payload, CancellationToken.None);

        var tradeCount = await db.BacktestTrades.CountAsync(t => t.StrategyExecutionId == id);
        Assert.Equal(2, tradeCount);

        // Verify the execution row exists with the expected Id
        var execution = await db.StrategyExecutions.SingleAsync(s => s.Id == id);
        Assert.Equal("ui_run_tx_happy", execution.LeanRunId);
    }

    // Fix 7 — idempotency: repeated calls with the same LeanRunId return the same id
    // (covers the happy path; real race-condition rollback requires a Postgres integration test)

    [Fact]
    public async Task PersistAsync_SameLeanRunId_ReturnsSameId()
    {
        var service = CreateService(out var db);
        var payload = BuildPayload(leanRunId: "ui_run_dedup");

        var id1 = await service.PersistAsync(payload, CancellationToken.None);
        var id2 = await service.PersistAsync(payload, CancellationToken.None);

        Assert.Equal(id1, id2);
        Assert.Equal(1, await db.StrategyExecutions.CountAsync(s => s.LeanRunId == "ui_run_dedup"));
    }
}
