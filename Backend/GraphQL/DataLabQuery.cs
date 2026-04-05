using Backend.Data;
using Backend.Models.DataLab;
using HotChocolate;
using HotChocolate.Data;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

[ExtendObjectType(typeof(Query))]
public class DataLabQuery
{
    /// <summary>
    /// List all Data Lab sessions, most recently updated first.
    /// Returns lightweight summaries (no chart snapshot payload).
    /// </summary>
    [GraphQLName("dataLabSessions")]
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<DataLabSession> GetDataLabSessions(AppDbContext context)
        => context.DataLabSessions.OrderByDescending(s => s.UpdatedAt);

    /// <summary>
    /// Get a single Data Lab session by ID, including the full chart snapshot.
    /// </summary>
    [GraphQLName("dataLabSession")]
    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<DataLabSession?> GetDataLabSession(AppDbContext context, Guid id)
        => context.DataLabSessions.Where(s => s.Id == id);
}
