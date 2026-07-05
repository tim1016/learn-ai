using System.Text.Json;
using Backend.Data;
using Backend.Models.MarketData;
using Backend.Temporal;
using Microsoft.EntityFrameworkCore;

namespace Backend;

/// <summary>
/// Minimal API endpoints for backtest study persistence and retrieval.
/// Called by the Python engine service (auto-save) and the Angular frontend (list/detail/notes).
/// </summary>
public static class StudiesApi
{
    public static void MapStudiesEndpoints(this WebApplication app)
    {
        var group = app.MapGroup("/api/studies").WithTags("Studies");

        group.MapPost("/", SaveStudyAsync);
        group.MapGet("/", ListStudiesAsync);
        group.MapGet("/{id:int}", GetStudyByIdAsync);
        group.MapPatch("/{id:int}/notes", UpdateNotesAsync);
        group.MapDelete("/{id:int}", DeleteStudyAsync);
    }

    // ── POST /api/studies — save a new study ──────────────────────
    private static async Task<IResult> SaveStudyAsync(
        SaveStudyRequest request,
        AppDbContext db,
        CancellationToken ct)
    {
        var tradeTimestampError = ValidateSaveStudyTradeTimestamps(request);
        if (tradeTimestampError is not null)
            return Results.BadRequest(new { error = tradeTimestampError });

        // Resolve or create the Ticker entity
        var ticker = await db.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == request.Symbol.ToUpper(), ct);

        if (ticker == null)
        {
            ticker = new Ticker { Symbol = request.Symbol.ToUpper(), Name = request.Symbol.ToUpper(), Market = "stocks" };
            db.Tickers.Add(ticker);
            await db.SaveChangesAsync(ct);
        }

        var execution = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = request.StrategyName,
            Parameters = request.Parameters ?? "{}",
            StartDate = request.StartDate,
            EndDate = request.EndDate,
            Timespan = request.Timespan ?? "minute",
            Multiplier = 1,
            TotalTrades = request.TotalTrades,
            WinningTrades = request.WinningTrades,
            LosingTrades = request.LosingTrades,
            TotalPnL = request.TotalPnL,
            MaxDrawdown = request.MaxDrawdown,
            SharpeRatio = request.SharpeRatio,
            InitialCash = request.InitialCash,
            FinalEquity = request.FinalEquity,
            TotalFees = request.TotalFees,
            WinRate = request.WinRate,
            CompoundingAnnualReturn = request.CompoundingAnnualReturn,
            SortinoRatio = request.SortinoRatio,
            ProbabilisticSharpeRatio = request.ProbabilisticSharpeRatio,
            ProfitFactor = request.ProfitFactor,
            Alpha = request.Alpha,
            Beta = request.Beta,
            InformationRatio = request.InformationRatio,
            TrackingError = request.TrackingError,
            TreynorRatio = request.TreynorRatio,
            ValueAtRisk95 = request.ValueAtRisk95,
            ValueAtRisk99 = request.ValueAtRisk99,
            AnnualStandardDeviation = request.AnnualStandardDeviation,
            DrawdownRecoveryDays = request.DrawdownRecoveryDays,
            LeanStatisticsJson = request.LeanStatisticsJson,
            Source = request.Source ?? "engine",
            FillMode = request.FillMode ?? "signal_bar_close",
            Notes = request.Notes,
            ExecutedAt = DateTime.UtcNow,
            DurationMs = request.DurationMs,
            // PR B (2026-05-19) — DataPolicy / Commission / Brokerage echo
            // from the Python engine's auto-save payload. Synthesized for
            // legacy callers that haven't been updated to send the canonical
            // block yet (mirrors the synthesis in BacktestRunPersistenceService).
            DataPolicyJson = request.DataPolicyJson ?? SynthesizeLegacyDataPolicy(request.Symbol),
            CommissionPerOrder = request.CommissionPerOrder ?? 0m,
            BrokeragePolicy = request.BrokeragePolicy ?? "algorithm_default",
        };

        // Attach trades if provided
        if (request.Trades is { Count: > 0 })
        {
            foreach (var t in request.Trades)
            {
                execution.Trades.Add(new BacktestTrade
                {
                    TradeType = t.TradeType ?? "Buy",
                    EntryTimestamp = UnixMs.ToUtcDateTime(t.EntryTimestamp!.Value),
                    ExitTimestamp = UnixMs.ToUtcDateTime(t.ExitTimestamp!.Value),
                    EntryPrice = t.EntryPrice,
                    ExitPrice = t.ExitPrice,
                    Quantity = t.Quantity ?? 1m,
                    PnL = t.PnL,
                    CumulativePnL = t.CumulativePnL,
                    SignalReason = t.SignalReason ?? "",
                });
            }
        }

        db.StrategyExecutions.Add(execution);
        await db.SaveChangesAsync(ct);

        return Results.Created($"/api/studies/{execution.Id}", new { id = execution.Id });
    }

    internal static string? ValidateSaveStudyTradeTimestamps(SaveStudyRequest request)
    {
        if (request.Trades is not { Count: > 0 })
            return null;

        for (var i = 0; i < request.Trades.Count; i++)
        {
            var trade = request.Trades[i];
            if (trade.EntryTimestamp is null or <= 0)
                return $"trades[{i}].entryTimestamp is required and must be a positive int64 ms UTC timestamp.";
            if (trade.ExitTimestamp is null or <= 0)
                return $"trades[{i}].exitTimestamp is required and must be a positive int64 ms UTC timestamp.";
        }

        return null;
    }

    // ── GET /api/studies — list studies with sorting ──────────────
    private static async Task<IResult> ListStudiesAsync(
        AppDbContext db,
        string? sortBy,
        string? sortDir,
        string? source,
        int page = 1,
        int pageSize = 50,
        CancellationToken ct = default)
    {
        var query = db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Ticker)
            .AsQueryable();

        if (!string.IsNullOrEmpty(source))
            query = query.Where(e => e.Source == source);

        // Dynamic sorting
        var descending = string.Equals(sortDir, "desc", StringComparison.OrdinalIgnoreCase);
        query = (sortBy?.ToLower()) switch
        {
            "date" or "executedat" => descending ? query.OrderByDescending(e => e.ExecutedAt) : query.OrderBy(e => e.ExecutedAt),
            "strategy" or "strategyname" => descending ? query.OrderByDescending(e => e.StrategyName) : query.OrderBy(e => e.StrategyName),
            "pnl" or "totalpnl" => descending ? query.OrderByDescending(e => e.TotalPnL) : query.OrderBy(e => e.TotalPnL),
            "sharpe" or "sharperatio" => descending ? query.OrderByDescending(e => e.SharpeRatio) : query.OrderBy(e => e.SharpeRatio),
            "sortino" or "sortinoratio" => descending ? query.OrderByDescending(e => e.SortinoRatio) : query.OrderBy(e => e.SortinoRatio),
            "cagr" or "compoundingannualreturn" => descending ? query.OrderByDescending(e => e.CompoundingAnnualReturn) : query.OrderBy(e => e.CompoundingAnnualReturn),
            "drawdown" or "maxdrawdown" => descending ? query.OrderByDescending(e => e.MaxDrawdown) : query.OrderBy(e => e.MaxDrawdown),
            "winrate" => descending ? query.OrderByDescending(e => e.WinRate) : query.OrderBy(e => e.WinRate),
            "trades" or "totaltrades" => descending ? query.OrderByDescending(e => e.TotalTrades) : query.OrderBy(e => e.TotalTrades),
            "profitfactor" => descending ? query.OrderByDescending(e => e.ProfitFactor) : query.OrderBy(e => e.ProfitFactor),
            "var95" => descending ? query.OrderByDescending(e => e.ValueAtRisk95) : query.OrderBy(e => e.ValueAtRisk95),
            "psr" => descending ? query.OrderByDescending(e => e.ProbabilisticSharpeRatio) : query.OrderBy(e => e.ProbabilisticSharpeRatio),
            _ => query.OrderByDescending(e => e.ExecutedAt), // default: most recent first
        };

        var totalCount = await query.CountAsync(ct);
        var items = await query
            .Skip((page - 1) * pageSize)
            .Take(pageSize)
            .Select(e => new StudyListItem
            {
                Id = e.Id,
                Symbol = e.Ticker.Symbol,
                StrategyName = e.StrategyName,
                StartDate = e.StartDate,
                EndDate = e.EndDate,
                Timespan = e.Timespan,
                FillMode = e.FillMode,
                Source = e.Source,
                TotalTrades = e.TotalTrades,
                WinningTrades = e.WinningTrades,
                LosingTrades = e.LosingTrades,
                WinRate = e.WinRate,
                TotalPnL = e.TotalPnL,
                MaxDrawdown = e.MaxDrawdown,
                SharpeRatio = e.SharpeRatio,
                SortinoRatio = e.SortinoRatio,
                CompoundingAnnualReturn = e.CompoundingAnnualReturn,
                ProbabilisticSharpeRatio = e.ProbabilisticSharpeRatio,
                ProfitFactor = e.ProfitFactor,
                ValueAtRisk95 = e.ValueAtRisk95,
                Alpha = e.Alpha,
                Beta = e.Beta,
                InitialCash = e.InitialCash,
                FinalEquity = e.FinalEquity,
                Parameters = e.Parameters,
                Notes = e.Notes,
                ExecutedAt = UnixMs.FromUtc(e.ExecutedAt),
                DurationMs = e.DurationMs,
            })
            .ToListAsync(ct);

        return Results.Ok(new StudyListResponse
        {
            Items = items,
            TotalCount = totalCount,
            Page = page,
            PageSize = pageSize,
        });
    }

    // ── GET /api/studies/{id} — single study with full LEAN stats + trades ──
    private static async Task<IResult> GetStudyByIdAsync(
        int id,
        AppDbContext db,
        CancellationToken ct)
    {
        var execution = await db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Ticker)
            .Include(e => e.Trades)
            .FirstOrDefaultAsync(e => e.Id == id, ct);

        if (execution == null)
            return Results.NotFound(new { error = $"Study {id} not found" });

        return Results.Ok(new StudyDetailResponse
        {
            Id = execution.Id,
            Symbol = execution.Ticker.Symbol,
            StrategyName = execution.StrategyName,
            Parameters = execution.Parameters,
            StartDate = execution.StartDate,
            EndDate = execution.EndDate,
            Timespan = execution.Timespan,
            FillMode = execution.FillMode,
            Source = execution.Source,
            InitialCash = execution.InitialCash,
            FinalEquity = execution.FinalEquity,
            TotalFees = execution.TotalFees,
            TotalTrades = execution.TotalTrades,
            WinningTrades = execution.WinningTrades,
            LosingTrades = execution.LosingTrades,
            WinRate = execution.WinRate,
            TotalPnL = execution.TotalPnL,
            MaxDrawdown = execution.MaxDrawdown,
            SharpeRatio = execution.SharpeRatio,
            SortinoRatio = execution.SortinoRatio,
            CompoundingAnnualReturn = execution.CompoundingAnnualReturn,
            ProbabilisticSharpeRatio = execution.ProbabilisticSharpeRatio,
            ProfitFactor = execution.ProfitFactor,
            Alpha = execution.Alpha,
            Beta = execution.Beta,
            InformationRatio = execution.InformationRatio,
            TrackingError = execution.TrackingError,
            TreynorRatio = execution.TreynorRatio,
            ValueAtRisk95 = execution.ValueAtRisk95,
            ValueAtRisk99 = execution.ValueAtRisk99,
            AnnualStandardDeviation = execution.AnnualStandardDeviation,
            DrawdownRecoveryDays = execution.DrawdownRecoveryDays,
            LeanStatisticsJson = execution.LeanStatisticsJson,
            Notes = execution.Notes,
            ExecutedAt = UnixMs.FromUtc(execution.ExecutedAt),
            DurationMs = execution.DurationMs,
            Trades = execution.Trades.OrderBy(t => t.EntryTimestamp).Select(t => new StudyTradeItem
            {
                TradeType = t.TradeType,
                EntryTimestamp = UnixMs.FromUtc(t.EntryTimestamp),
                ExitTimestamp = UnixMs.FromUtc(t.ExitTimestamp),
                EntryPrice = t.EntryPrice,
                ExitPrice = t.ExitPrice,
                Quantity = t.Quantity,
                PnL = t.PnL,
                CumulativePnL = t.CumulativePnL,
                SignalReason = t.SignalReason,
            }).ToList(),
        });
    }

    // ── PATCH /api/studies/{id}/notes — update notes ──────────────
    private static async Task<IResult> UpdateNotesAsync(
        int id,
        UpdateNotesRequest request,
        AppDbContext db,
        CancellationToken ct)
    {
        var execution = await db.StrategyExecutions.FindAsync([id], ct);
        if (execution == null)
            return Results.NotFound(new { error = $"Study {id} not found" });

        execution.Notes = request.Notes;
        await db.SaveChangesAsync(ct);

        return Results.Ok(new { id, notes = execution.Notes });
    }

    // ── DELETE /api/studies/{id} — remove a study ─────────────────
    private static async Task<IResult> DeleteStudyAsync(
        int id,
        AppDbContext db,
        CancellationToken ct)
    {
        var execution = await db.StrategyExecutions.FindAsync([id], ct);
        if (execution == null)
            return Results.NotFound(new { error = $"Study {id} not found" });

        db.StrategyExecutions.Remove(execution);
        await db.SaveChangesAsync(ct);

        return Results.NoContent();
    }

    // PR B (2026-05-19) — one-cycle backwards-compat for pre-PR-B callers
    // that POST without ``DataPolicyJson``. Records the engines' actual
    // behaviour today (Polygon, pre-adjusted, regular session, m/1 → m/15)
    // so the history surface and compare-view never see a null DataPolicy
    // on a freshly-written row. Mirrors the synthesizer in
    // ``BacktestRunPersistenceService.SynthesizeLegacyDataPolicy``.
    private static string SynthesizeLegacyDataPolicy(string symbol)
    {
        var dp = new
        {
            source = "polygon",
            symbol = symbol?.ToUpperInvariant() ?? "",
            adjusted = true,
            session = "regular",
            input_bars = new { timespan = "minute", multiplier = 1 },
            strategy_bars = new { timespan = "minute", multiplier = 15 },
            timestamp_policy = "bar_close_ms_utc",
            timezone = "America/New_York",
            provider_kind = "live",
            fixture_id = (string?)null,
            fixture_sha256 = (string?)null,
        };
        return System.Text.Json.JsonSerializer.Serialize(dp);
    }
}

// ── Request / Response DTOs ──────────────────────────────────────

public record SaveStudyRequest
{
    public string Symbol { get; init; } = "";
    public string StrategyName { get; init; } = "";
    public string? Parameters { get; init; }
    public string StartDate { get; init; } = "";
    public string EndDate { get; init; } = "";
    public string? Timespan { get; init; }
    public string? FillMode { get; init; }
    public string? Source { get; init; }
    public int TotalTrades { get; init; }
    public int WinningTrades { get; init; }
    public int LosingTrades { get; init; }
    public decimal TotalPnL { get; init; }
    public decimal MaxDrawdown { get; init; }
    public decimal SharpeRatio { get; init; }
    public decimal InitialCash { get; init; }
    public decimal FinalEquity { get; init; }
    public decimal TotalFees { get; init; }
    public decimal WinRate { get; init; }
    public decimal CompoundingAnnualReturn { get; init; }
    public decimal SortinoRatio { get; init; }
    public decimal ProbabilisticSharpeRatio { get; init; }
    public decimal ProfitFactor { get; init; }
    public decimal Alpha { get; init; }
    public decimal Beta { get; init; }
    public decimal InformationRatio { get; init; }
    public decimal TrackingError { get; init; }
    public decimal TreynorRatio { get; init; }
    public decimal ValueAtRisk95 { get; init; }
    public decimal ValueAtRisk99 { get; init; }
    public decimal AnnualStandardDeviation { get; init; }
    public int DrawdownRecoveryDays { get; init; }
    public string? LeanStatisticsJson { get; init; }
    public string? Notes { get; init; }
    public long DurationMs { get; init; }
    public List<SaveStudyTrade>? Trades { get; init; }

    /// <summary>
    /// PR B (2026-05-19) — canonical DataPolicy block as a JSON string.
    /// Python engine emits it as part of the auto-save payload. Null from
    /// legacy callers; ``SaveStudyAsync`` synthesizes a default block from
    /// ``Symbol`` in that case.
    /// </summary>
    public string? DataPolicyJson { get; init; }

    /// <summary>
    /// PR B (2026-05-19) — commission per order recorded with the run so
    /// the compare-view can gate on it. Null from legacy callers; defaults
    /// to 0 on persist.
    /// </summary>
    public decimal? CommissionPerOrder { get; init; }

    /// <summary>
    /// PR B (2026-05-19) — brokerage policy. The Python engine writes
    /// ``"algorithm_default"`` because it doesn't model brokerage.
    /// </summary>
    public string? BrokeragePolicy { get; init; }
}

public record SaveStudyTrade
{
    public string? TradeType { get; init; }
    public long? EntryTimestamp { get; init; }
    public long? ExitTimestamp { get; init; }
    public decimal EntryPrice { get; init; }
    public decimal ExitPrice { get; init; }
    // Nullable so callers that haven't been updated still validate; missing
    // values default to 1m at the BacktestTrade construction site to keep the
    // legacy shape compatible. Real engine runs always include the resolved
    // fill quantity — a null at runtime is a regression worth investigating.
    public decimal? Quantity { get; init; }
    public decimal PnL { get; init; }
    public decimal CumulativePnL { get; init; }
    public string? SignalReason { get; init; }
}

public record UpdateNotesRequest
{
    public string? Notes { get; init; }
}

public record StudyListResponse
{
    public List<StudyListItem> Items { get; init; } = [];
    public int TotalCount { get; init; }
    public int Page { get; init; }
    public int PageSize { get; init; }
}

public record StudyListItem
{
    public int Id { get; init; }
    public string Symbol { get; init; } = "";
    public string StrategyName { get; init; } = "";
    public string StartDate { get; init; } = "";
    public string EndDate { get; init; } = "";
    public string Timespan { get; init; } = "";
    public string FillMode { get; init; } = "";
    public string Source { get; init; } = "";
    public int TotalTrades { get; init; }
    public int WinningTrades { get; init; }
    public int LosingTrades { get; init; }
    public decimal WinRate { get; init; }
    public decimal TotalPnL { get; init; }
    public decimal MaxDrawdown { get; init; }
    public decimal SharpeRatio { get; init; }
    public decimal SortinoRatio { get; init; }
    public decimal CompoundingAnnualReturn { get; init; }
    public decimal ProbabilisticSharpeRatio { get; init; }
    public decimal ProfitFactor { get; init; }
    public decimal ValueAtRisk95 { get; init; }
    public decimal Alpha { get; init; }
    public decimal Beta { get; init; }
    public decimal InitialCash { get; init; }
    public decimal FinalEquity { get; init; }
    public string Parameters { get; init; } = "{}";
    public string? Notes { get; init; }
    public long ExecutedAt { get; init; }
    public long DurationMs { get; init; }
}

public record StudyDetailResponse : StudyListItem
{
    public decimal TotalFees { get; init; }
    public decimal InformationRatio { get; init; }
    public decimal TrackingError { get; init; }
    public decimal TreynorRatio { get; init; }
    public decimal ValueAtRisk99 { get; init; }
    public decimal AnnualStandardDeviation { get; init; }
    public int DrawdownRecoveryDays { get; init; }
    public string? LeanStatisticsJson { get; init; }
    public List<StudyTradeItem> Trades { get; init; } = [];
}

public record StudyTradeItem
{
    public string TradeType { get; init; } = "";
    public long EntryTimestamp { get; init; }
    public long ExitTimestamp { get; init; }
    public decimal EntryPrice { get; init; }
    public decimal ExitPrice { get; init; }
    public decimal Quantity { get; init; } = 1m;
    public decimal PnL { get; init; }
    public decimal CumulativePnL { get; init; }
    public string SignalReason { get; init; } = "";
}
