using Microsoft.EntityFrameworkCore;

namespace Backend.Data;

/// <summary>
/// Classifies <see cref="DbUpdateException"/>s by Postgres SQL state, for
/// any service that races a unique constraint under concurrent writers
/// (today <c>BacktestRunPersistenceService</c>; the Recency writer moved to
/// the Python service, ADR 0057).
/// </summary>
public static class PostgresErrors
{
    public static bool IsUniqueViolation(DbUpdateException ex)
    {
        // Npgsql: SqlState 23505 == unique_violation
        return ex.InnerException is Npgsql.PostgresException pg && pg.SqlState == "23505";
    }
}
