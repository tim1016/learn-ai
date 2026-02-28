using System.ComponentModel.DataAnnotations;

namespace Backend.Models.MarketData;

public class SignalExperiment
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

    // Graduation summary
    [MaxLength(10)]
    public string OverallGrade { get; set; } = "F";

    [MaxLength(50)]
    public string StatusLabel { get; set; } = "Exploratory";

    public bool OverallPassed { get; set; }

    // Walk-forward summary
    public decimal MeanOosSharpe { get; set; }
    public decimal BestThreshold { get; set; }
    public decimal BestCostBps { get; set; }

    // Settings
    public bool FlipSign { get; set; }
    public bool RegimeGateEnabled { get; set; }

    /// <summary>JSON-serialized full SignalEngineReportDto for detailed drill-down</summary>
    public string JsonReport { get; set; } = "{}";

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}
