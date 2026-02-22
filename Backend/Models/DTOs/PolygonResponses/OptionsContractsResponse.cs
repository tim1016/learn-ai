namespace Backend.Models.DTOs.PolygonResponses;

public class OptionsContractsResponse
{
    public bool Success { get; set; }
    public List<OptionsContractDto> Contracts { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class OptionsExpirationsResponse
{
    public bool Success { get; set; }
    public List<string> Expirations { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class OptionsContractDto
{
    public required string Ticker { get; set; }
    public string? UnderlyingTicker { get; set; }
    public string? ContractType { get; set; }
    public decimal? StrikePrice { get; set; }
    public string? ExpirationDate { get; set; }
    public string? ExerciseStyle { get; set; }
    public decimal? SharesPerContract { get; set; }
    public string? PrimaryExchange { get; set; }
}
