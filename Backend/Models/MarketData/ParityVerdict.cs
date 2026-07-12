using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

public class ParityVerdict
{
    public int Id { get; set; }

    public int LeftExecutionId { get; set; }
    public StrategyExecution LeftExecution { get; set; } = null!;

    public int RightExecutionId { get; set; }
    public StrategyExecution RightExecution { get; set; } = null!;

    [MaxLength(64)]
    [Column(TypeName = "varchar(64)")]
    public string? ParityGroupId { get; set; }

    public int VerdictVersion { get; set; }

    [Required]
    [MaxLength(16)]
    [Column(TypeName = "varchar(16)")]
    public string Status { get; set; } = "";

    [Required]
    [Column(TypeName = "jsonb")]
    public string VerdictJson { get; set; } = "{}";

    public DateTime CreatedAtUtc { get; set; } = DateTime.UtcNow;
}
