namespace Backend.GraphQL.Types;

/// <summary>
/// GraphQL mutation result for fetching trades
/// </summary>
public class FetchTradesResult
{
    public bool Success { get; set; }
    public required string Ticker { get; set; }
    public int Count { get; set; }
    public string? Message { get; set; }
}
