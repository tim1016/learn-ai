using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

/// <summary>
/// Catalog row for a single physical artifact in the Polygon → LEAN data lake.
/// Written by Python <c>app/data_lake/catalog_client.py</c> via asyncpg; read
/// by both Backend (for coverage queries) and Python.
/// Schema authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.1
/// </summary>
public class DataLakeArtifact
{
    public long Id { get; set; }

    [Required]
    [MaxLength(40)]
    public string ArtifactKind { get; set; } = "";

    [MaxLength(20)]
    public string? Market { get; set; }

    [MaxLength(20)]
    public string? Symbol { get; set; }

    public DateOnly? TradingDate { get; set; }

    [MaxLength(20)]
    public string? Resolution { get; set; }

    [MaxLength(20)]
    public string? DataType { get; set; }

    [Required]
    [MaxLength(40)]
    public string Provider { get; set; } = "";

    [Required]
    [Column(TypeName = "jsonb")]
    public string ProviderParams { get; set; } = "{}";

    [MaxLength(40)]
    public string? PriceAdjustmentMode { get; set; }

    [Required]
    [MaxLength(64)]
    public string DataContractHash { get; set; } = "";

    public int? RowCount { get; set; }

    public long? FirstBarStartMs { get; set; }

    public long? LastBarStartMs { get; set; }

    [MaxLength(64)]
    public string? CorpActionRevision { get; set; }

    [Required]
    public string FilePath { get; set; } = "";

    public long? FileSizeBytes { get; set; }

    [MaxLength(64)]
    public string? FileSha256 { get; set; }

    [Required]
    [MaxLength(20)]
    public string Status { get; set; } = "fetching";

    [MaxLength(128)]
    public string? LeaseOwner { get; set; }

    public long? LeaseExpiresAtMs { get; set; }

    public int AttemptCount { get; set; } = 0;

    public string? LastError { get; set; }

    public string? ErrorMessage { get; set; }

    public long FetchedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }
}
