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

    // Phase 1.1 opt-in extensions. Null when the corresponding include_*
    // flag was not set on the Python request.
    public List<CurrentCurvePointDto>? CurrentCurve { get; set; }
    public List<GreekCurvePointDto>? GreekCurves { get; set; }
    public List<LegDiagnosticDto>? LegDiagnostics { get; set; }
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

public class CurrentCurvePointDto
{
    public decimal Price { get; set; }
    public decimal TheoreticalValue { get; set; }
    public decimal TheoreticalPnl { get; set; }
}

public class GreekCurvePointDto
{
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
}

public class LegDiagnosticDto
{
    public string? LegId { get; set; }
    public decimal Strike { get; set; }
    public string OptionType { get; set; } = "";
    public string Position { get; set; } = "";
    public int Quantity { get; set; }
    public decimal Iv { get; set; }
    public decimal EntryPremium { get; set; }
    public decimal CurrentTheoretical { get; set; }
    public decimal CurrentDelta { get; set; }
    public decimal CurrentGamma { get; set; }
    public decimal CurrentTheta { get; set; }
    public decimal CurrentVega { get; set; }
}
