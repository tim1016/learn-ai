namespace Backend.Models.DTOs.PolygonResponses;

public class StrategyAnalyzeResponseDto
{
    public bool Success { get; set; }
    public string Symbol { get; set; } = "";
    public decimal SpotPrice { get; set; }
    public decimal StrategyCost { get; set; }
    public decimal Pop { get; set; }
    public decimal ExpectedValue { get; set; }
    public decimal MaxProfit { get; set; }
    public decimal MaxLoss { get; set; }
    public List<decimal> Breakevens { get; set; } = [];
    public List<PayoffPointDto> Curve { get; set; } = [];
    public GreeksDto Greeks { get; set; } = new();
    public string? Error { get; set; }
}

public class PayoffPointDto
{
    public decimal Price { get; set; }
    public decimal Pnl { get; set; }
}

public class GreeksDto
{
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
}
