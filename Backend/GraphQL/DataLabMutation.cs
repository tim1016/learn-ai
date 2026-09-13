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
                // Numeric authority: input values win when supplied, otherwise
                // derive (interim UTC-midnight derivation for the window).
                WindowStartMsUtc = input.WindowStartMsUtc ?? TryParseDateOnlyMsUtc(input.FromDate),
                WindowEndMsUtc = input.WindowEndMsUtc ?? TryParseDateOnlyMsUtc(input.ToDate),
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
            session.UpdatedAt = updatedNow;
            session.WindowStartMsUtc = input.WindowStartMsUtc ?? TryParseDateOnlyMsUtc(input.FromDate);
            session.WindowEndMsUtc = input.WindowEndMsUtc ?? TryParseDateOnlyMsUtc(input.ToDate);
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
    // strings (interim UTC-midnight derivation) and created/updated from now. ──

    public long? WindowStartMsUtc { get; set; }

    public long? WindowEndMsUtc { get; set; }

    public long? CreatedMsUtc { get; set; }

    public long? UpdatedMsUtc { get; set; }
}

public class DataLabSessionResult
{
    public bool Success { get; set; }
    public Guid? Id { get; set; }
    public string Message { get; set; } = "";
}
