using System.Net;
using System.Text.Json;
using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Unit.Services;

public class ComparisonServiceTests
{
    private static PersistLeanTradePayload MakeTrade(
        int n,
        long entryMs,
        long exitMs,
        decimal entryPrice,
        decimal exitPrice,
        decimal qty = 10m) =>
        new(
            TradeNumber: n,
            EntryMsUtc: entryMs,
            ExitMsUtc: exitMs,
            EntryPrice: entryPrice,
            ExitPrice: exitPrice,
            Quantity: qty,
            Pnl: (exitPrice - entryPrice) * qty,
            SignalReason: "test",
            IsSyntheticExit: false);

    private static ComparisonService MakeService(FakeHttpMessageHandler handler) =>
        new(
            new HttpClient(handler) { BaseAddress = new Uri("http://test/") },
            NullLogger<ComparisonService>.Instance);

    [Fact]
    public async Task CompareTradesAsync_DeserializesSnakeCaseResponse()
    {
        const string responseJson = """
        {
            "divergences": [
                {
                    "category": "FILL_PRICE_DRIFT",
                    "trade_number": 1,
                    "ms_utc": 1700000000000,
                    "message": "drift",
                    "left_fill_price": 100.0,
                    "right_fill_price": 100.1
                }
            ],
            "first_divergence_ms_utc": 1700000000000
        }
        """;

        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, responseJson);
        var svc = MakeService(handler);

        var request = new CompareTradesRequest(
            LeftTrades: new[] { MakeTrade(1, 1700000000000L, 1700000300000L, 100m, 101m) },
            RightTrades: new[] { MakeTrade(1, 1700000000000L, 1700000300000L, 100.1m, 101m) });

        var result = await svc.CompareTradesAsync(request, CancellationToken.None);

        Assert.NotNull(result);
        Assert.Single(result.Divergences);
        Assert.Equal("FILL_PRICE_DRIFT", result.Divergences[0].Category);
        Assert.Equal(100.0m, result.Divergences[0].LeftFillPrice);
        Assert.Equal(100.1m, result.Divergences[0].RightFillPrice);
        Assert.Equal(1700000000000L, result.FirstDivergenceMsUtc);
    }

    [Fact]
    public async Task CompareTradesAsync_SerializesRequestAsSnakeCase()
    {
        const string emptyResponse = """{"divergences":[],"first_divergence_ms_utc":null}""";
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, emptyResponse);
        var svc = MakeService(handler);

        var request = new CompareTradesRequest(
            LeftTrades: new[] { MakeTrade(1, 1700000000000L, 1700000300000L, 100m, 101m, 10m) with { IsSyntheticExit = true } },
            RightTrades: Array.Empty<PersistLeanTradePayload>());

        await svc.CompareTradesAsync(request, CancellationToken.None);

        Assert.NotNull(handler.LastRequestBody);
        using var doc = JsonDocument.Parse(handler.LastRequestBody!);
        var root = doc.RootElement;

        Assert.Equal(JsonValueKind.Array, root.GetProperty("left_trades").ValueKind);
        var firstTrade = root.GetProperty("left_trades")[0];
        Assert.Equal(1700000000000L, firstTrade.GetProperty("entry_ms_utc").GetInt64());
        Assert.True(firstTrade.GetProperty("is_synthetic_exit").GetBoolean());
        // fill_price_atol default must be present on the wire
        Assert.Equal(0.01, root.GetProperty("fill_price_atol").GetDouble(), precision: 6);
    }

    [Fact]
    public async Task CompareTradesAsync_HandlesEmptyDivergences()
    {
        var handler = new FakeHttpMessageHandler(
            HttpStatusCode.OK,
            """{"divergences":[],"first_divergence_ms_utc":null}""");
        var svc = MakeService(handler);

        var result = await svc.CompareTradesAsync(
            new CompareTradesRequest(
                LeftTrades: Array.Empty<PersistLeanTradePayload>(),
                RightTrades: Array.Empty<PersistLeanTradePayload>()),
            CancellationToken.None);

        Assert.Empty(result.Divergences);
        Assert.Null(result.FirstDivergenceMsUtc);
    }

    [Fact]
    public async Task CompareTradesAsync_ThrowsOnNon200()
    {
        var handler = new FakeHttpMessageHandler(
            HttpStatusCode.InternalServerError,
            "internal error");
        var svc = MakeService(handler);

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            svc.CompareTradesAsync(
                new CompareTradesRequest(
                    LeftTrades: Array.Empty<PersistLeanTradePayload>(),
                    RightTrades: Array.Empty<PersistLeanTradePayload>()),
                CancellationToken.None));
    }
}
