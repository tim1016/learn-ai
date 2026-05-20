using System.Text.Json;
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.Compare;
using Backend.Models.MarketData;
using Backend.Services;
using Microsoft.EntityFrameworkCore;

namespace Backend.Controllers;

/// <summary>
/// PR B (2026-05-19) Phase 4 — minimal-API endpoint for the compare view.
///
/// ``GET /api/runs/compare?left=&lt;id&gt;&amp;right=&lt;id&gt;`` loads both runs
/// (with their trades), runs the equivalence gate, computes summary deltas,
/// delegates trade reconciliation to Python's ``reconcile-trades`` endpoint,
/// and detects state-trace availability. Response shape is the canonical
/// snake_case wire shape from spec § 6.5; the Angular ``RunsCompareService``
/// consumes the same shape.
///
/// Backend uses minimal API throughout (see ``Backend/StudiesApi.cs``,
/// ``Backend/BacktestRunsApi.cs``); this file lives at the plan's named
/// location (<c>Backend/Controllers/CompareController.cs</c>) for clarity
/// even though it is not a class-based MVC controller.
/// </summary>
public static class CompareController
{
    public static void MapCompareEndpoints(this WebApplication app)
    {
        var group = app.MapGroup("/api/runs").WithTags("Compare");
        group.MapGet("/compare", CompareAsync);
    }

    private static async Task<IResult> CompareAsync(
        int left,
        int right,
        AppDbContext db,
        RunCompareService compareService,
        IHttpClientFactory httpFactory,
        ILogger<CompareEndpointMarker> logger,
        CancellationToken ct)
    {
        var pythonClient = httpFactory.CreateClient("python");
        var result = await BuildCompareAsync(left, right, db, compareService, pythonClient, logger, ct);

        if (result is null)
        {
            return Results.NotFound(new { error = "one or both runs not found", left, right });
        }

        return Results.Ok(result);
    }

    /// <summary>
    /// PR B Phase 4 — controller body extracted as a pure method so unit
    /// tests can drive it without spinning up the ASP.NET pipeline. Returns
    /// <c>null</c> when either run id is missing from the DB (the
    /// minimal-API wrapper translates that to a 404). All other paths
    /// produce a well-formed <see cref="CompareResponse"/>; transport
    /// failures against the Python reconcile-trades endpoint degrade to an
    /// empty <see cref="TradeDiff"/> so the compatibility verdict still
    /// surfaces.
    /// </summary>
    internal static async Task<CompareResponse?> BuildCompareAsync(
        int left,
        int right,
        AppDbContext db,
        RunCompareService compareService,
        HttpClient pythonClient,
        ILogger logger,
        CancellationToken ct)
    {
        var leftRow = await LoadRowAsync(db, left, ct);
        var rightRow = await LoadRowAsync(db, right, ct);

        if (leftRow is null || rightRow is null)
        {
            return null;
        }

        var compat = compareService.EvaluateCompatibility(leftRow, rightRow);
        var deltas = compareService.ComputeSummaryDeltas(leftRow, rightRow);
        var stateTrace = compareService.DetectStateTrace(leftRow, rightRow);

        TradeDiff tradeDiff;
        try
        {
            tradeDiff = await compareService.ReconcileTrades(pythonClient, leftRow, rightRow, ct);
        }
        catch (Exception ex) when (ex is HttpRequestException || ex is JsonException || ex is InvalidOperationException)
        {
            logger.LogError(ex, "[COMPARE] reconcile-trades call to Python failed");
            tradeDiff = new TradeDiff(
                MatchedPairs: new List<MatchedTradePair>(),
                PythonOnly: new List<UnmatchedTrade>(),
                LeanOnly: new List<UnmatchedTrade>(),
                FirstDivergence: null);
        }

        return new CompareResponse(
            Left: BuildSummary(leftRow),
            Right: BuildSummary(rightRow),
            Compatible: compat.Compatible,
            Mismatches: compat.Mismatches,
            InformationalDifferences: compat.InformationalDifferences,
            SummaryDeltas: deltas,
            TradeDiff: tradeDiff,
            FirstDivergence: tradeDiff.FirstDivergence,
            StateTraceAvailable: stateTrace,
            RawRunLinks: BuildRawLinks(leftRow, rightRow));
    }

    private static async Task<StrategyExecution?> LoadRowAsync(AppDbContext db, int id, CancellationToken ct)
    {
        return await db.StrategyExecutions
            .Include(s => s.Trades)
            .AsNoTracking()
            .SingleOrDefaultAsync(s => s.Id == id, ct);
    }

    private static RunSummary BuildSummary(StrategyExecution row)
    {
        var engine = EngineExtensions.FromSource(row.Source).ToString();
        var dataPolicy = TryParseDataPolicy(row.DataPolicyJson);
        var summary = new RunSummaryStats(
            TotalTrades: row.TotalTrades,
            TotalPnL: row.TotalPnL,
            TotalFees: row.TotalFees,
            WinRate: row.WinRate,
            MaxDrawdown: row.MaxDrawdown,
            Sharpe: row.SharpeRatio);
        var identity = new StrategyIdentity(
            Kind: engine == "LEAN" ? "lean_template" : "python_registry",
            Name: row.StrategyName,
            Sha256: null);

        return new RunSummary(
            Id: row.Id,
            Engine: engine,
            DataPolicy: dataPolicy,
            Summary: summary,
            StartingCash: row.InitialCash,
            CommissionPerOrder: (row.CommissionPerOrder ?? 0m).ToString(System.Globalization.CultureInfo.InvariantCulture),
            FillMode: row.FillMode,
            BrokeragePolicy: row.BrokeragePolicy,
            StrategyIdentity: identity);
    }

    private static RawRunLinks BuildRawLinks(StrategyExecution left, StrategyExecution right)
    {
        // v1: only LEAN runs persist a workspace path / manifest path. Until
        // Phase 5 wires the columns onto StrategyExecution, both sides return
        // nulls — the UI hides the Raw Run Links section in that case.
        return new RawRunLinks(
            Left: new RawRunSide(ManifestPath: null, LogPath: null, StagedZipSha256: null),
            Right: new RawRunSide(ManifestPath: null, LogPath: null, StagedZipSha256: null));
    }

    private static DataPolicyDto? TryParseDataPolicy(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return null;
        }

        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            return new DataPolicyDto(
                Source: root.GetProperty("source").GetString() ?? "",
                Symbol: root.GetProperty("symbol").GetString() ?? "",
                Adjusted: root.GetProperty("adjusted").GetBoolean(),
                Session: root.GetProperty("session").GetString() ?? "",
                InputBars: ParseBars(root.GetProperty("input_bars")),
                StrategyBars: ParseBars(root.GetProperty("strategy_bars")),
                TimestampPolicy: root.GetProperty("timestamp_policy").GetString() ?? "",
                Timezone: root.GetProperty("timezone").GetString() ?? "",
                ProviderKind: root.GetProperty("provider_kind").GetString() ?? "",
                FixtureId: root.TryGetProperty("fixture_id", out var fid) && fid.ValueKind != JsonValueKind.Null ? fid.GetString() : null,
                FixtureSha256: root.TryGetProperty("fixture_sha256", out var fsh) && fsh.ValueKind != JsonValueKind.Null ? fsh.GetString() : null);
        }
        catch (JsonException)
        {
            return null;
        }
        catch (System.Collections.Generic.KeyNotFoundException)
        {
            return null;
        }
    }

    private static BarsSpecDto ParseBars(JsonElement el) => new(
        Timespan: el.GetProperty("timespan").GetString() ?? "",
        Multiplier: el.GetProperty("multiplier").GetInt32());
}

/// <summary>
/// PR B Phase 4 — marker type used only as the generic argument to
/// <see cref="ILogger{T}"/> in the minimal-API endpoint above so the log
/// category name is stable across renames.
/// </summary>
public sealed class CompareEndpointMarker { }
