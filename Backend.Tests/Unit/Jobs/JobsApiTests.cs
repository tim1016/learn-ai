using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Nodes;
using Backend.Jobs;
using Microsoft.Extensions.Configuration;

namespace Backend.Tests.Unit.Jobs;

public class JobsApiTests
{
    [Fact]
    public void SerializeParametersForPersistence_RedactsLeanSourceWithoutMutatingDispatchPayload()
    {
        const string source = "from AlgorithmImports import *\nclass SecretAlgorithm(QCAlgorithm):\n    pass";
        var dispatchPayload = new JsonObject
        {
            ["request"] = new JsonObject
            {
                ["strategy_name"] = "ema_crossover_signal",
                ["algorithm_source"] = source,
            },
        };

        var persistedJson = JobsApi.SerializeParametersForPersistence(dispatchPayload);

        using var persisted = JsonDocument.Parse(persistedJson);
        var persistedRequest = persisted.RootElement.GetProperty("request");
        Assert.False(persistedRequest.TryGetProperty("algorithm_source", out _));
        Assert.Equal("ema_crossover_signal", persistedRequest.GetProperty("strategy_name").GetString());
        Assert.Equal(source, dispatchPayload["request"]!["algorithm_source"]!.GetValue<string>());
    }

    private static IConfiguration BuildConfig(string? controlSecret) =>
        new ConfigurationBuilder()
            .AddInMemoryCollection(
                controlSecret is null
                    ? new Dictionary<string, string?>()
                    : new Dictionary<string, string?> { ["DataPlane:ControlSecret"] = controlSecret }
            )
            .Build();

    [Fact]
    public void ResolveControlSecretHeader_DataLakeBackfillWithSecretConfigured_ReturnsTheSecret()
    {
        var config = BuildConfig("shh-its-a-secret");

        var header = JobsApi.ResolveControlSecretHeader("data_lake_backfill", config);

        Assert.Equal("shh-its-a-secret", header);
    }

    [Fact]
    public void ResolveControlSecretHeader_DataLakeBackfillWithNoSecretConfigured_ReturnsNull()
    {
        var config = BuildConfig(null);

        var header = JobsApi.ResolveControlSecretHeader("data_lake_backfill", config);

        Assert.Null(header);
    }

    [Theory]
    [InlineData("backtest")]
    [InlineData("engine_backtest")]
    [InlineData("recency_chart")]
    public void ResolveControlSecretHeader_UnprotectedJobType_ReturnsNullEvenWithASecretConfigured(string type)
    {
        var config = BuildConfig("shh-its-a-secret");

        var header = JobsApi.ResolveControlSecretHeader(type, config);

        Assert.Null(header);
    }
}
