using System.ComponentModel.DataAnnotations;

namespace Backend.Models.Portfolio;

public class Account
{
    public Guid Id { get; set; }

    [Required]
    [MaxLength(200)]
    public string Name { get; set; } = "";

    public AccountType Type { get; set; }

    [MaxLength(10)]
    public string BaseCurrency { get; set; } = "USD";

    public decimal InitialCash { get; set; }

    public decimal Cash { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    // Navigation properties
    public List<Order> Orders { get; set; } = [];
    public List<PortfolioTrade> Trades { get; set; } = [];
    public List<Position> Positions { get; set; } = [];
}
