using System.Net;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Backend.Tests.Unit.Services;

public class SpecStrategyServiceTests
{
    private const string ValidSpecJson = """
    {
      "schema_version": "1.0",
      "name": "test spec",
      "symbols": ["SPY"],
      "resolution": {"period_minutes": 15},
      "indicators": [],
      "entry": {
        "logic": "AND",
        "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 0}],
        "size": {"kind": "SetHoldings", "fraction": 1.0}
      },
      "exit": {"logic": "OR", "conditions": []}
    }
    """;

    // Single trade in the body so the int64-ms timestamp deserialization
    // is actually exercised — empty Trades arrays would silently accept
    // a wrong DTO type for entry_time/exit_time.
    private const string SuccessResponseBody = """
    {
      "success": true,
      "strategy_name": "test spec",
      "initial_cash": 100000.0,
      "final_equity": 102500.0,
      "net_profit": 2500.0,
      "total_fees": 0.0,
      "total_trades": 1,
      "winning_trades": 1,
      "losing_trades": 0,
      "win_rate": 1.0,
      "trades": [
        {
          "trade_number": 1,
          "entry_time": 1704153600000,
          "entry_price": 470.5,
          "exit_time": 1704157200000,
          "exit_price": 472.1,
          "indicators": {"sma_s": 470.4, "sma_l": 470.0},
          "pnl_pts": 1.6,
          "pnl_pct": 0.0034,
          "result": "WIN",
          "signal_reason": "test"
        }
      ],
      "log_lines": ["Trade 1: WIN"],
      "error": null
    }
    """;

    [Fact]
    public async Task RunBacktestAsync_HappyPath_ReturnsParsedResult()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, SuccessResponseBody);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var service = new SpecStrategyService(httpClient, NullLogger<SpecStrategyService>.Instance);

        var request = new SpecBacktestRequestDto(
            Spec: ValidSpecJson,
            StartDate: "2024-01-02",
            EndDate: "2024-12-31",
            InitialCash: 100000m
        );

        var result = await service.RunBacktestAsync(request);

        Assert.True(result.Success);
        Assert.Equal("test spec", result.StrategyName);
        Assert.Equal(1, result.TotalTrades);
        Assert.Equal(1.0m, result.WinRate);
        Assert.Single(result.LogLines);

        // Trade timestamps round-trip as int64 ms UTC — proves the
        // long-typed DTO fields deserialize correctly from JSON ints.
        // 1704153600000 ms = 2024-01-02 00:00:00 UTC.
        var trade = Assert.Single(result.Trades);
        Assert.Equal(1704153600000L, trade.EntryTime);
        Assert.Equal(1704157200000L, trade.ExitTime);
    }

    [Fact]
    public async Task RunBacktestAsync_PostsToCorrectEndpoint_WithSpecAsObject()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, SuccessResponseBody);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var service = new SpecStrategyService(httpClient, NullLogger<SpecStrategyService>.Instance);

        await service.RunBacktestAsync(new SpecBacktestRequestDto(
            Spec: ValidSpecJson,
            StartDate: "2024-01-02",
            EndDate: "2024-12-31"
        ));

        Assert.NotNull(handler.LastRequestUri);
        Assert.EndsWith("/api/spec-strategy/backtest", handler.LastRequestUri!.AbsolutePath);
        Assert.Equal(HttpMethod.Post, handler.LastRequestMethod);

        // Body must contain "spec" as a JSON object (not a string), so the
        // Python endpoint can validate it as a StrategySpec.
        Assert.NotNull(handler.LastRequestBody);
        using var doc = JsonDocument.Parse(handler.LastRequestBody!);
        var root = doc.RootElement;
        Assert.Equal(JsonValueKind.Object, root.GetProperty("spec").ValueKind);
        Assert.Equal("test spec", root.GetProperty("spec").GetProperty("name").GetString());
        Assert.Equal("2024-01-02", root.GetProperty("start_date").GetString());
    }

    [Fact]
    public async Task RunBacktestAsync_InvalidSpecJson_Throws()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, SuccessResponseBody);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var service = new SpecStrategyService(httpClient, NullLogger<SpecStrategyService>.Instance);

        var request = new SpecBacktestRequestDto(
            Spec: "this is not json {",
            StartDate: "2024-01-02",
            EndDate: "2024-12-31"
        );

        await Assert.ThrowsAsync<ArgumentException>(() => service.RunBacktestAsync(request));
    }

    [Fact]
    public async Task RunBacktestAsync_PythonReturns400_RaisesHttpRequestException()
    {
        var handler = new FakeHttpMessageHandler(
            HttpStatusCode.BadRequest,
            """{"detail":"spec uses unsupported feature: option template"}""");
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var service = new SpecStrategyService(httpClient, NullLogger<SpecStrategyService>.Instance);

        var request = new SpecBacktestRequestDto(
            Spec: ValidSpecJson,
            StartDate: "2024-01-02",
            EndDate: "2024-12-31"
        );

        await Assert.ThrowsAsync<HttpRequestException>(() => service.RunBacktestAsync(request));
    }

    [Fact]
    public async Task RunBacktestAsync_PythonReturnsSuccessFalse_SurfacesError()
    {
        const string body = """
        {
          "success": false,
          "strategy_name": "broken",
          "initial_cash": 0.0,
          "final_equity": 0.0,
          "net_profit": 0.0,
          "total_fees": 0.0,
          "total_trades": 0,
          "winning_trades": 0,
          "losing_trades": 0,
          "win_rate": 0.0,
          "trades": [],
          "log_lines": [],
          "error": "data source unavailable"
        }
        """;
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, body);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var service = new SpecStrategyService(httpClient, NullLogger<SpecStrategyService>.Instance);

        var result = await service.RunBacktestAsync(new SpecBacktestRequestDto(
            Spec: ValidSpecJson,
            StartDate: "2024-01-02",
            EndDate: "2024-12-31"
        ));

        Assert.False(result.Success);
        Assert.Equal("data source unavailable", result.Error);
    }
}
