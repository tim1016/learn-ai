using Backend.Configuration;
using Quartz;

namespace Backend.Jobs;

/// <summary>
/// Wires the <see cref="IvRecorderJob"/> into the Quartz scheduler with
/// one CronTrigger per configured slot. Step D follow-up of the
/// IV-ownership plan.
///
/// <para>
/// The schedule lives in <c>America/New_York</c> with DST transitions
/// handled by Quartz's <c>CronScheduleBuilder.InTimeZone</c>. Triggers
/// fire Mon–Fri only; equity-options markets are closed on weekends,
/// and on holidays the Python endpoint will write an error-tagged row
/// (acceptable audit-trail behavior; the cost of a wasted fire is the
/// same shape as a recorder failure).
/// </para>
///
/// <para>
/// The cron is opt-in: <c>IvRecorder:Enabled = false</c> by default.
/// Dev / CI environments don't fire crons against external Polygon
/// endpoints unless the operator explicitly enables it. Production
/// sets <c>Enabled = true</c> via environment variable or a deployed
/// <c>appsettings.Production.json</c>.
/// </para>
/// </summary>
public static class IvRecorderRegistration
{
    private const string TimeZoneId = "America/New_York";
    private const string JobIdentity = "iv-recorder";

    /// <summary>
    /// Add Quartz, the <see cref="IvRecorderJob"/>, and one trigger per
    /// configured slot. Safe to call when <c>IvRecorder:Enabled = false</c>;
    /// it binds the options class but skips the scheduler wire-up.
    /// </summary>
    public static IServiceCollection AddIvRecorder(
        this IServiceCollection services,
        IConfiguration configuration)
    {
        services.Configure<IvRecorderOptions>(
            configuration.GetSection(IvRecorderOptions.SectionName));

        var options = new IvRecorderOptions();
        configuration.GetSection(IvRecorderOptions.SectionName).Bind(options);

        if (!options.Enabled)
        {
            return services;
        }

        if (options.Slots.Count == 0)
        {
            throw new InvalidOperationException(
                "IvRecorder is enabled but no slots are configured. " +
                "Set IvRecorder:Slots to a non-empty list of HH:mm ET times " +
                "(e.g. [\"09:35\", \"12:30\", \"16:00\"]).");
        }

        if (options.Tickers.Count == 0)
        {
            throw new InvalidOperationException(
                "IvRecorder is enabled but no tickers are configured. " +
                "Set IvRecorder:Tickers to a non-empty list (e.g. [\"SPY\"]).");
        }

        var easternTz = TimeZoneInfo.FindSystemTimeZoneById(TimeZoneId);

        services.AddQuartz(q =>
        {
            var jobKey = new JobKey(JobIdentity);
            q.AddJob<IvRecorderJob>(opts => opts
                .WithIdentity(jobKey)
                .StoreDurably());

            foreach (var slot in options.Slots)
            {
                var cron = SlotToCronExpression(slot);
                q.AddTrigger(t => t
                    .ForJob(jobKey)
                    .WithIdentity($"{JobIdentity}-{slot}")
                    .UsingJobData(IvRecorderJob.SlotJobDataKey, slot)
                    .WithCronSchedule(cron, x => x.InTimeZone(easternTz)));
            }
        });

        services.AddQuartzHostedService(q =>
        {
            // Wait for in-flight jobs to finish on shutdown — a Polygon
            // POST takes seconds, the host can absorb the wait, and an
            // abandoned mid-flight POST leaves the recorder's audit
            // trail with no row at all (worse than a clean error row).
            q.WaitForJobsToComplete = true;
        });

        return services;
    }

    /// <summary>
    /// Convert <c>"HH:mm"</c> to a Quartz cron expression that fires
    /// Mon–Fri. Quartz cron has 7 fields (sec min hour DOM mon DOW),
    /// with <c>?</c> on DOM when DOW is set.
    /// </summary>
    internal static string SlotToCronExpression(string slot)
    {
        var parts = slot.Split(':');
        if (parts.Length != 2
            || !int.TryParse(parts[0], out var hour)
            || !int.TryParse(parts[1], out var minute)
            || hour < 0 || hour > 23
            || minute < 0 || minute > 59)
        {
            throw new ArgumentException(
                $"slot must be HH:mm in 24-hour format, got '{slot}'",
                nameof(slot));
        }
        return $"0 {minute} {hour} ? * MON-FRI";
    }
}
