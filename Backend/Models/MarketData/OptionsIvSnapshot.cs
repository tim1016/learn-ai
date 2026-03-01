using System.ComponentModel.DataAnnotations;

namespace Backend.Models.MarketData;

public class OptionsIvSnapshot
{
    public long Id { get; set; }

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    public DateTime TradingDate { get; set; }

    public decimal? Iv30dAtm { get; set; }
    public decimal? Iv30dPut { get; set; }
    public decimal? Iv30dCall { get; set; }
    public decimal? StockClose { get; set; }

    public int? DteLow { get; set; }
    public int? DteHigh { get; set; }

    [MaxLength(20)]
    public string PriceSource { get; set; } = "";

    [MaxLength(20)]
    public string Source { get; set; } = "derived";

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}
