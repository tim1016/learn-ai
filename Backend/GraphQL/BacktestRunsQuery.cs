using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.MarketData;
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
    public IQueryable<StrategyExecution> GetBacktestRuns(
        AppDbContext context,
        Engine? engine = null,
        string? symbol = null)
    {
        var query = context.StrategyExecutions
            .Include(s => s.Trades)
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

        return query.OrderByDescending(s => s.ExecutedAt);
    }
}
