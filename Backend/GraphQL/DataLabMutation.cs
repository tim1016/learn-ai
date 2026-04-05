using Backend.Data;
using Backend.Models.DataLab;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

[ExtendObjectType(typeof(Mutation))]
public class DataLabMutation
{
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
            var session = new DataLabSession
            {
                Id = Guid.NewGuid(),
                Name = input.Name,
                Ticker = input.Ticker,
                FromDate = input.FromDate,
                ToDate = input.ToDate,
                Session = input.Session,
                ForwardFill = input.ForwardFill,
                EntriesJson = input.EntriesJson,
                ChartSnapshotJson = input.ChartSnapshotJson,
                CreatedAt = DateTime.UtcNow,
                UpdatedAt = DateTime.UtcNow,
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
            session.EntriesJson = input.EntriesJson;
            session.ChartSnapshotJson = input.ChartSnapshotJson;
            session.UpdatedAt = DateTime.UtcNow;

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
            session.UpdatedAt = DateTime.UtcNow;

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
            session.UpdatedAt = DateTime.UtcNow;

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
    public string EntriesJson { get; set; } = "[]";
    public string? ChartSnapshotJson { get; set; }
}

public class DataLabSessionResult
{
    public bool Success { get; set; }
    public Guid? Id { get; set; }
    public string Message { get; set; } = "";
}
