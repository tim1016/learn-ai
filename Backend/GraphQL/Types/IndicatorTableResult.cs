using System.Text.Json;
using HotChocolate;

namespace Backend.GraphQL.Types;

public class IndicatorTableResult
{
    public bool Success { get; set; }
    public required string Ticker { get; set; }
    public int RowCount { get; set; }
    public List<string> Columns { get; set; } = [];

    /// <summary>
    /// Each row is a JSON-serialized string of the indicator values.
    /// Frontend parses these to display the table.
    /// </summary>
    public List<string> Rows { get; set; } = [];
    public string? Error { get; set; }
}
