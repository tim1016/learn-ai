using System.Text.Json;
using Backend.Models.MarketData;

namespace Backend.Tests.Unit.Models;

/// <summary>
/// Regression tests for <see cref="PersistLeanRunPayload"/> JSON deserialization.
///
/// Before the Task 1.11 fix, the record's positional constructor parameters had no
/// [JsonPropertyName] attributes, so a snake_case payload from PythonDataService
/// bound every field as null/default — causing a NOT NULL DB constraint violation on
/// StrategyName. These tests pin the exact key names Python sends so any future
/// rename of the Python fields surfaces here rather than as a 500 from the DB.
/// </summary>
public class PersistLeanRunPayloadTests
{
    private static readonly JsonSerializerOptions _opts = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    [Fact]
    public void Deserialize_SnakeCasePayload_BindsAllFields()
    {
        const string json = """
            {
                "lean_run_id": "my-run-001",
                "source": "lean-sidecar",
                "strategy_name": "ema_crossover",
                "symbol": "SPY",
                "starting_cash": 100000,
                "start_date_ms": 1736173800000,
                "end_date_ms": 1737469800000,
                "total_trades": 2,
                "winning_trades": 1,
                "losing_trades": 1,
                "total_pnl": 42.50,
                "total_fees": 9.98,
                "final_equity": 100042.50,
                "win_rate": 0.5,
                "trades": [
                    {
                        "trade_number": 1,
                        "entry_ms_utc": 1736175000000,
                        "exit_ms_utc": 1736179500000,
                        "entry_price": 580.00,
                        "exit_price": 582.00,
                        "quantity": 172,
                        "pnl": 344.00,
                        "signal_reason": "EMA crossover exit (5-bar time stop)",
                        "is_synthetic_exit": false
                    }
                ],
                "lean_statistics": { "parser_version": "phase-3a-r1" }
            }
            """;

        var payload = JsonSerializer.Deserialize<PersistLeanRunPayload>(json, _opts);

        Assert.NotNull(payload);
        Assert.Equal("my-run-001", payload.LeanRunId);
        Assert.Equal("lean-sidecar", payload.Source);
        Assert.Equal("ema_crossover", payload.StrategyName);
        Assert.Equal("SPY", payload.Symbol);
        Assert.Equal(100_000m, payload.StartingCash);
        Assert.Equal(1_736_173_800_000L, payload.StartDateMs);
        Assert.Equal(1_737_469_800_000L, payload.EndDateMs);
        Assert.Equal(2, payload.TotalTrades);
        Assert.Equal(1, payload.WinningTrades);
        Assert.Equal(1, payload.LosingTrades);
        Assert.Equal(42.50m, payload.TotalPnl);
        Assert.Equal(9.98m, payload.TotalFees);
        Assert.Equal(100_042.50m, payload.FinalEquity);
        Assert.Equal(0.5, payload.WinRate, precision: 9);
        Assert.Single(payload.Trades);

        var trade = payload.Trades[0];
        Assert.Equal(1, trade.TradeNumber);
        Assert.Equal(1_736_175_000_000L, trade.EntryMsUtc);
        Assert.Equal(1_736_179_500_000L, trade.ExitMsUtc);
        Assert.Equal(580.00m, trade.EntryPrice);
        Assert.Equal(582.00m, trade.ExitPrice);
        Assert.Equal(172m, trade.Quantity);
        Assert.Equal(344.00m, trade.Pnl);
        Assert.Equal("EMA crossover exit (5-bar time stop)", trade.SignalReason);
        Assert.False(trade.IsSyntheticExit);
    }

    [Fact]
    public void Deserialize_SnakeCasePayload_StrategyNameNotNull()
    {
        // Regression: before JsonPropertyName fix, strategy_name was null and the
        // NOT NULL DB constraint fired with "null value in column StrategyName".
        const string json = """
            {
                "lean_run_id": "r2",
                "source": "lean-sidecar",
                "strategy_name": "ema_crossover",
                "symbol": "SPY",
                "starting_cash": 100000,
                "start_date_ms": 1736173800000,
                "end_date_ms": 1737469800000,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0,
                "total_fees": 0,
                "final_equity": 100000,
                "win_rate": 0,
                "trades": [],
                "lean_statistics": null
            }
            """;

        var payload = JsonSerializer.Deserialize<PersistLeanRunPayload>(json, _opts);

        Assert.NotNull(payload);
        Assert.NotNull(payload.StrategyName);
        Assert.Equal("ema_crossover", payload.StrategyName);
    }
}
