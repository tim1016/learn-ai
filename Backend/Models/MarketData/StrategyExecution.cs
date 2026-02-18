using System.ComponentModel.DataAnnotations;

namespace Backend.Models.MarketData;

public class StrategyExecution
{
    public int Id { get; set; }

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    [Required]
    [MaxLength(100)]
    public string StrategyName { get; set; } = "";

    /// <summary>JSON string of strategy parameters (e.g. {"shortWindow":10,"longWindow":30})</summary>
    public string Parameters { get; set; } = "{}";

    [Required]
    [MaxLength(20)]
    public string StartDate { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string EndDate { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string Timespan { get; set; } = "minute";

    public int Multiplier { get; set; } = 1;

    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }

    public decimal TotalPnL { get; set; }
    public decimal MaxDrawdown { get; set; }
    public decimal SharpeRatio { get; set; }

    public DateTime ExecutedAt { get; set; } = DateTime.UtcNow;
    public long DurationMs { get; set; }

    public List<BacktestTrade> Trades { get; set; } = [];
}
