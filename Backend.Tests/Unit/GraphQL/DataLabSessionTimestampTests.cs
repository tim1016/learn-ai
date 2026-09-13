using Backend.GraphQL;
using Backend.Models.DataLab;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.GraphQL;

public class DataLabSessionTimestampTests
{
    private static DataLabSessionInput NewInput(string fromDate = "2026-01-05", string toDate = "2026-02-06") => new()
    {
        Name = "test session",
        Ticker = "AAPL",
        FromDate = fromDate,
        ToDate = toDate,
        Session = "rth",
        ForwardFill = true,
        Adjusted = true,
        EntriesJson = "[]",
    };

    [Fact]
    public async Task SaveDataLabSession_DerivesNumericTimestampFields_FromLegacyInputs()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();

        var result = await mutation.SaveDataLabSession(context, NewInput());

        Assert.True(result.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == result.Id!.Value);
        Assert.NotNull(session.CreatedMsUtc);
        Assert.NotNull(session.UpdatedMsUtc);
        // Interim derivation: legacy date-only strings parsed as UTC midnight.
        Assert.Equal(1767571200000L, session.WindowStartMsUtc); // 2026-01-05T00:00:00Z
        Assert.Equal(1770336000000L, session.WindowEndMsUtc);   // 2026-02-06T00:00:00Z
        // Legacy dual-read fields remain populated.
        Assert.Equal(new DateTime(2026, 1, 5, 0, 0, 0, DateTimeKind.Utc),
            DateTime.Parse(session.FromDate).Date);
        Assert.True(session.CreatedAt != default);
        Assert.True(session.UpdatedAt != default);
    }

    [Fact]
    public async Task SaveDataLabSession_ExplicitNumericInput_OverridesDerivation()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var input = NewInput();
        input.WindowStartMsUtc = 1L;
        input.WindowEndMsUtc = 2L;
        input.CreatedMsUtc = 3L;

        var result = await mutation.SaveDataLabSession(context, input);

        Assert.True(result.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == result.Id!.Value);
        Assert.Equal(1L, session.WindowStartMsUtc);
        Assert.Equal(2L, session.WindowEndMsUtc);
        Assert.Equal(3L, session.CreatedMsUtc);
        Assert.NotNull(session.UpdatedMsUtc);
    }

    [Fact]
    public async Task UpdateDataLabSession_KeepsNumericFieldsConsistent()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var saveResult = await mutation.SaveDataLabSession(context, NewInput());

        var updated = await mutation.UpdateDataLabSession(
            context, saveResult.Id!.Value, NewInput(fromDate: "2026-03-02", toDate: "2026-03-31"));

        Assert.True(updated.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == saveResult.Id.Value);
        // Window re-derives from the new legacy dates (UTC midnight).
        Assert.Equal(1772409600000L, session.WindowStartMsUtc); // 2026-03-02T00:00:00Z
        Assert.Equal(1774915200000L, session.WindowEndMsUtc);   // 2026-03-31T00:00:00Z
        // Server write time; >= (not >) because save and update can share a millisecond.
        Assert.True(session.UpdatedMsUtc >= session.CreatedMsUtc);
        // Legacy columns stay populated and move with the numeric authority.
        Assert.Equal("2026-03-02", session.FromDate);
        Assert.Equal("2026-03-31", session.ToDate);
    }

    [Fact]
    public async Task UpdateDataLabSession_LegacySessionWithoutNumericFields_BackfillsCreatedMs()
    {
        using var context = TestDbContextFactory.Create();
        var legacy = new DataLabSession
        {
            Id = Guid.NewGuid(),
            Name = "legacy",
            Ticker = "AAPL",
            FromDate = "2025-12-01",
            ToDate = "2025-12-31",
            CreatedAt = new DateTime(2025, 12, 1, 9, 0, 0, DateTimeKind.Utc),
            UpdatedAt = new DateTime(2025, 12, 1, 9, 0, 0, DateTimeKind.Utc),
        };
        context.DataLabSessions.Add(legacy);
        await context.SaveChangesAsync();

        var mutation = new DataLabMutation();
        var updated = await mutation.UpdateDataLabSession(context, legacy.Id, NewInput(fromDate: "2025-12-01", toDate: "2025-12-31"));

        Assert.True(updated.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == legacy.Id);
        Assert.Equal(new DateTimeOffset(new DateTime(2025, 12, 1, 9, 0, 0, DateTimeKind.Utc), TimeSpan.Zero)
            .ToUnixTimeMilliseconds(), session.CreatedMsUtc);
        Assert.NotNull(session.UpdatedMsUtc);
        Assert.NotNull(session.WindowStartMsUtc);
        Assert.NotNull(session.WindowEndMsUtc);
    }

    [Fact]
    public async Task LegacySessionWithoutNumericFields_StillReadsViaLegacyFields()
    {
        using var context = TestDbContextFactory.Create();
        var created = new DateTime(2025, 6, 15, 12, 0, 0, DateTimeKind.Utc);
        var legacy = new DataLabSession
        {
            Id = Guid.NewGuid(),
            Name = "legacy read",
            Ticker = "MSFT",
            FromDate = "2025-06-01",
            ToDate = "2025-06-30",
            CreatedAt = created,
            UpdatedAt = created,
        };
        context.DataLabSessions.Add(legacy);
        await context.SaveChangesAsync();

        var query = new DataLabQuery();
        var session = await query.GetDataLabSession(context, legacy.Id).FirstOrDefaultAsync();

        Assert.NotNull(session);
        Assert.Null(session!.WindowStartMsUtc);
        Assert.Null(session.WindowEndMsUtc);
        Assert.Null(session.CreatedMsUtc);
        Assert.Null(session.UpdatedMsUtc);
        Assert.Equal(created, session.CreatedAt);
        Assert.Equal(created, session.UpdatedAt);
        Assert.Equal("2025-06-01", session.FromDate);
        Assert.Equal("2025-06-30", session.ToDate);
    }

    // ── Timestamp validation ──────────────────────────────────

    private const long MaxTimestampMsUtc = 253_402_300_799_999; // mirrors PythonDataService MAX_TIMESTAMP_MS

    [Fact]
    public async Task SaveDataLabSession_RejectsInvertedWindow()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var input = NewInput();
        input.WindowStartMsUtc = 1770336000000L; // 2026-02-06
        input.WindowEndMsUtc = 1767571200000L;   // 2026-01-05 (earlier)

        var result = await mutation.SaveDataLabSession(context, input);

        Assert.False(result.Success);
        Assert.Contains("windowStartMsUtc", result.Message);
        Assert.Empty(await context.DataLabSessions.ToListAsync());
    }

    [Theory]
    [InlineData(-1L)]
    [InlineData(MaxTimestampMsUtc + 1)]
    [InlineData(long.MaxValue)]
    public async Task SaveDataLabSession_RejectsOutOfRangeWindowStart(long value)
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var input = NewInput();
        input.WindowStartMsUtc = value;
        input.WindowEndMsUtc = value + 1_000_000; // window ordering itself is fine

        var result = await mutation.SaveDataLabSession(context, input);

        Assert.False(result.Success);
        Assert.Contains("windowStartMsUtc", result.Message);
    }

    [Theory]
    [InlineData(-1L)]
    [InlineData(MaxTimestampMsUtc + 1)]
    public async Task SaveDataLabSession_RejectsOutOfRangeCreatedMs(long value)
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var input = NewInput();
        input.CreatedMsUtc = value;

        var result = await mutation.SaveDataLabSession(context, input);

        Assert.False(result.Success);
        Assert.Contains("createdMsUtc", result.Message);
    }

    [Fact]
    public async Task UpdateDataLabSession_RejectsInvertedWindow()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var saveResult = await mutation.SaveDataLabSession(context, NewInput());

        var input = NewInput();
        input.WindowStartMsUtc = 2000L;
        input.WindowEndMsUtc = 1000L;
        var updated = await mutation.UpdateDataLabSession(context, saveResult.Id!.Value, input);

        Assert.False(updated.Success);
        Assert.Contains("windowStartMsUtc", updated.Message);
        // The stored session is untouched by the rejected update.
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == saveResult.Id.Value);
        Assert.Equal(1767571200000L, session.WindowStartMsUtc);
        Assert.Equal(1770336000000L, session.WindowEndMsUtc);
    }

    [Fact]
    public async Task UpdateDataLabSession_RejectsOutOfRangeTimestamp()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var saveResult = await mutation.SaveDataLabSession(context, NewInput());

        var input = NewInput();
        input.WindowEndMsUtc = long.MaxValue;
        var updated = await mutation.UpdateDataLabSession(context, saveResult.Id!.Value, input);

        Assert.False(updated.Success);
        Assert.Contains("windowEndMsUtc", updated.Message);
    }

    [Fact]
    public async Task UpdateDataLabSession_IgnoresCreatedMsUtcInput()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var input = NewInput();
        input.CreatedMsUtc = 1000L; // create-only: honored on save
        var saveResult = await mutation.SaveDataLabSession(context, input);

        var updateInput = NewInput();
        updateInput.CreatedMsUtc = 999L; // must be ignored on update
        var updated = await mutation.UpdateDataLabSession(context, saveResult.Id!.Value, updateInput);

        Assert.True(updated.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == saveResult.Id.Value);
        Assert.Equal(1000L, session.CreatedMsUtc);
    }

    [Fact]
    public async Task UpdateDataLabSession_UpdatedMsUtcIsServerOwned()
    {
        using var context = TestDbContextFactory.Create();
        var mutation = new DataLabMutation();
        var saveResult = await mutation.SaveDataLabSession(context, NewInput());

        var updateInput = NewInput();
        updateInput.UpdatedMsUtc = 42L; // must be ignored: server sets write time
        var updated = await mutation.UpdateDataLabSession(context, saveResult.Id!.Value, updateInput);

        Assert.True(updated.Success);
        var session = await context.DataLabSessions.AsNoTracking().SingleAsync(s => s.Id == saveResult.Id.Value);
        Assert.NotNull(session.UpdatedMsUtc);
        Assert.NotEqual(42L, session.UpdatedMsUtc);
        Assert.True(session.UpdatedMsUtc >= session.CreatedMsUtc);
    }
}
