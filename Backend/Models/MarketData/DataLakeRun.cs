using System;
using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

/// <summary>
/// Audit row for a UI-initiated backtest run. Links to <see cref="StrategyExecution"/>
/// when the engine row materializes.
/// Schema authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.2
/// </summary>
public class DataLakeRun
{
    public Guid Id { get; set; }

    public int? StrategyExecutionId { get; set; }
    public StrategyExecution? StrategyExecution { get; set; }

    [MaxLength(128)]
    public string? EngineRunId { get; set; }

    [Required]
    [MaxLength(20)]
    public string RunType { get; set; } = "";

    [Required]
    [Column(TypeName = "jsonb")]
    public string RunSpec { get; set; } = "{}";

    public string? WorkspacePath { get; set; }

    [MaxLength(64)]
    public string? ManifestSha256 { get; set; }

    [MaxLength(64)]
    public string? DataAvailabilityHash { get; set; }

    [MaxLength(20)]
    public string? EnsureDataStatus { get; set; }

    [Column(TypeName = "jsonb")]
    public string? EnsureDataResponse { get; set; }

    [MaxLength(20)]
    public string? EngineStatus { get; set; }

    public long RequestedAtMs { get; set; }

    public long? StartedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }
}
