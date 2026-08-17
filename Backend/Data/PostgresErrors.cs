using Microsoft.EntityFrameworkCore;

namespace Backend.Data;

/// <summary>
/// Classifies <see cref="DbUpdateException"/>s by Postgres SQL state.
/// Extracted from <c>BacktestRunPersistenceService</c>'s original private
/// helper so every service that races a unique constraint under
/// concurrent writers (e.g. <c>RecencyPersistenceService</c>) shares one
/// classifier instead of re-implementing it.
/// </summary>
public static class PostgresErrors
{
    public static bool IsUniqueViolation(DbUpdateException ex)
    {
        // Npgsql: SqlState 23505 == unique_violation
        return ex.InnerException is Npgsql.PostgresException pg && pg.SqlState == "23505";
    }
}
