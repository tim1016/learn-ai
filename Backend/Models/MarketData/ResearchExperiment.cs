using System.ComponentModel.DataAnnotations;

namespace Backend.Models.MarketData;

public class ResearchExperiment
{
    public int Id { get; set; }

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    [Required]
    [MaxLength(100)]
    public string FeatureName { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string StartDate { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string EndDate { get; set; } = "";

    public int BarsUsed { get; set; }

    public decimal MeanIC { get; set; }
    public decimal ICTStat { get; set; }
    public decimal ICPValue { get; set; }
    public decimal AdfPValue { get; set; }
    public decimal KpssPValue { get; set; }
    public bool IsStationary { get; set; }
    public bool PassedValidation { get; set; }

    public decimal MonotonicityRatio { get; set; }
    public bool IsMonotonic { get; set; }

    /// <summary>JSON-serialized full research report for detailed drill-down</summary>
    public string JsonReport { get; set; } = "{}";

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}
