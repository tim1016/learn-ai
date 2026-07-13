using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

public class ParityVerdict
{
    public int Id { get; set; }

    public int LeftExecutionId { get; set; }
    public StrategyExecution LeftExecution { get; set; } = null!;

    /// <summary>
    /// The LEAN companion run. Null while the verdict is pending (the
    /// companion hasn't landed) and on unavailable/failed dispositions.
    /// </summary>
    public int? RightExecutionId { get; set; }
    public StrategyExecution? RightExecution { get; set; }

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

    // DateTime column (no migration needed). Use DateTimeOffset.UtcNow.UtcDateTime to
    // guarantee Kind=Utc; DateTime.UtcNow can return Kind=Local on some hosts (temporal-rigor.md).
    public DateTime CreatedAtUtc { get; set; } = DateTimeOffset.UtcNow.UtcDateTime;
}
