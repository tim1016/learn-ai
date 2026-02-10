namespace Backend.GraphQL.Types;

/// <summary>
/// GraphQL mutation result for fetching aggregates
/// Testable: Simple POCO, easy to assert in tests
/// </summary>
public class FetchAggregatesResult
{
    public bool Success { get; set; }
    public required string Ticker { get; set; }
    public int Count { get; set; }
    public string? Message { get; set; }
    public int? OriginalCount { get; set; }
    public int? RemovedCount { get; set; }
}
