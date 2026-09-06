using System.Text.Json;
using Backend.Models.DTOs.PolygonResponses;

namespace Backend.Tests.Contract;

public class CrossStackContractFixtureTests
{
    private static readonly JsonSerializerOptions SnakeCaseJson = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    [Fact]
    public void AggregateResponseFixture_DeserializesPythonPayloadWithInt64Timestamp()
    {
        var response = JsonSerializer.Deserialize<AggregateResponse>(
            ReadFixture("aggregate-response-v1.json"), SnakeCaseJson);

        Assert.NotNull(response);
        Assert.True(response.Success);
        var bar = Assert.Single(response.Data);
        Assert.Equal(1_704_153_600_000L, bar.Timestamp);
        Assert.Equal(471.75m, bar.Close);
        Assert.NotNull(response.Summary);
        Assert.Equal(0m, response.Summary.RemovalPercentage);
    }

    private static string ReadFixture(string name) => File.ReadAllText(
        Path.Combine(AppContext.BaseDirectory, "contracts", "fixtures", name));
}
