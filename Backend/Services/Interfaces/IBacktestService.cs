using Backend.Models.MarketData;

namespace Backend.Services.Interfaces;

public interface IBacktestService
{
    Task<StrategyExecution> RunBacktestAsync(
        int tickerId,
        string strategyName,
        string parametersJson,
        string startDate,
        string endDate,
        string timespan,
        int multiplier,
        List<StockAggregate> bars,
        CancellationToken cancellationToken = default);
}
