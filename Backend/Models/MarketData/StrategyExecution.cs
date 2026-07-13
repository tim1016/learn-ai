using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Backend.Models.MarketData;

public class StrategyExecution
{
    public int Id { get; set; }

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    [Required]
    [MaxLength(100)]
    public string StrategyName { get; set; } = "";

    /// <summary>JSON string of strategy parameters (e.g. {"shortWindow":10,"longWindow":30})</summary>
    public string Parameters { get; set; } = "{}";

    [Required]
    [MaxLength(20)]
    public string StartDate { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string EndDate { get; set; } = "";

    [Required]
    [MaxLength(20)]
    public string Timespan { get; set; } = "minute";

    public int Multiplier { get; set; } = 1;

    // ── Core metrics (original) ──
    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }

    public decimal TotalPnL { get; set; }
    public decimal MaxDrawdown { get; set; }
    public decimal SharpeRatio { get; set; }

    // ── LEAN-parity KPIs ──
    public decimal InitialCash { get; set; }
    public decimal FinalEquity { get; set; }
    public decimal TotalFees { get; set; }
    public decimal WinRate { get; set; }
    public decimal CompoundingAnnualReturn { get; set; }
    public decimal SortinoRatio { get; set; }
    public decimal ProbabilisticSharpeRatio { get; set; }
    public decimal ProfitFactor { get; set; }
    public decimal Alpha { get; set; }
    public decimal Beta { get; set; }
    public decimal InformationRatio { get; set; }
    public decimal TrackingError { get; set; }
    public decimal TreynorRatio { get; set; }
    public decimal ValueAtRisk95 { get; set; }
    public decimal ValueAtRisk99 { get; set; }
    public decimal AnnualStandardDeviation { get; set; }
    public int DrawdownRecoveryDays { get; set; }

    /// <summary>Full LEAN statistics suite stored as JSONB for ad-hoc queries.</summary>
    [Column(TypeName = "jsonb")]
    public string? LeanStatisticsJson { get; set; }

    /// <summary>Frozen backend-authored run verdict JSONB. Null for pre-versioning rows.</summary>
    [Column(TypeName = "jsonb")]
    public string? RunVerdictJson { get; set; }

    public int? VerdictVersion { get; set; }

    [MaxLength(4)]
    [Column(TypeName = "varchar(4)")]
    public string? VerdictGrade { get; set; }

    [MaxLength(16)]
    [Column(TypeName = "varchar(16)")]
    public string? VerdictSignal { get; set; }

    [Column(TypeName = "jsonb")]
    public string? EquityCurveJson { get; set; }

    [Column(TypeName = "jsonb")]
    public string? InsightSummaryJson { get; set; }

    /// <summary>
    /// Frozen validation-analytics envelope (horizons, timing heatmap,
    /// seasonality, rolling stability) computed at run completion by the
    /// producing engine. Never recomputed server-side. Null for rows that
    /// predate the envelope or whose run rejected analytics computation.
    /// </summary>
    [Column(TypeName = "jsonb")]
    public string? ValidationAnalyticsJson { get; set; }

    // ── Metadata ──
    /// <summary>Origin: "engine" or "strategy-lab".</summary>
    [MaxLength(20)]
    public string Source { get; set; } = "engine";

    [MaxLength(128)]
    public string? LeanRunId { get; set; }

    /// <summary>Free-text notes field for researcher observations.</summary>
    public string? Notes { get; set; }

    [MaxLength(20)]
    public string FillMode { get; set; } = "signal_bar_close";

    /// <summary>
    /// PR B (2026-05-19) — canonical DataPolicy block, serialized to JSON.
    /// Nullable: legacy rows predate the column. New rows are written by
    /// both engine and lean-sidecar paths.
    /// </summary>
    [Column(TypeName = "jsonb")]
    public string? DataPolicyJson { get; set; }

    /// <summary>
    /// PR B (2026-05-19) — commission per order, recorded so the compare
    /// view can gate on it. Nullable for legacy rows. Defaults to 0 when a
    /// new row's payload omits the field (LEAN-zero-commission templates).
    /// </summary>
    [Column(TypeName = "numeric(18,8)")]
    public decimal? CommissionPerOrder { get; set; }

    /// <summary>
    /// PR B (2026-05-19) — brokerage policy. Python engine writes
    /// "algorithm_default" because it doesn't model brokerage; LEAN writes
    /// whatever the manifest records. Nullable for legacy rows.
    /// </summary>
    [MaxLength(40)]
    [Column(TypeName = "varchar(40)")]
    public string? BrokeragePolicy { get; set; }

    [MaxLength(64)]
    [Column(TypeName = "varchar(64)")]
    public string? ParityGroupId { get; set; }

    public DateTime ExecutedAt { get; set; } = DateTime.UtcNow;
    public long DurationMs { get; set; }

    public List<BacktestTrade> Trades { get; set; } = [];
}
