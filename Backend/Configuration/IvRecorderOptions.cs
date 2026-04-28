namespace Backend.Configuration;

/// <summary>
/// Configuration for the IV recorder cron.
///
/// See <c>docs/architecture/iv-ownership-research.md</c> §7.5 for the
/// .NET-owned-cron rationale, §7.6 for the slot schedule, and §9 for the
/// queued 15:55 slot experiment.
///
/// The .NET host owns the schedule; each slot fires a Quartz job that
/// POSTs to the Python <c>/api/iv-recorder/snapshot</c> endpoint per
/// configured ticker. Tickers and slots are config-driven so expanding
/// from SPY-only to SPY/QQQ/IWM/DIA after burn-in is one settings change.
/// </summary>
public class IvRecorderOptions
{
    public const string SectionName = "IvRecorder";

    /// <summary>
    /// Underlying tickers to record per slot. Start with SPY only; expand
    /// after 30 sessions of clean data validate the pipeline (research-doc
    /// §7 / §9).
    /// </summary>
    public List<string> Tickers { get; set; } = new();

    /// <summary>
    /// Daily snapshot times in <c>HH:mm</c> America/New_York wall-clock,
    /// Mon–Fri only. Default: 09:35 / 12:30 / 16:00 ET (research-doc §7.6).
    /// </summary>
    public List<string> Slots { get; set; } = new();

    /// <summary>
    /// Master switch. Default <c>false</c> so dev / CI environments
    /// don't fire crons against external Polygon endpoints unless the
    /// operator explicitly enables it.
    /// </summary>
    public bool Enabled { get; set; }

    /// <summary>
    /// Constant-maturity target passed to the Python recorder. 30 days
    /// is the standard IV30 horizon and matches the parametric / VIX-style
    /// endpoints; configurable in case a future tenor (e.g. IV60) wants
    /// its own cron.
    /// </summary>
    public int TargetCalendarDays { get; set; } = 30;
}
