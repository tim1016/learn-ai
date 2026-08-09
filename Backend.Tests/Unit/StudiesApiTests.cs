using Backend;
using Backend.Models.MarketData;
using System.Text.Json;

namespace Backend.Tests.Unit;

public class StudiesApiTests
{
    [Theory]
    [InlineData("engine", "python", true)]
    [InlineData("engine", "both", true)]
    [InlineData("engine", "lean", false)]
    [InlineData("lean-sidecar", "lean", true)]
    [InlineData("lean-sidecar", "both", true)]
    [InlineData("lean-sidecar", "python", false)]
    public void RequestedEngineContract_ValidatesSourceAwarePairs(
        string source,
        string requestedEngine,
        bool expected)
    {
        Assert.Equal(
            expected,
            RequestedEngineContract.IsValidForSource(
                source,
                requestedEngine,
                allowLegacyNull: false));
    }

    [Fact]
    public void ToStudyDetailResponse_PreservesRequestedEngine()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            RequestedEngine = "both",
            ExecutedAt = DateTime.UtcNow,
        };

        var response = StudiesApi.ToStudyDetailResponse(execution);

        Assert.Equal("both", response.RequestedEngine);
    }

    [Fact]
    public void SaveStudyRequest_UnavailableRiskMetrics_DeserializesAsNull()
    {
        const string json = """
            {
              "sharpeRatio": null,
              "sortinoRatio": null,
              "profitFactor": null
            }
            """;
        var options = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        };

        var request = JsonSerializer.Deserialize<SaveStudyRequest>(json, options);

        Assert.NotNull(request);
        Assert.Null(request.SharpeRatio);
        Assert.Null(request.SortinoRatio);
        Assert.Null(request.ProfitFactor);
    }

    [Fact]
    public void SaveStudyRequest_DeserializesSyntheticExitReceipt()
    {
        const string json = """
            {
              "trades": [
                {
                  "entryTimestamp": 1748629800000,
                  "exitTimestamp": 1748629860000,
                  "isSyntheticExit": true
                }
              ]
            }
            """;
        var request = JsonSerializer.Deserialize<SaveStudyRequest>(json, new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        });

        Assert.NotNull(request);
        Assert.True(request.Trades![0].IsSyntheticExit);
    }

    [Fact]
    public void ValidateSaveStudyTradeTimestamps_MissingEntryTimestamp_ReturnsError()
    {
        var request = new SaveStudyRequest
        {
            Trades =
            [
                new SaveStudyTrade
                {
                    ExitTimestamp = 1_748_633_400_000,
                },
            ],
        };

        var error = StudiesApi.ValidateSaveStudyTradeTimestamps(request);

        Assert.Equal(
            "trades[0].entryTimestamp is required and must be a positive int64 ms UTC timestamp.",
            error);
    }

    [Fact]
    public void ValidateSaveStudyTradeTimestamps_ZeroExitTimestamp_ReturnsError()
    {
        var request = new SaveStudyRequest
        {
            Trades =
            [
                new SaveStudyTrade
                {
                    EntryTimestamp = 1_748_629_800_000,
                    ExitTimestamp = 0,
                },
            ],
        };

        var error = StudiesApi.ValidateSaveStudyTradeTimestamps(request);

        Assert.Equal(
            "trades[0].exitTimestamp is required and must be a positive int64 ms UTC timestamp.",
            error);
    }

    [Fact]
    public void ValidateSaveStudyTradeTimestamps_ValidTrade_ReturnsNull()
    {
        var request = new SaveStudyRequest
        {
            Trades =
            [
                new SaveStudyTrade
                {
                    EntryTimestamp = 1_748_629_800_000,
                    ExitTimestamp = 1_748_633_400_000,
                },
            ],
        };

        var error = StudiesApi.ValidateSaveStudyTradeTimestamps(request);

        Assert.Null(error);
    }
}
