using System;
using Backend.Models.MarketData;
using Xunit;

namespace Backend.Tests.Unit.Models;

public class DataLakeRunTests
{
    [Fact]
    public void DataLakeRun_Defaults_AreSane()
    {
        var run = new DataLakeRun
        {
            Id = Guid.NewGuid(),
            RunType = "python_lab",
            RunSpec = "{}",
            RequestedAtMs = 1_700_000_000_000L,
        };

        Assert.Null(run.StrategyExecutionId);
        Assert.Null(run.EngineRunId);
        Assert.Null(run.StartedAtMs);
        Assert.Equal("python_lab", run.RunType);
    }
}
