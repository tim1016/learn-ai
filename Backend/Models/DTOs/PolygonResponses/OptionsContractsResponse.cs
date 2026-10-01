namespace Backend.Models.DTOs.PolygonResponses;

public class OptionsExpirationsResponse
{
    public bool Success { get; set; }
    public List<string> Expirations { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}
