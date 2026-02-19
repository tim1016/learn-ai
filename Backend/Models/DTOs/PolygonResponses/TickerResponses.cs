namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Basic ticker info from Polygon reference API
/// </summary>
public class TickerInfoDto
{
    public string Ticker { get; set; } = "";
    public string Name { get; set; } = "";
    public string Market { get; set; } = "";
    public string Type { get; set; } = "";
    public bool Active { get; set; } = true;
    public string? PrimaryExchange { get; set; }
    public string? CurrencyName { get; set; }
}

/// <summary>
/// Response for batch ticker list from Python service
/// </summary>
public class TickerListResponse
{
    public bool Success { get; set; }
    public List<TickerInfoDto> Tickers { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

/// <summary>
/// Address from ticker details
/// </summary>
public class TickerAddressDto
{
    public string? Address1 { get; set; }
    public string? City { get; set; }
    public string? State { get; set; }
    public string? PostalCode { get; set; }
}

/// <summary>
/// Detailed ticker overview from Polygon reference API
/// </summary>
public class TickerDetailResponse
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string Name { get; set; } = "";
    public string? Description { get; set; }
    public double? MarketCap { get; set; }
    public string? HomepageUrl { get; set; }
    public int? TotalEmployees { get; set; }
    public string? ListDate { get; set; }
    public string? SicDescription { get; set; }
    public string? PrimaryExchange { get; set; }
    public string? Type { get; set; }
    public double? WeightedSharesOutstanding { get; set; }
    public TickerAddressDto? Address { get; set; }
    public string? Error { get; set; }
}

/// <summary>
/// Response for related companies lookup from Python service
/// </summary>
public class RelatedTickersResponse
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public List<string> Related { get; set; } = [];
    public string? Error { get; set; }
}
