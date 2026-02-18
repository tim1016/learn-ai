using System.ComponentModel.DataAnnotations;

namespace Backend.Models.MarketData;

public class BacktestTrade
{
    public int Id { get; set; }

    public int StrategyExecutionId { get; set; }
    public StrategyExecution StrategyExecution { get; set; } = null!;

    [Required]
    [MaxLength(10)]
    public string TradeType { get; set; } = ""; // "Buy" or "Sell"

    public DateTime EntryTimestamp { get; set; }
    public DateTime ExitTimestamp { get; set; }

    public decimal EntryPrice { get; set; }
    public decimal ExitPrice { get; set; }
    public decimal Quantity { get; set; } = 1;

    public decimal PnL { get; set; }
    public decimal CumulativePnL { get; set; }

    [MaxLength(200)]
    public string SignalReason { get; set; } = "";
}
