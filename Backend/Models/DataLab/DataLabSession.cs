using System.ComponentModel.DataAnnotations;

namespace Backend.Models.DataLab;

public class DataLabSession
{
    public Guid Id { get; set; }

    [Required]
    [MaxLength(300)]
    public string Name { get; set; } = "";

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;

    // ── Configuration (stored as individual columns for filtering) ──

    [Required]
    [MaxLength(20)]
    public string Ticker { get; set; } = "";

    [Required]
    [MaxLength(10)]
    public string FromDate { get; set; } = "";

    [Required]
    [MaxLength(10)]
    public string ToDate { get; set; } = "";

    [Required]
    [MaxLength(10)]
    public string Session { get; set; } = "rth";

    public bool ForwardFill { get; set; } = true;

    public bool Adjusted { get; set; } = true;

    /// <summary>
    /// Indicator entries stored as JSON array.
    /// Each element: { "name": "ema", "params": { "length": 20 } }
    /// </summary>
    [Required]
    public string EntriesJson { get; set; } = "[]";

    // ── Chart snapshot (stored as JSONB for efficient Postgres storage) ──

    /// <summary>
    /// Full chart data snapshot: bars, indicators, quality, timeframe info.
    /// Null when the session was saved without fetching chart data first.
    /// Stored as JSONB in Postgres for efficient read/write of large payloads.
    /// </summary>
    public string? ChartSnapshotJson { get; set; }
}
