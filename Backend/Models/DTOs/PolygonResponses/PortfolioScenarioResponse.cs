namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Mirror of the Python `/api/portfolio/scenario` response shape from
/// `PythonDataService/app/models/portfolio.py`. Phase 2.1/2.2 of
/// `docs/architecture/numerical-authority-migration-plan.md`.
/// </summary>
public class PortfolioScenarioResponseDto
{
    public long AsOfMs { get; set; }
    public string Symbol { get; set; } = "";
    public decimal SpotPrice { get; set; }
    public decimal RiskFreeRate { get; set; }
    public decimal DividendYield { get; set; }
    public List<ScenarioPointDto> Points { get; set; } = [];
    public List<string> Warnings { get; set; } = [];
}

public class ScenarioPointDto
{
    public decimal SpotShock { get; set; }
    public decimal TimeShiftDays { get; set; }
    public decimal IvShift { get; set; }
    public decimal Spot { get; set; }
    public decimal PortfolioPnl { get; set; }
    public decimal AggregateDelta { get; set; }
    public decimal AggregateGamma { get; set; }
    public decimal AggregateTheta { get; set; }
    public decimal AggregateVega { get; set; }
    public decimal AggregateRho { get; set; }
    public List<LegGreeksDto> Legs { get; set; } = [];
}

public class LegGreeksDto
{
    public string? LegId { get; set; }
    public string Instrument { get; set; } = "";
    public decimal TheoreticalPrice { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
}

/// <summary>
/// Position request shape — mirror of the Python `Position` discriminated
/// union (StockPosition | OptionPosition). Set <see cref="Instrument"/> to
/// "stock" or "option"; the option-only fields are ignored for stock positions.
/// </summary>
public class PortfolioScenarioPositionDto
{
    public string Instrument { get; set; } = "stock";
    public string Symbol { get; set; } = "";
    public decimal Quantity { get; set; }
    public decimal EntryPrice { get; set; }
    public string? LegId { get; set; }

    // Option-only — null/ignored for stocks.
    public string? OptionType { get; set; }
    public decimal? Strike { get; set; }
    public long? ExpirationMs { get; set; }
    public decimal? Multiplier { get; set; }
    public decimal? CurrentIv { get; set; }
}

public class PortfolioScenarioGridDto
{
    public List<decimal> SpotShocks { get; set; } = [0m];
    public List<decimal> TimeShiftsDays { get; set; } = [0m];
    public List<decimal> IvShifts { get; set; } = [0m];
}
