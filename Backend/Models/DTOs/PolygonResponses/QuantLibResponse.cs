namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Response from the Python QuantLib pricing endpoint for a single option.
/// </summary>
public class QuantLibPriceResponse
{
    public bool Success { get; set; }
    public string Engine { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
    public decimal? D1 { get; set; }
    public decimal? D2 { get; set; }
    public string? Error { get; set; }
}

/// <summary>
/// Response from the Python QuantLib strategy pricing endpoint.
/// </summary>
public class QuantLibStrategyResponse
{
    public bool Success { get; set; }
    public string Engine { get; set; } = "";
    public decimal NetPrice { get; set; }
    public decimal NetDelta { get; set; }
    public decimal NetGamma { get; set; }
    public decimal NetTheta { get; set; }
    public decimal NetVega { get; set; }
    public decimal NetRho { get; set; }
    public List<QuantLibLegResult> Legs { get; set; } = [];
    public string? Error { get; set; }
}

public class QuantLibLegResult
{
    public string Engine { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
    public decimal? D1 { get; set; }
    public decimal? D2 { get; set; }
}

/// <summary>
/// Response from the Python QuantLib status endpoint.
/// </summary>
public class QuantLibStatusResponse
{
    public bool Available { get; set; }
    public string? Version { get; set; }
    public List<string> Engines { get; set; } = [];
}

// ------------------------------------------------------------------
// Pricing model comparison DTOs
// ------------------------------------------------------------------

public class PricingPointDto
{
    public decimal Spot { get; set; }
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
}

public class PricingModelCurveDto
{
    public string Model { get; set; } = "";
    public List<PricingPointDto> Points { get; set; } = [];
}

public class PricingCompareResponse
{
    public bool Success { get; set; }
    public decimal Strike { get; set; }
    public string OptionType { get; set; } = "";
    public string ExpirationDate { get; set; } = "";
    public decimal TimeToExpiryYears { get; set; }
    public List<PricingModelCurveDto> Models { get; set; } = [];
    public string? Error { get; set; }
}
