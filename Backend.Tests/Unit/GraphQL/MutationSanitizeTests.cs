using Backend.GraphQL;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class MutationSanitizeTests
{
    private readonly Mock<ISanitizationService> _sanitizationMock = new();

    #region SanitizeMarketData

    [Fact]
    public async Task SanitizeMarketData_Success_ReturnsCounts()
    {
        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1_000_000m, 1704067200000),
            new("AAPL", 153m, 158m, 151m, 157m, 900_000m, 1704153600000),
        };

        _sanitizationMock.Setup(s => s.SanitizeAsync(input, 0.99, default))
            .ReturnsAsync([input[0]]); // Simulates one record removed

        var mutation = new Mutation();
        var result = await mutation.SanitizeMarketData(_sanitizationMock.Object, input);

        Assert.True(result.Success);
        Assert.Equal(2, result.OriginalCount);
        Assert.Equal(1, result.CleanedCount);
        Assert.Contains("2 records", result.Message);
    }

    [Fact]
    public async Task SanitizeMarketData_ServiceThrows_ReturnsErrorResult()
    {
        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1_000_000m, 1704067200000),
        };

        _sanitizationMock.Setup(s => s.SanitizeAsync(
                It.IsAny<List<MarketDataRecord>>(), It.IsAny<double>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Service unavailable"));

        var mutation = new Mutation();
        var result = await mutation.SanitizeMarketData(_sanitizationMock.Object, input);

        Assert.False(result.Success);
        Assert.Equal(0, result.CleanedCount);
        Assert.Contains("Service unavailable", result.Message);
    }

    #endregion

}
