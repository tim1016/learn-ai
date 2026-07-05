using Backend;

namespace Backend.Tests.Unit;

public class StudiesApiTests
{
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
