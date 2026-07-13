using System.Text.Json;
using Backend.Data;
using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

/// <summary>
/// Freezes the cross-engine ParityVerdict for a parity group when the
/// LEAN companion run lands. Trade reconciliation is delegated to the
/// Python compare endpoint (via <see cref="IComparisonService"/>) — the
/// 8-category DivergenceCategory taxonomy stays Python-owned; this class
/// only maps "any divergences?" onto the verdict state machine.
///
/// State machine: <c>pending → agree | diverged</c> here;
/// <c>pending → run_failed | persist_failed</c> via the mark-failed
/// endpoint. All transitions are conditional on <c>pending</c> — the
/// first terminal state wins and is never overwritten.
/// </summary>
public class ParityVerdictService : IParityVerdictService
{
    public const int VerdictVersion = 1;
    private const string FillPriceAtol = "0.01";

    private readonly AppDbContext _db;
    private readonly IComparisonService _comparison;
    private readonly ILogger<ParityVerdictService> _logger;

    public ParityVerdictService(
        AppDbContext db,
        IComparisonService comparison,
        ILogger<ParityVerdictService> logger)
    {
        _db = db;
        _comparison = comparison;
        _logger = logger;
    }

    public async Task ComputeForLeanRunAsync(int rightExecutionId, string parityGroupId, CancellationToken ct)
    {
        var right = await _db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Trades)
            .FirstOrDefaultAsync(e => e.Id == rightExecutionId, ct);
        var left = await _db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Trades)
            .Where(e => e.ParityGroupId == parityGroupId && e.Source == "engine")
            .OrderBy(e => e.Id)
            .FirstOrDefaultAsync(ct);

        if (right is null || left is null)
        {
            _logger.LogWarning(
                "[PARITY] Cannot compute verdict for group {Group}: left={LeftFound} right={RightFound}",
                parityGroupId, left is not null, right is not null);
            return;
        }

        var comparison = await _comparison.CompareTradesAsync(
            new CompareTradesRequest(
                LeftTrades: ToComparePayload(left.Trades),
                RightTrades: ToComparePayload(right.Trades)),
            ct);

        var status = comparison.Divergences.Count == 0 ? "agree" : "diverged";
        var verdictJson = JsonSerializer.Serialize(new
        {
            schema_version = 1,
            parity_group_id = parityGroupId,
            left_execution_id = left.Id,
            right_execution_id = right.Id,
            engines = new { left = "python", right = "lean" },
            status,
            reason = (string?)null,
            tolerances = new { fill_price_atol = FillPriceAtol },
            divergences = comparison.Divergences.Select(d => new
            {
                category = d.Category,
                trade_number = d.TradeNumber,
                ms_utc = d.MsUtc,
                message = d.Message,
            }),
            counts_by_category = comparison.Divergences
                .GroupBy(d => d.Category)
                .ToDictionary(g => g.Key, g => g.Count()),
            computed_at_ms = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });

        var row = await _db.ParityVerdicts.FirstOrDefaultAsync(v => v.ParityGroupId == parityGroupId, ct);
        if (row is null)
        {
            // The pending row from run time was lost (best-effort create) —
            // recover by inserting the terminal verdict directly.
            _db.ParityVerdicts.Add(new ParityVerdict
            {
                LeftExecutionId = left.Id,
                RightExecutionId = right.Id,
                ParityGroupId = parityGroupId,
                VerdictVersion = VerdictVersion,
                Status = status,
                VerdictJson = verdictJson,
                CreatedAtUtc = DateTime.UtcNow,
            });
        }
        else if (row.Status == "pending")
        {
            row.RightExecutionId = right.Id;
            row.VerdictVersion = VerdictVersion;
            row.Status = status;
            row.VerdictJson = verdictJson;
        }
        else
        {
            _logger.LogInformation(
                "[PARITY] Verdict for group {Group} already terminal ({Status}); not overwriting",
                parityGroupId, row.Status);
            return;
        }

        await _db.SaveChangesAsync(ct);
        _logger.LogInformation(
            "[PARITY] Frozen verdict for group {Group}: {Status} ({Count} divergences)",
            parityGroupId, status, comparison.Divergences.Count);
    }

    private static List<PersistLeanTradePayload> ToComparePayload(IEnumerable<BacktestTrade> trades)
    {
        return trades
            .OrderBy(t => t.EntryTimestamp)
            .Select((t, index) => new PersistLeanTradePayload(
                TradeNumber: index + 1,
                EntryMsUtc: new DateTimeOffset(t.EntryTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                ExitMsUtc: new DateTimeOffset(t.ExitTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                EntryPrice: t.EntryPrice,
                ExitPrice: t.ExitPrice,
                Quantity: t.Quantity,
                Pnl: t.PnL,
                SignalReason: t.SignalReason,
                IsSyntheticExit: t.IsSyntheticExit))
            .ToList();
    }
}
