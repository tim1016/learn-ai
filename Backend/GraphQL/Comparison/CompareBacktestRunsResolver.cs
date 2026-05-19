using System.Text.Json;
using Backend.Data;
using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL.Comparison;

[ExtendObjectType<Query>]
public class CompareBacktestRunsResolver
{
    [GraphQLName("compareBacktestRuns")]
    public static async Task<RunComparisonResult?> GetCompareBacktestRunsAsync(
        int leftId,
        int rightId,
        [Service] AppDbContext db,
        [Service] IComparisonService comparisonService,
        CancellationToken ct)
    {
        var left = await db.StrategyExecutions
            .AsNoTracking()
            .Include(s => s.Trades)
            .FirstOrDefaultAsync(s => s.Id == leftId, ct);
        if (left is null) return null;

        var right = await db.StrategyExecutions
            .AsNoTracking()
            .Include(s => s.Trades)
            .FirstOrDefaultAsync(s => s.Id == rightId, ct);
        if (right is null) return null;

        var guardrails = ComputeGuardrails(left, right);
        var summary = ComputeSummary(left, right);

        var leftSorted = left.Trades.OrderBy(t => t.EntryTimestamp).ToList();
        var rightSorted = right.Trades.OrderBy(t => t.EntryTimestamp).ToList();

        var compareRequest = new CompareTradesRequest(
            LeftTrades: leftSorted.Select((t, i) => ToCompareTrade(t, tradeNumber: i + 1)).ToList(),
            RightTrades: rightSorted.Select((t, i) => ToCompareTrade(t, tradeNumber: i + 1)).ToList());

        var pythonResponse = await comparisonService.CompareTradesAsync(compareRequest, ct);

        var divergences = pythonResponse.Divergences
            .Select(d => new TradeDivergence(
                Category: Enum.TryParse<DivergenceCategory>(d.Category, ignoreCase: false, out var cat)
                    ? cat
                    : DivergenceCategory.FIXTURE_INSUFFICIENT,
                TradeNumber: d.TradeNumber,
                MsUtc: d.MsUtc,
                Message: d.Message,
                LeftFillPrice: d.LeftFillPrice,
                RightFillPrice: d.RightFillPrice,
                LeftQuantity: d.LeftQuantity,
                RightQuantity: d.RightQuantity))
            .ToList();

        return new RunComparisonResult(
            Left: left,
            Right: right,
            Guardrails: guardrails,
            Summary: summary,
            Divergences: divergences,
            FirstDivergenceMsUtc: pythonResponse.FirstDivergenceMsUtc);
    }

    private static ComparisonGuardrails ComputeGuardrails(StrategyExecution left, StrategyExecution right)
    {
        var warnings = new List<string>();

        bool sameAlgorithm = left.StrategyName == right.StrategyName;
        if (!sameAlgorithm)
        {
            warnings.Add($"Different algorithms: {left.StrategyName} vs {right.StrategyName}");
        }

        string? leftSymbol = ExtractSymbol(left.Parameters);
        string? rightSymbol = ExtractSymbol(right.Parameters);
        bool sameSymbol = string.Equals(leftSymbol, rightSymbol, StringComparison.OrdinalIgnoreCase);
        if (!sameSymbol)
        {
            warnings.Add($"Different symbols: {leftSymbol ?? "(none)"} vs {rightSymbol ?? "(none)"}");
        }

        bool sameWindow = left.StartDate == right.StartDate && left.EndDate == right.EndDate;
        if (!sameWindow)
        {
            warnings.Add("Different windows; comparison restricted to intersection");
        }

        bool sameParameters = left.Parameters == right.Parameters;

        return new ComparisonGuardrails(
            SameAlgorithm: sameAlgorithm,
            SameSymbol: sameSymbol,
            SameWindow: sameWindow,
            SameParameters: sameParameters,
            Warnings: warnings);
    }

    private static ComparisonSummary ComputeSummary(StrategyExecution left, StrategyExecution right)
    {
        return new ComparisonSummary(
            PnlDelta: right.TotalPnL - left.TotalPnL,
            TradeCountDelta: right.TotalTrades - left.TotalTrades,
            WinRateDelta: (double)(right.WinRate - left.WinRate),
            FeesDelta: right.TotalFees - left.TotalFees,
            FinalEquityDelta: right.FinalEquity - left.FinalEquity);
    }

    private static PersistLeanTradePayload ToCompareTrade(BacktestTrade t, int tradeNumber)
    {
        return new PersistLeanTradePayload(
            TradeNumber: tradeNumber,
            EntryMsUtc: new DateTimeOffset(t.EntryTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
            ExitMsUtc: new DateTimeOffset(t.ExitTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
            EntryPrice: t.EntryPrice,
            ExitPrice: t.ExitPrice,
            Quantity: t.Quantity,
            Pnl: t.PnL,
            SignalReason: t.SignalReason,
            IsSyntheticExit: t.IsSyntheticExit);
    }

    private static string? ExtractSymbol(string? parametersJson)
    {
        if (string.IsNullOrEmpty(parametersJson)) return null;
        try
        {
            using var doc = JsonDocument.Parse(parametersJson);
            if (doc.RootElement.TryGetProperty("symbol", out var sym))
            {
                return sym.GetString();
            }
        }
        catch (JsonException)
        {
            // Parameters isn't JSON; treat as opaque.
        }
        return null;
    }
}
