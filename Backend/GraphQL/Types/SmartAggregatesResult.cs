using Backend.Models.MarketData;

namespace Backend.GraphQL.Types;

public class SmartAggregatesResult
{
    public required string Ticker { get; set; }
    public List<StockAggregate> Aggregates { get; set; } = [];
    public AggregatesSummary? Summary { get; set; }
}

public class AggregatesSummary
{
    public decimal PeriodHigh { get; set; }
    public decimal PeriodLow { get; set; }
    public decimal AverageVolume { get; set; }
    public decimal? AverageVwap { get; set; }
    public decimal OpenPrice { get; set; }
    public decimal ClosePrice { get; set; }
    public decimal PriceChange { get; set; }
    public decimal PriceChangePercent { get; set; }
    public int TotalBars { get; set; }
}
