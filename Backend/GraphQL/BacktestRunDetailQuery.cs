using System.Text.Json;
using System.Globalization;
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.MarketData;
using Backend.Temporal;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.GraphQL;

[ExtendObjectType<Query>]
public class BacktestRunDetailQuery
{
    // Run totals remain authoritative on StrategyExecution; this bounds only
    // the automatic chart-marker and ledger evidence carried by the report.
    private const int ReportTradeLimit = 500;

    [GraphQLName("backtestRun")]
    public async Task<BacktestRunDetailType?> GetBacktestRun(
        int id,
        [Service] AppDbContext context,
        [Service] ILogger<BacktestRunDetailQuery> logger,
        CancellationToken ct)
    {
        var execution = await context.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Ticker)
            .FirstOrDefaultAsync(e => e.Id == id, ct);

        if (execution is null)
            return null;

        var recentTrades = await context.BacktestTrades
            .AsNoTracking()
            .Where(t => t.StrategyExecutionId == id)
            .OrderByDescending(t => t.EntryTimestamp)
            .ThenByDescending(t => t.Id)
            .Take(ReportTradeLimit + 1)
            .ToListAsync(ct);
        var tradesTruncated = recentTrades.Count > ReportTradeLimit;
        if (tradesTruncated)
            recentTrades.RemoveAt(recentTrades.Count - 1);
        recentTrades.Reverse();

        var parityVerdicts = await context.ParityVerdicts
            .AsNoTracking()
            .Where(p => p.LeftExecutionId == id || p.RightExecutionId == id)
            .Select(p => new BacktestRunParityVerdictType
            {
                Id = p.Id,
                LeftExecutionId = p.LeftExecutionId,
                RightExecutionId = p.RightExecutionId,
                ParityGroupId = p.ParityGroupId,
                VerdictVersion = p.VerdictVersion,
                Status = p.Status,
                VerdictJson = p.VerdictJson,
                CreatedAt = UnixMs.FromUtc(p.CreatedAtUtc),
            })
            .ToListAsync(ct);

        return BacktestRunDetailType.FromExecution(
            execution,
            parityVerdicts,
            logger,
            recentTrades,
            tradesTruncated);
    }
}

public sealed record BacktestRunDetailType
{
    public int Id { get; init; }
    public Engine Engine { get; init; }
    public string Source { get; init; } = "";
    public string? RequestedEngine { get; init; }
    public string StrategyName { get; init; } = "";
    public string Symbol { get; init; } = "";
    public string? LeanRunId { get; init; }
    public string? Parameters { get; init; }
    public string StartDate { get; init; } = "";
    public string EndDate { get; init; } = "";
    public string FillMode { get; init; } = "";
    [GraphQLIgnore]
    public DateTime ExecutedAtUtc { get; init; }
    public long ExecutedAt => UnixMs.FromUtc(ExecutedAtUtc);
    public long DurationMs { get; init; }
    public int TotalTrades { get; init; }
    public int WinningTrades { get; init; }
    public int LosingTrades { get; init; }
    public decimal WinRate { get; init; }
    [GraphQLName("totalPnL")]
    public decimal TotalPnL { get; init; }
    public decimal InitialCash { get; init; }
    public decimal FinalEquity { get; init; }
    public decimal TotalFees { get; init; }
    public decimal MaxDrawdown { get; init; }
    public decimal? SharpeRatio { get; init; }
    public decimal? SortinoRatio { get; init; }
    public decimal? ProfitFactor { get; init; }
    public string? LeanStatisticsJson { get; init; }
    [GraphQLName("leanAnalysisJson")]
    public string? LeanAnalysisJson { get; init; }
    public string? VerdictJson { get; init; }
    public int? VerdictVersion { get; init; }
    public string? VerdictGrade { get; init; }
    public string? VerdictSignal { get; init; }
    public BacktestRunEquityCurvesType? EquityCurve { get; init; }
    public BacktestRunValidationAnalyticsType? ValidationAnalytics { get; init; }
    public IReadOnlyList<MetricDocumentationContextType> MetricDocumentation { get; init; } = [];
    public string? InsightSummaryJson { get; init; }
    public string? DataPolicyJson { get; init; }
    public DataPolicyType? DataPolicy => DataPolicyType.TryParse(DataPolicyJson);
    public decimal? CommissionPerOrder { get; init; }
    public string? ParityGroupId { get; init; }
    public IReadOnlyList<BacktestRunTradeDetailType> Trades { get; init; } = [];
    public bool TradesTruncated { get; init; }
    public IReadOnlyList<BacktestRunParityVerdictType> ParityVerdicts { get; init; } = [];

    public static BacktestRunDetailType FromExecution(
        StrategyExecution execution,
        IReadOnlyList<BacktestRunParityVerdictType> parityVerdicts,
        ILogger logger,
        IReadOnlyList<BacktestTrade>? trades = null,
        bool tradesTruncated = false)
    {
        var leanKpis = ParseLeanKpis(execution, logger);
        return new BacktestRunDetailType
        {
            Id = execution.Id,
            Engine = EngineExtensions.FromSource(execution.Source),
            Source = execution.Source,
            RequestedEngine = execution.RequestedEngine,
            StrategyName = execution.StrategyName,
            Symbol = execution.Ticker.Symbol,
            LeanRunId = execution.LeanRunId,
            Parameters = execution.Parameters,
            StartDate = execution.StartDate,
            EndDate = execution.EndDate,
            FillMode = execution.FillMode,
            ExecutedAtUtc = execution.ExecutedAt,
            DurationMs = execution.DurationMs,
            TotalTrades = execution.TotalTrades,
            WinningTrades = execution.WinningTrades,
            LosingTrades = execution.LosingTrades,
            WinRate = execution.WinRate,
            TotalPnL = execution.TotalPnL,
            InitialCash = execution.InitialCash,
            FinalEquity = execution.FinalEquity,
            TotalFees = execution.TotalFees,
            MaxDrawdown = leanKpis?.MaxDrawdown ?? execution.MaxDrawdown,
            SharpeRatio = leanKpis?.SharpeRatio ?? execution.SharpeRatio,
            SortinoRatio = leanKpis?.SortinoRatio ?? execution.SortinoRatio,
            ProfitFactor = leanKpis?.ProfitFactor ?? execution.ProfitFactor,
            LeanStatisticsJson = execution.LeanStatisticsJson,
            LeanAnalysisJson = execution.LeanAnalysisJson,
            VerdictJson = execution.RunVerdictJson,
            VerdictVersion = execution.VerdictVersion,
            VerdictGrade = execution.VerdictGrade,
            VerdictSignal = execution.VerdictSignal,
            EquityCurve = ParseEquityCurve(execution.EquityCurveJson, execution.Id, logger),
            ValidationAnalytics = ParseValidationAnalytics(execution.ValidationAnalyticsJson, execution.Id, logger),
            MetricDocumentation = ParseMetricDocumentation(execution, leanKpis, logger),
            InsightSummaryJson = execution.InsightSummaryJson,
            DataPolicyJson = execution.DataPolicyJson,
            CommissionPerOrder = execution.CommissionPerOrder,
            ParityGroupId = execution.ParityGroupId,
            Trades = (trades ?? execution.Trades)
                .OrderBy(t => t.EntryTimestamp)
                .Select(BacktestRunTradeDetailType.FromTrade)
                .ToList(),
            TradesTruncated = tradesTruncated,
            ParityVerdicts = parityVerdicts,
        };
    }

    private static BacktestRunLeanKpiType? ParseLeanKpis(StrategyExecution execution, ILogger logger)
    {
        if (execution.Source != "lean-sidecar" || string.IsNullOrWhiteSpace(execution.LeanStatisticsJson))
            return null;

        try
        {
            using var doc = JsonDocument.Parse(execution.LeanStatisticsJson);
            var portfolio = doc.RootElement.TryGetProperty("portfolio", out var portfolioElement) &&
                portfolioElement.ValueKind == JsonValueKind.Object
                    ? portfolioElement
                    : default;
            var trade = doc.RootElement.TryGetProperty("trade", out var tradeElement) &&
                tradeElement.ValueKind == JsonValueKind.Object
                    ? tradeElement
                    : default;

            return new BacktestRunLeanKpiType
            {
                MaxDrawdown = TryReadDecimal(portfolio, "drawdown"),
                SharpeRatio = TryReadDecimal(portfolio, "sharpe_ratio"),
                SortinoRatio = TryReadDecimal(portfolio, "sortino_ratio"),
                ProfitFactor = TryReadDecimal(trade, "profit_factor"),
            };
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException)
        {
            logger.LogWarning(
                ex,
                "StrategyExecution {ExecutionId} LEAN statistics JSON is unreadable",
                execution.Id);
            return null;
        }
    }

    private static decimal? TryReadDecimal(JsonElement parent, string propertyName)
    {
        if (parent.ValueKind != JsonValueKind.Object ||
            !parent.TryGetProperty(propertyName, out var value))
        {
            return null;
        }

        if (value.ValueKind == JsonValueKind.Number && value.TryGetDecimal(out var number))
            return number;

        if (value.ValueKind == JsonValueKind.String &&
            decimal.TryParse(value.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed))
        {
            return parsed;
        }

        return null;
    }

    private sealed record BacktestRunLeanKpiType
    {
        public decimal? MaxDrawdown { get; init; }
        public decimal? SharpeRatio { get; init; }
        public decimal? SortinoRatio { get; init; }
        public decimal? ProfitFactor { get; init; }
    }

    private static IReadOnlyList<MetricDocumentationContextType> ParseMetricDocumentation(
        StrategyExecution execution,
        BacktestRunLeanKpiType? leanKpis,
        ILogger logger)
    {
        if (!string.IsNullOrWhiteSpace(execution.MetricDocumentationJson))
        {
            try
            {
                var recorded = JsonSerializer.Deserialize<List<MetricDocumentationContextType>>(
                    execution.MetricDocumentationJson,
                    SnakeCaseJson);
                if (recorded is not null && recorded.All(context => context.IsValid()))
                {
                    return recorded
                        .Select(context => context with { ContractProvenance = "recorded" })
                        .ToList();
                }
            }
            catch (JsonException ex)
            {
                logger.LogWarning(
                    ex,
                    "StrategyExecution {ExecutionId} metric documentation JSON is unreadable; inferring context",
                    execution.Id);
            }
        }

        var variant = execution.Source == "lean-sidecar" && leanKpis?.SharpeRatio is not null
            ? new MetricDocumentationContextType
            {
                MetricId = "sharpe",
                VariantId = "sharpe.lean_native.v1",
                Producer = "lean_native",
                ContractId = "lean-statistics-oracle-v1",
                ContractProvenance = "inferred",
            }
            : new MetricDocumentationContextType
            {
                MetricId = "sharpe",
                VariantId = "sharpe.platform.v1",
                Producer = "platform",
                ContractId = "platform-sharpe-v1",
                ContractProvenance = "inferred",
            };

        return [variant];
    }

    private static readonly JsonSerializerOptions SnakeCaseJson = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    private static BacktestRunValidationAnalyticsType? ParseValidationAnalytics(
        string? json,
        int executionId,
        ILogger logger)
    {
        if (string.IsNullOrWhiteSpace(json))
            return null;

        try
        {
            var envelope = JsonSerializer.Deserialize<ValidationAnalyticsEnvelopeDto>(json, SnakeCaseJson);
            if (envelope is null)
                return null;

            return new BacktestRunValidationAnalyticsType
            {
                SchemaVersion = envelope.SchemaVersion,
                ComputedAtMs = envelope.ComputedAtMs,
                Engine = envelope.Engine ?? "",
                Horizons = envelope.Analytics?.Horizons ?? [],
                TimingCells = envelope.Analytics?.TimingCells ?? [],
                Seasonality = envelope.Analytics?.Seasonality ?? [],
                RollingTradeStability = envelope.Analytics?.RollingTradeStability ?? [],
            };
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException)
        {
            logger.LogWarning(
                ex,
                "StrategyExecution {ExecutionId} validation analytics JSON is unreadable",
                executionId);
            return new BacktestRunValidationAnalyticsType
            {
                Error = "Validation analytics envelope unreadable.",
            };
        }
    }

    private sealed record ValidationAnalyticsEnvelopeDto(
        int SchemaVersion,
        long ComputedAtMs,
        string? Engine,
        ValidationAnalyticsBodyDto? Analytics);

    private sealed record ValidationAnalyticsBodyDto(
        List<ValidationHorizonType>? Horizons,
        List<ValidationTimingCellType>? TimingCells,
        List<ValidationSeasonalityMonthType>? Seasonality,
        List<ValidationRollingTradePointType>? RollingTradeStability);

    private static BacktestRunEquityCurvesType? ParseEquityCurve(string? json, int executionId, ILogger logger)
    {
        if (string.IsNullOrWhiteSpace(json))
            return null;

        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (!root.TryGetProperty("schema_version", out var versionElement))
            {
                return new BacktestRunEquityCurvesType
                {
                    SchemaVersion = 1,
                    MarkToMarket = ParseLegacyCurve(root),
                    Realized = new BacktestRunEquityCurveType
                    {
                        Error = "Realized equity was not recorded for this legacy run.",
                    },
                };
            }
            if (!versionElement.TryGetInt32(out var schemaVersion) || schemaVersion != 2)
            {
                return new BacktestRunEquityCurvesType
                {
                    Error = "Equity report uses an unsupported schema version.",
                };
            }

            return new BacktestRunEquityCurvesType
            {
                SchemaVersion = schemaVersion,
                Error = ReadError(root),
                MarkToMarket = ParseCurve(root, "mark_to_market"),
                Realized = ParseCurve(root, "realized"),
            };
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException)
        {
            logger.LogWarning(
                ex,
                "StrategyExecution {ExecutionId} equity curve JSON is unreadable",
                executionId);
            return new BacktestRunEquityCurvesType
            {
                Error = "Equity curve envelope unreadable.",
            };
        }
    }

    private static BacktestRunEquityCurveType ParseCurve(JsonElement root, string propertyName)
    {
        if (!root.TryGetProperty(propertyName, out var curve) || curve.ValueKind != JsonValueKind.Object)
        {
            return new BacktestRunEquityCurveType
            {
                Error = $"Equity report is missing its {CurveLabel(propertyName)} curve.",
            };
        }

        return ParseCurveValue(curve, propertyName);
    }

    private static BacktestRunEquityCurveType ParseLegacyCurve(JsonElement curve) =>
        ParseCurveValue(curve, "mark_to_market");

    private static BacktestRunEquityCurveType ParseCurveValue(JsonElement curve, string propertyName)
    {
        var cadence = curve.TryGetProperty("cadence", out var cadenceElement) &&
            cadenceElement.ValueKind == JsonValueKind.String
                ? cadenceElement.GetString()
                : null;
        var rawPoints = ReadPointCount(curve, "raw_points");
        var keptPoints = ReadPointCount(curve, "kept_points");
        var error = ReadError(curve);
        if (error is not null)
        {
            return new BacktestRunEquityCurveType
            {
                Cadence = cadence,
                RawPoints = rawPoints,
                KeptPoints = keptPoints,
                Error = error,
            };
        }
        if (!HasExpectedCadence(propertyName, cadence))
        {
            return new BacktestRunEquityCurveType
            {
                Cadence = cadence,
                RawPoints = rawPoints,
                KeptPoints = keptPoints,
                Error = $"{CurveLabel(propertyName)} curve has an unsupported cadence.",
            };
        }
        if (!curve.TryGetProperty("points", out var points) || points.ValueKind != JsonValueKind.Array)
        {
            return new BacktestRunEquityCurveType
            {
                Cadence = cadence,
                RawPoints = rawPoints,
                KeptPoints = keptPoints,
                Error = $"{CurveLabel(propertyName)} curve is missing points.",
            };
        }

        var parsed = new List<BacktestRunEquityPointType>();
        long previousTimestamp = 0;
        foreach (var point in points.EnumerateArray())
        {
            if (!point.TryGetProperty("t", out var t) || !t.TryGetInt64(out var timestamp) ||
                !point.TryGetProperty("e", out var e) || !e.TryGetDecimal(out var equity) ||
                timestamp <= previousTimestamp)
            {
                return new BacktestRunEquityCurveType
                {
                    Cadence = cadence,
                    RawPoints = rawPoints,
                    KeptPoints = keptPoints,
                    Error = $"{CurveLabel(propertyName)} curve has invalid or non-increasing points.",
                };
            }
            parsed.Add(new BacktestRunEquityPointType(timestamp, equity));
            previousTimestamp = timestamp;
        }

        return new BacktestRunEquityCurveType
        {
            Cadence = cadence,
            RawPoints = rawPoints == 0 ? parsed.Count : rawPoints,
            KeptPoints = keptPoints == 0 ? parsed.Count : keptPoints,
            Points = parsed,
        };
    }

    private static string? ReadError(JsonElement element) =>
        element.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String
            ? error.GetString()
            : null;

    private static bool HasExpectedCadence(string propertyName, string? cadence) =>
        propertyName == "realized"
            ? cadence == "trade_exit"
            : cadence is "strategy_bar_close" or "lean_chart_sampling";

    private static string CurveLabel(string propertyName) =>
        propertyName == "mark_to_market" ? "Mark-to-market" : "Realized";

    private static int ReadPointCount(JsonElement curve, string propertyName)
    {
        if (curve.TryGetProperty("downsample", out var downsample) &&
            downsample.ValueKind == JsonValueKind.Object &&
            downsample.TryGetProperty(propertyName, out var value) &&
            value.TryGetInt32(out var count))
        {
            return count;
        }
        return 0;
    }
}

public sealed record BacktestRunEquityCurvesType
{
    public int SchemaVersion { get; init; }
    public string? Error { get; init; }
    public BacktestRunEquityCurveType? MarkToMarket { get; init; }
    public BacktestRunEquityCurveType? Realized { get; init; }
}

public sealed record BacktestRunEquityCurveType
{
    public string? Cadence { get; init; }
    public int RawPoints { get; init; }
    public int KeptPoints { get; init; }
    public string? Error { get; init; }
    public IReadOnlyList<BacktestRunEquityPointType> Points { get; init; } = [];
}

public sealed record BacktestRunValidationAnalyticsType
{
    public int SchemaVersion { get; init; }
    public long ComputedAtMs { get; init; }
    /// <summary>Engine that computed the frozen analytics ("python" | "lean").</summary>
    public string Engine { get; init; } = "";
    public string? Error { get; init; }
    public IReadOnlyList<ValidationHorizonType> Horizons { get; init; } = [];
    public IReadOnlyList<ValidationTimingCellType> TimingCells { get; init; } = [];
    public IReadOnlyList<ValidationSeasonalityMonthType> Seasonality { get; init; } = [];
    public IReadOnlyList<ValidationRollingTradePointType> RollingTradeStability { get; init; } = [];
}

public sealed record ValidationHorizonType
{
    public string Key { get; init; } = "";
    public string Label { get; init; } = "";
    public long StartMsUtc { get; init; }
    public long EndMsUtc { get; init; }
    public bool HasFullCoverage { get; init; }
    public double? NetReturn { get; init; }
    public int TradeCount { get; init; }
    public double? WinRate { get; init; }
    public double? ProfitFactor { get; init; }
}

public sealed record ValidationTimingCellType
{
    public int Weekday { get; init; }
    public string WeekdayLabel { get; init; } = "";
    public int HourEt { get; init; }
    public int TradeCount { get; init; }
    public double WinRate { get; init; }
    public double AverageReturn { get; init; }
}

public sealed record ValidationSeasonalityMonthType
{
    public int Month { get; init; }
    public string MonthLabel { get; init; } = "";
    public int ObservationCount { get; init; }
    public double? MedianCompoundedReturn { get; init; }
}

public sealed record ValidationRollingTradePointType
{
    public int TradeNumber { get; init; }
    public long EndMsUtc { get; init; }
    public int WindowSize { get; init; }
    public double AverageReturn { get; init; }
    public double WinRate { get; init; }
}

public sealed record BacktestRunEquityPointType(long T, decimal E);

public sealed record BacktestRunTradeDetailType
{
    public int Id { get; init; }
    public string TradeType { get; init; } = "";
    public long EntryTimestamp { get; init; }
    public long ExitTimestamp { get; init; }
    public decimal EntryPrice { get; init; }
    public decimal ExitPrice { get; init; }
    public decimal Quantity { get; init; }
    [GraphQLName("pnL")]
    public decimal PnL { get; init; }
    public decimal CumulativePnL { get; init; }
    public decimal PnlPts { get; init; }
    public decimal PnlPct { get; init; }
    public string SignalReason { get; init; } = "";
    public bool IsSyntheticExit { get; init; }

    public static BacktestRunTradeDetailType FromTrade(BacktestTrade trade) => new()
    {
        Id = trade.Id,
        TradeType = trade.TradeType,
        EntryTimestamp = UnixMs.FromUtc(trade.EntryTimestamp),
        ExitTimestamp = UnixMs.FromUtc(trade.ExitTimestamp),
        EntryPrice = trade.EntryPrice,
        ExitPrice = trade.ExitPrice,
        Quantity = trade.Quantity,
        PnL = trade.PnL,
        CumulativePnL = trade.CumulativePnL,
        PnlPts = trade.ExitPrice - trade.EntryPrice,
        PnlPct = trade.EntryPrice > 0 ? (trade.ExitPrice - trade.EntryPrice) / trade.EntryPrice : 0m,
        SignalReason = trade.SignalReason,
        IsSyntheticExit = trade.IsSyntheticExit,
    };
}

public sealed record BacktestRunParityVerdictType
{
    public int Id { get; init; }
    public int LeftExecutionId { get; init; }
    /// <summary>Null while pending / on unavailable and failed dispositions.</summary>
    public int? RightExecutionId { get; init; }
    public string? ParityGroupId { get; init; }
    public int VerdictVersion { get; init; }
    public string Status { get; init; } = "";
    public string VerdictJson { get; init; } = "";
    public long CreatedAt { get; init; }
}
