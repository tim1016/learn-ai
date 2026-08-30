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

    /// <summary>
    /// Which physical lake root this row belongs to (issue #1876, PR A of
    /// #1861). Additive: backfilled to the deterministic legacy-root UUID
    /// (<c>Guid.Empty</c>) for every pre-#1876 row by migration
    /// AddDataRootIdToDataLakeArtifactsAndRuns. Issue #1878 (PR B) dropped
    /// the temporary server default and rebuilt catalog uniqueness to lead
    /// with this column — every write now supplies it explicitly (the
    /// service's configured active root, or — for
    /// <c>app.data_lake.cache_import</c> — the selected root's own marker
    /// UUID). Not an FK — no DataLakeRoots table exists yet (explicit
    /// non-goal of this slice); the on-disk marker at
    /// <c>&lt;base-root&gt;/lake/.data-root.json</c> is the source of truth
    /// for what a root's identity actually is.
    /// </summary>
    public Guid DataRootId { get; set; }

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

    /// <summary>
    /// Monotonic fencing generation for this artifact's lease (issue #1888,
    /// ADR 0048's fencing idiom applied to this subsystem). Starts at 1 on
    /// the row's INSERT and is incremented by exactly 1 on every reclaim
    /// (a lease-expiry steal, a retry of a failed row, or a rebuild's
    /// complete-to-fetching transition). Python's write path
    /// (<c>app/data_lake/catalog_client.py</c>) gates both the completion
    /// UPDATE and the pre-promotion authorization check on this value
    /// matching the durable row, not on the caller's own recollection of
    /// still holding the lease -- see that module's write-ops section for
    /// why a status-only check was insufficient.
    /// </summary>
    public int LeaseGeneration { get; set; } = 1;

    public int AttemptCount { get; set; } = 0;

    public string? LastError { get; set; }

    public string? ErrorMessage { get; set; }

    public long FetchedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }
}
