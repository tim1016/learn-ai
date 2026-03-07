using System.ComponentModel.DataAnnotations;
using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class OptionContract
{
    public Guid Id { get; set; }

    public int UnderlyingTickerId { get; set; }
    public Ticker UnderlyingTicker { get; set; } = null!;

    [Required]
    [MaxLength(100)]
    public string Symbol { get; set; } = "";

    public decimal Strike { get; set; }
    public DateOnly Expiration { get; set; }
    public OptionType OptionType { get; set; }
    public int Multiplier { get; set; } = 100;
}
