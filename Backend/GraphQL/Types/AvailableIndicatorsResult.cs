using HotChocolate;

namespace Backend.GraphQL.Types;

public class IndicatorInfoItem
{
    public required string Name { get; set; }
    public required string Category { get; set; }
    public required string Description { get; set; }
}

public class IndicatorCategory
{
    public required string Name { get; set; }
    public List<IndicatorInfoItem> Indicators { get; set; } = [];
}

public class AvailableIndicatorsResult
{
    public bool Success { get; set; }
    public List<IndicatorCategory> Categories { get; set; } = [];
    public int Total { get; set; }
    public string? Error { get; set; }
}
