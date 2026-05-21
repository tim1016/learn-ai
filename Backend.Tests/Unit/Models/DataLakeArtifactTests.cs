using Backend.Models.MarketData;
using Xunit;

namespace Backend.Tests.Unit.Models;

public class DataLakeArtifactTests
{
    [Fact]
    public void DataLakeArtifact_Defaults_AreSane()
    {
        var artifact = new DataLakeArtifact
        {
            ArtifactKind = "time_series_bars",
            Symbol = "SPY",
            Market = "usa",
            Resolution = "minute",
            DataType = "trade",
            Provider = "polygon",
            ProviderParams = "{}",
            PriceAdjustmentMode = "raw",
            DataContractHash = new string('a', 64),
            FilePath = "equity/usa/minute/spy/20240520_trade.zip",
            Status = "fetching",
            FetchedAtMs = 1_700_000_000_000L,
        };

        Assert.Equal(0, artifact.AttemptCount);
        Assert.Null(artifact.CompletedAtMs);
        Assert.Equal("time_series_bars", artifact.ArtifactKind);
    }
}
