using System.Text.Json;
using System.Text.Json.Nodes;
using Backend.Jobs;

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
}
