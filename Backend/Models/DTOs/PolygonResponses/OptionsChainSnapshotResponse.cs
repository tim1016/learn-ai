namespace Backend.Models.DTOs.PolygonResponses;

public class OptionsChainSnapshotResponse
{
    public bool Success { get; set; }
    public UnderlyingSnapshotDto? Underlying { get; set; }
    public List<OptionsContractSnapshotDto> Contracts { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class UnderlyingSnapshotDto
{
    public string Ticker { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Change { get; set; }
    public decimal ChangePercent { get; set; }
}

public class OptionsContractSnapshotDto
{
    public string? Ticker { get; set; }
    public string? ContractType { get; set; }
    public decimal? StrikePrice { get; set; }
    public string? ExpirationDate { get; set; }
    public decimal? BreakEvenPrice { get; set; }
    public decimal? ImpliedVolatility { get; set; }
    public decimal? OpenInterest { get; set; }
    public GreeksSnapshotDto? Greeks { get; set; }
    public DaySnapshotDto? Day { get; set; }
    public LastTradeSnapshotDto? LastTrade { get; set; }
    public LastQuoteSnapshotDto? LastQuote { get; set; }
}

public class GreeksSnapshotDto
{
    public decimal? Delta { get; set; }
    public decimal? Gamma { get; set; }
    public decimal? Theta { get; set; }
    public decimal? Vega { get; set; }
}

public class DaySnapshotDto
{
    public decimal? Open { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? Close { get; set; }
    public decimal? Volume { get; set; }
    public decimal? Vwap { get; set; }
}

public class LastTradeSnapshotDto
{
    public decimal? Price { get; set; }
    public decimal? Size { get; set; }
    public int? Exchange { get; set; }
    public string? Timeframe { get; set; }
}

public class LastQuoteSnapshotDto
{
    public decimal? Bid { get; set; }
    public decimal? Ask { get; set; }
    public decimal? BidSize { get; set; }
    public decimal? AskSize { get; set; }
    public decimal? Midpoint { get; set; }
    public string? Timeframe { get; set; }
}
