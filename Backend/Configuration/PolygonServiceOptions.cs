namespace Backend.Configuration;

/// <summary>
/// Configuration for Python Polygon service
/// Bound from appsettings.json via IOptions pattern (testable)
/// </summary>
public class PolygonServiceOptions
{
    public const string SectionName = "PolygonService";

    public string BaseUrl { get; set; } = "http://localhost:8000";
    public int TimeoutSeconds { get; set; } = 120;
    public int MaxRetries { get; set; } = 3;
}
