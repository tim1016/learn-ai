using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.MarketData;
using Backend.Temporal;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

[ExtendObjectType<Query>]
public class BacktestRunsQuery
{
    /// <summary>
    /// Paginated list of strategy executions, optionally filtered by engine
    /// (PR B unified <see cref="Engine"/> enum) and/or symbol. Returns a
    /// cursor-based connection ordered newest-first.
    /// </summary>
    /// <remarks>
    /// PR B (2026-05-19) — the previous resolver accepted the 3-state
    /// <c>EngineSource</c> enum (ENGINE / STRATEGY_LAB / LEAN_SIDECAR), one
    /// per literal database <see cref="StrategyExecution.Source"/> string.
    /// The unified Engine Lab surface only needs to distinguish the two
    /// engine identities (Python vs LEAN), so the filter argument now uses
    /// the 2-state <see cref="Engine"/> enum: <see cref="Engine.PYTHON"/>
    /// covers both <c>"engine"</c> and <c>"strategy-lab"</c> rows;
    /// <see cref="Engine.LEAN"/> covers <c>"lean-sidecar"</c>.
    /// </remarks>
    [GraphQLName("backtestRuns")]
    [UsePaging(MaxPageSize = 100, DefaultPageSize = 25)]
    public IQueryable<BacktestRunNodeType> GetBacktestRuns(
        AppDbContext context,
        Engine? engine = null,
        string? symbol = null)
    {
        var query = context.StrategyExecutions
            .AsNoTracking()
            .AsQueryable();

        if (engine.HasValue)
        {
            // PYTHON spans the two database string values that share the
            // Python engine identity. LEAN is a single string. Two equality
            // checks against constants keep the SQL trivially indexable.
            switch (engine.Value)
            {
                case Engine.PYTHON:
                    query = query.Where(s => s.Source == "engine" || s.Source == "strategy-lab");
                    break;
                case Engine.LEAN:
                    query = query.Where(s => s.Source == "lean-sidecar");
                    break;
            }
        }

        if (!string.IsNullOrEmpty(symbol))
        {
            // Parameters is a JSON text string, e.g. {"symbol":"SPY","starting_cash":100000}.
            // Match the exact JSON key/value pair to avoid prefix false-positives ("SP" matching "SPY").
            var jsonFragment = $"\"symbol\":\"{symbol}\"";
            query = query.Where(s => s.Parameters != null && s.Parameters.Contains(jsonFragment));
        }

        return query
            .OrderByDescending(s => s.ExecutedAt)
            // Keep this SQL-translatable projection aligned with
            // BacktestRunNodeType.FromExecution below. The materialized path can
            // call EngineExtensions.FromSource; this projection keeps the same
            // source mapping inline so EF can translate it.
            .Select(s => new BacktestRunNodeType
            {
                Id = s.Id,
                Source = s.Source,
                Engine = s.Source == "lean-sidecar" ? Engine.LEAN : Engine.PYTHON,
                StrategyName = s.StrategyName,
                LeanRunId = s.LeanRunId,
                Parameters = s.Parameters,
                StartDate = s.StartDate,
                EndDate = s.EndDate,
                ExecutedAtUtc = s.ExecutedAt,
                TotalTrades = s.TotalTrades,
                TotalPnL = s.TotalPnL,
                CommissionPerOrder = s.CommissionPerOrder,
                BrokeragePolicy = s.BrokeragePolicy,
                Notes = s.Notes,
                DataPolicyJson = s.DataPolicyJson,
                VerdictGrade = s.VerdictGrade,
                VerdictSignal = s.VerdictSignal,
                ParityGroupId = s.ParityGroupId,
                Trades = s.Trades.Select(t => new BacktestRunTradeSummaryType
                {
                    IsSyntheticExit = t.IsSyntheticExit,
                }).ToList(),
            });
    }
}

public sealed record BacktestRunNodeType
{
    public int Id { get; init; }
    public string Source { get; init; } = "";
    public Engine Engine { get; init; }
    public string StrategyName { get; init; } = "";
    public string? LeanRunId { get; init; }
    public string? Parameters { get; init; }
    public string StartDate { get; init; } = "";
    public string EndDate { get; init; } = "";
    [GraphQLIgnore]
    public DateTime ExecutedAtUtc { get; init; }
    public long ExecutedAt => UnixMs.FromUtc(ExecutedAtUtc);
    public int TotalTrades { get; init; }
    [GraphQLName("totalPnL")]
    public decimal TotalPnL { get; init; }
    public decimal? CommissionPerOrder { get; init; }
    public string? BrokeragePolicy { get; init; }
    public string? Notes { get; init; }
    [GraphQLIgnore]
    public string? DataPolicyJson { get; init; }
    public DataPolicyType? DataPolicy => DataPolicyType.TryParse(DataPolicyJson);
    public string? VerdictGrade { get; init; }
    public string? VerdictSignal { get; init; }
    public string? ParityGroupId { get; init; }
    public IReadOnlyList<BacktestRunTradeSummaryType> Trades { get; init; } = [];

    // Keep this materialized mapping aligned with GetBacktestRuns' projection.
    public static BacktestRunNodeType FromExecution(StrategyExecution execution) => new()
    {
        Id = execution.Id,
        Source = execution.Source,
        Engine = EngineExtensions.FromSource(execution.Source),
        StrategyName = execution.StrategyName,
        LeanRunId = execution.LeanRunId,
        Parameters = execution.Parameters,
        StartDate = execution.StartDate,
        EndDate = execution.EndDate,
        ExecutedAtUtc = execution.ExecutedAt,
        TotalTrades = execution.TotalTrades,
        TotalPnL = execution.TotalPnL,
        CommissionPerOrder = execution.CommissionPerOrder,
        BrokeragePolicy = execution.BrokeragePolicy,
        Notes = execution.Notes,
        DataPolicyJson = execution.DataPolicyJson,
        VerdictGrade = execution.VerdictGrade,
        VerdictSignal = execution.VerdictSignal,
        ParityGroupId = execution.ParityGroupId,
        Trades = execution.Trades.Select(t => new BacktestRunTradeSummaryType
        {
            IsSyntheticExit = t.IsSyntheticExit,
        }).ToList(),
    };
}

public sealed record BacktestRunTradeSummaryType
{
    public bool IsSyntheticExit { get; init; }
}
