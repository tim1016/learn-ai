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
            .Include(e => e.Trades)
            .FirstOrDefaultAsync(e => e.Id == id, ct);

        if (execution is null)
            return null;

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

        return BacktestRunDetailType.FromExecution(execution, parityVerdicts, logger);
    }
}

public sealed record BacktestRunDetailType
{
    public int Id { get; init; }
    public Engine Engine { get; init; }
    public string Source { get; init; } = "";
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
    public decimal SharpeRatio { get; init; }
    public decimal SortinoRatio { get; init; }
    public decimal ProfitFactor { get; init; }
    public string? LeanStatisticsJson { get; init; }
    public string? VerdictJson { get; init; }
    public int? VerdictVersion { get; init; }
    public string? VerdictGrade { get; init; }
    public string? VerdictSignal { get; init; }
    public BacktestRunEquityCurveType? EquityCurve { get; init; }
    public BacktestRunValidationAnalyticsType? ValidationAnalytics { get; init; }
    public string? InsightSummaryJson { get; init; }
    public string? DataPolicyJson { get; init; }
    public DataPolicyType? DataPolicy => DataPolicyType.TryParse(DataPolicyJson);
    public string? ParityGroupId { get; init; }
    public IReadOnlyList<BacktestRunTradeDetailType> Trades { get; init; } = [];
    public IReadOnlyList<BacktestRunParityVerdictType> ParityVerdicts { get; init; } = [];

    public static BacktestRunDetailType FromExecution(
        StrategyExecution execution,
        IReadOnlyList<BacktestRunParityVerdictType> parityVerdicts,
        ILogger logger)
    {
        var leanKpis = ParseLeanKpis(execution, logger);
        return new BacktestRunDetailType
        {
            Id = execution.Id,
            Engine = EngineExtensions.FromSource(execution.Source),
            Source = execution.Source,
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
            VerdictJson = execution.RunVerdictJson,
            VerdictVersion = execution.VerdictVersion,
            VerdictGrade = execution.VerdictGrade,
            VerdictSignal = execution.VerdictSignal,
            EquityCurve = ParseEquityCurve(execution.EquityCurveJson, execution.Id, logger),
            ValidationAnalytics = ParseValidationAnalytics(execution.ValidationAnalyticsJson, execution.Id, logger),
            InsightSummaryJson = execution.InsightSummaryJson,
            DataPolicyJson = execution.DataPolicyJson,
            ParityGroupId = execution.ParityGroupId,
            Trades = execution.Trades
                .OrderBy(t => t.EntryTimestamp)
                .Select(BacktestRunTradeDetailType.FromTrade)
                .ToList(),
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

    private static BacktestRunEquityCurveType? ParseEquityCurve(string? json, int executionId, ILogger logger)
    {
        if (string.IsNullOrWhiteSpace(json))
            return null;

        try
        {
            using var doc = JsonDocument.Parse(json);
            var cadence = doc.RootElement.TryGetProperty("cadence", out var cadenceElement) &&
                cadenceElement.ValueKind == JsonValueKind.String
                    ? cadenceElement.GetString()
                    : null;
            var rawPoints = 0;
            var keptPoints = 0;
            if (doc.RootElement.TryGetProperty("downsample", out var downsample) &&
                downsample.ValueKind == JsonValueKind.Object)
            {
                if (downsample.TryGetProperty("raw_points", out var raw))
                    rawPoints = raw.GetInt32();
                if (downsample.TryGetProperty("kept_points", out var kept))
                    keptPoints = kept.GetInt32();
            }
            if (!doc.RootElement.TryGetProperty("points", out var points) ||
                points.ValueKind != JsonValueKind.Array)
            {
                logger.LogWarning(
                    "StrategyExecution {ExecutionId} equity curve JSON has no points array",
                    executionId);
                return new BacktestRunEquityCurveType
                {
                    Cadence = cadence,
                    RawPoints = rawPoints,
                    KeptPoints = keptPoints,
                    Error = "Equity curve envelope missing points.",
                    Points = [],
                };
            }

            var parsed = new List<BacktestRunEquityPointType>();
            foreach (var point in points.EnumerateArray())
            {
                if (!point.TryGetProperty("t", out var t) || !point.TryGetProperty("e", out var e))
                    continue;
                parsed.Add(new BacktestRunEquityPointType(t.GetInt64(), e.GetDecimal()));
            }
            return new BacktestRunEquityCurveType
            {
                Cadence = cadence,
                RawPoints = rawPoints == 0 ? parsed.Count : rawPoints,
                KeptPoints = keptPoints == 0 ? parsed.Count : keptPoints,
                Points = parsed,
            };
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException)
        {
            logger.LogWarning(
                ex,
                "StrategyExecution {ExecutionId} equity curve JSON is unreadable",
                executionId);
            return new BacktestRunEquityCurveType
            {
                Error = "Equity curve envelope unreadable.",
                Points = [],
            };
        }
    }
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
