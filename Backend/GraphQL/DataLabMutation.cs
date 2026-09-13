using Backend.Data;
using Backend.Models.DataLab;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

[ExtendObjectType(typeof(Mutation))]
public class DataLabMutation
{
    // ── int64 ms UTC helpers (additive timestamp migration, PRD §13) ──
    // Legacy DateTime/string columns remain written and read for dual-read
    // compatibility; numeric fields are authoritative when non-null.

    /// <summary>
    /// Upper bound for caller-supplied ms-UTC timestamps (9999-12-31T23:59:59.999Z).
    /// Mirrors MAX_TIMESTAMP_MS in PythonDataService/app/utils/session_anchors.py;
    /// deliberately far below Int64.MaxValue.
    /// </summary>
    private const long MaxTimestampMsUtc = 253_402_300_799_999;

    /// <summary>
    /// Validates caller-supplied numeric timestamps: each must be nonnegative and
    /// ≤ <see cref="MaxTimestampMsUtc"/>, and caller-provided window endpoints
    /// must be strictly increasing when both are supplied. Returns an error
    /// message, or null when valid.
    /// </summary>
    private static string? ValidateTimestamps(DataLabSessionInput input)
    {
        if (input.WindowStartMsUtc is < 0 or > MaxTimestampMsUtc)
            return $"windowStartMsUtc must be between 0 and {MaxTimestampMsUtc}";
        if (input.WindowEndMsUtc is < 0 or > MaxTimestampMsUtc)
            return $"windowEndMsUtc must be between 0 and {MaxTimestampMsUtc}";
        if (input.CreatedMsUtc is < 0 or > MaxTimestampMsUtc)
            return $"createdMsUtc must be between 0 and {MaxTimestampMsUtc}";
        // Window ordering is enforced only on caller-provided endpoints; the
        // legacy date-string derivation path keeps its existing behavior.
        if (input.WindowStartMsUtc.HasValue && input.WindowEndMsUtc.HasValue
            && input.WindowStartMsUtc.Value >= input.WindowEndMsUtc.Value)
            return "windowStartMsUtc must be strictly before windowEndMsUtc";
        return null;
    }

    private static long ToEpochMsUtc(DateTime utc) =>
        new DateTimeOffset(DateTime.SpecifyKind(utc, DateTimeKind.Utc), TimeSpan.Zero).ToUnixTimeMilliseconds();

    /// <summary>
    /// Interim window derivation: parse the legacy 10-char date-only string as
    /// UTC midnight. This is a bounded interim derivation only — the
    /// calendar-accurate window resolution lands in PythonDataService later.
    /// Returns null when the string is not a parseable date.
    /// </summary>
    private static long? TryParseDateOnlyMsUtc(string? dateOnly) =>
        DateTime.TryParse(
            dateOnly,
            null,
            System.Globalization.DateTimeStyles.AssumeUniversal | System.Globalization.DateTimeStyles.AdjustToUniversal,
            out var parsed)
            ? ToEpochMsUtc(DateTime.SpecifyKind(parsed.Date, DateTimeKind.Utc))
            : null;

    /// <summary>
    /// Save a new Data Lab session (config + optional chart snapshot).
    /// </summary>
    [GraphQLName("saveDataLabSession")]
    public async Task<DataLabSessionResult> SaveDataLabSession(
        AppDbContext context,
        DataLabSessionInput input)
    {
        try
        {
            // Numeric authority: input values win when supplied, otherwise
            // derive (interim UTC-midnight derivation for the window).
            var windowStartMsUtc = input.WindowStartMsUtc ?? TryParseDateOnlyMsUtc(input.FromDate);
            var windowEndMsUtc = input.WindowEndMsUtc ?? TryParseDateOnlyMsUtc(input.ToDate);
            var validationError = ValidateTimestamps(input);
            if (validationError != null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = validationError,
                };
            }

            var now = DateTime.UtcNow;
            var session = new DataLabSession
            {
                Id = Guid.NewGuid(),
                Name = input.Name,
                Ticker = input.Ticker,
                FromDate = input.FromDate,
                ToDate = input.ToDate,
                Session = input.Session,
                ForwardFill = input.ForwardFill,
                Adjusted = input.Adjusted,
                EntriesJson = input.EntriesJson,
                ChartSnapshotJson = input.ChartSnapshotJson,
                CreatedAt = now,
                UpdatedAt = now,
                WindowStartMsUtc = windowStartMsUtc,
                WindowEndMsUtc = windowEndMsUtc,
                // CreatedMsUtc is create-only: honored on save when provided,
                // else server time. UpdatedMsUtc is always server-owned.
                CreatedMsUtc = input.CreatedMsUtc ?? ToEpochMsUtc(now),
                UpdatedMsUtc = ToEpochMsUtc(now),
            };

            context.DataLabSessions.Add(session);
            await context.SaveChangesAsync();

            return new DataLabSessionResult
            {
                Success = true,
                Id = session.Id,
                Message = "Session saved",
            };
        }
        catch (Exception ex)
        {
            return new DataLabSessionResult
            {
                Success = false,
                Message = $"Error: {ex.Message}",
            };
        }
    }

    /// <summary>
    /// Update an existing Data Lab session (config, name, and/or chart snapshot).
    /// </summary>
    [GraphQLName("updateDataLabSession")]
    public async Task<DataLabSessionResult> UpdateDataLabSession(
        AppDbContext context,
        Guid id,
        DataLabSessionInput input)
    {
        try
        {
            var session = await context.DataLabSessions.FindAsync(id);
            if (session == null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = "Session not found",
                };
            }

            session.Name = input.Name;
            session.Ticker = input.Ticker;
            session.FromDate = input.FromDate;
            session.ToDate = input.ToDate;
            session.Session = input.Session;
            session.ForwardFill = input.ForwardFill;
            session.Adjusted = input.Adjusted;
            session.EntriesJson = input.EntriesJson;
            session.ChartSnapshotJson = input.ChartSnapshotJson;
            var updatedNow = DateTime.UtcNow;
            var windowStartMsUtc = input.WindowStartMsUtc ?? TryParseDateOnlyMsUtc(input.FromDate);
            var windowEndMsUtc = input.WindowEndMsUtc ?? TryParseDateOnlyMsUtc(input.ToDate);
            var validationError = ValidateTimestamps(input);
            if (validationError != null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = validationError,
                };
            }

            session.UpdatedAt = updatedNow;
            session.WindowStartMsUtc = windowStartMsUtc;
            session.WindowEndMsUtc = windowEndMsUtc;
            // CreatedMsUtc is create-only: on update the stored value is kept
            // (backfilled from CreatedAt for legacy rows); input.CreatedMsUtc
            // is ignored. UpdatedMsUtc is server-owned.
            session.CreatedMsUtc ??= ToEpochMsUtc(session.CreatedAt);
            session.UpdatedMsUtc = ToEpochMsUtc(updatedNow);

            await context.SaveChangesAsync();

            return new DataLabSessionResult
            {
                Success = true,
                Id = session.Id,
                Message = "Session updated",
            };
        }
        catch (Exception ex)
        {
            return new DataLabSessionResult
            {
                Success = false,
                Message = $"Error: {ex.Message}",
            };
        }
    }

    /// <summary>
    /// Update only the chart snapshot for a session (after re-fetching data).
    /// </summary>
    [GraphQLName("updateDataLabChartSnapshot")]
    public async Task<DataLabSessionResult> UpdateDataLabChartSnapshot(
        AppDbContext context,
        Guid id,
        string chartSnapshotJson)
    {
        try
        {
            var session = await context.DataLabSessions.FindAsync(id);
            if (session == null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = "Session not found",
                };
            }

            session.ChartSnapshotJson = chartSnapshotJson;
            var updatedNow = DateTime.UtcNow;
            session.UpdatedAt = updatedNow;
            session.CreatedMsUtc ??= ToEpochMsUtc(session.CreatedAt);
            session.UpdatedMsUtc = ToEpochMsUtc(updatedNow);

            await context.SaveChangesAsync();

            return new DataLabSessionResult
            {
                Success = true,
                Id = session.Id,
                Message = "Chart snapshot updated",
            };
        }
        catch (Exception ex)
        {
            return new DataLabSessionResult
            {
                Success = false,
                Message = $"Error: {ex.Message}",
            };
        }
    }

    /// <summary>
    /// Rename a session.
    /// </summary>
    [GraphQLName("renameDataLabSession")]
    public async Task<DataLabSessionResult> RenameDataLabSession(
        AppDbContext context,
        Guid id,
        string name)
    {
        try
        {
            var session = await context.DataLabSessions.FindAsync(id);
            if (session == null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = "Session not found",
                };
            }

            session.Name = name;
            var renamedAt = DateTime.UtcNow;
            session.UpdatedAt = renamedAt;
            session.CreatedMsUtc ??= ToEpochMsUtc(session.CreatedAt);
            session.UpdatedMsUtc = ToEpochMsUtc(renamedAt);

            await context.SaveChangesAsync();

            return new DataLabSessionResult
            {
                Success = true,
                Id = session.Id,
                Message = "Session renamed",
            };
        }
        catch (Exception ex)
        {
            return new DataLabSessionResult
            {
                Success = false,
                Message = $"Error: {ex.Message}",
            };
        }
    }

    /// <summary>
    /// Delete a Data Lab session.
    /// </summary>
    [GraphQLName("deleteDataLabSession")]
    public async Task<DataLabSessionResult> DeleteDataLabSession(
        AppDbContext context,
        Guid id)
    {
        try
        {
            var session = await context.DataLabSessions.FindAsync(id);
            if (session == null)
            {
                return new DataLabSessionResult
                {
                    Success = false,
                    Message = "Session not found",
                };
            }

            context.DataLabSessions.Remove(session);
            await context.SaveChangesAsync();

            return new DataLabSessionResult
            {
                Success = true,
                Id = id,
                Message = "Session deleted",
            };
        }
        catch (Exception ex)
        {
            return new DataLabSessionResult
            {
                Success = false,
                Message = $"Error: {ex.Message}",
            };
        }
    }
}

// ── Input / Result types ───────────────────────────────────

public class DataLabSessionInput
{
    public string Name { get; set; } = "";
    public string Ticker { get; set; } = "";
    public string FromDate { get; set; } = "";
    public string ToDate { get; set; } = "";
    public string Session { get; set; } = "rth";
    public bool ForwardFill { get; set; } = true;
    public bool Adjusted { get; set; } = true;
    public string EntriesJson { get; set; } = "[]";
    public string? ChartSnapshotJson { get; set; }

    // ── Optional int64 ms UTC overrides (additive timestamp migration, PRD §13).
    // When null, mutations derive the values: window from the legacy date-only
    // strings (interim UTC-midnight derivation) and created/updated from now.
    // Validation: supplied values must be within [0, 253402300799999]
    // (9999-12-31T23:59:59.999Z) and the window strictly increasing. ──

    [GraphQLDescription("Inclusive window start in int64 ms UTC. When null, derived from fromDate (interim UTC-midnight derivation). Must be within [0, 253402300799999] and strictly before windowEndMsUtc when both are set.")]
    public long? WindowStartMsUtc { get; set; }

    [GraphQLDescription("Inclusive window end in int64 ms UTC. When null, derived from toDate (interim UTC-midnight derivation). Must be within [0, 253402300799999] and strictly after windowStartMsUtc when both are set.")]
    public long? WindowEndMsUtc { get; set; }

    [GraphQLDescription("Create-only: session creation time in int64 ms UTC. Honored by saveDataLabSession when provided (within [0, 253402300799999]); ignored by updateDataLabSession, which keeps the stored value (or backfills it from the legacy createdAt).")]
    public long? CreatedMsUtc { get; set; }

    [GraphQLDescription("Server-owned: ignored on input. The server always sets updatedMsUtc to the write time.")]
    public long? UpdatedMsUtc { get; set; }
}

public class DataLabSessionResult
{
    public bool Success { get; set; }
    public Guid? Id { get; set; }
    public string Message { get; set; } = "";
}
