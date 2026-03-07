namespace Backend.Models.Portfolio;

public class OptionLeg
{
    public Guid Id { get; set; }

    public Guid TradeId { get; set; }
    public PortfolioTrade Trade { get; set; } = null!;

    public Guid OptionContractId { get; set; }
    public OptionContract OptionContract { get; set; } = null!;

    public decimal Quantity { get; set; }
    public decimal? EntryIV { get; set; }
    public decimal? EntryDelta { get; set; }
    public decimal? EntryGamma { get; set; }
    public decimal? EntryTheta { get; set; }
    public decimal? EntryVega { get; set; }
}
