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
    /// Paginated list of strategy executions, optionally filtered by engine source and/or symbol.
    /// Returns a cursor-based connection ordered newest-first.
    /// </summary>
    [GraphQLName("backtestRuns")]
    [UsePaging(MaxPageSize = 100, DefaultPageSize = 25)]
    public IQueryable<StrategyExecution> GetBacktestRuns(
        AppDbContext context,
        EngineSource? engine = null,
        string? symbol = null)
    {
        var query = context.StrategyExecutions
            .Include(s => s.Trades)
            .AsNoTracking()
            .AsQueryable();

        if (engine.HasValue)
        {
            var dbValue = engine.Value.ToDbValue();
            query = query.Where(s => s.Source == dbValue);
        }

        if (!string.IsNullOrEmpty(symbol))
        {
            // Parameters is a JSON string, e.g. {"symbol":"SPY","starting_cash":100000}.
            // Simple Contains is safe because symbol is a bounded uppercase ticker string.
            query = query.Where(s => s.Parameters != null && s.Parameters.Contains(symbol));
        }

        return query.OrderByDescending(s => s.ExecutedAt);
    }
}
