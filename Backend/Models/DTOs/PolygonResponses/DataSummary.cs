namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Summary statistics from Python data sanitization
/// </summary>
public class DataSummary
{
    public int OriginalCount { get; set; }
    public int CleanedCount { get; set; }
    public int RemovedCount { get; set; }
    public decimal? RemovalPercentage { get; set; }
}
