using System.Text;
using System.Text.Json;
using Backend.Configuration;
using Microsoft.Extensions.Options;
using Quartz;

namespace Backend.Jobs;

/// <summary>
/// Per-slot IV recorder job — Step D follow-up of the IV-ownership plan.
///
/// One <see cref="IvRecorderJob"/> instance executes per fired trigger.
/// Each trigger carries the slot string (e.g. <c>"09:35"</c>) in its
/// JobDataMap; the job iterates the configured tickers and POSTs to
/// the Python <c>/api/iv-recorder/snapshot</c> endpoint for each
/// (ticker, slot) pair.
///
/// <para>
/// <see cref="DisallowConcurrentExecutionAttribute"/> ensures slots that
/// fire close together (or that slip on heavy load) don't pile up — the
/// late slot waits for the earlier one to complete. The Python endpoint
/// itself is idempotent on (ticker, snapshot_ts_ms), so a duplicate fire
/// is harmless.
/// </para>
///
/// <para>
/// Per-ticker errors are logged and continue; one bad symbol does not
/// abort the whole slot. The Python endpoint already writes an
/// error-tagged audit row when Polygon or the solver fails, so the
/// recorder side captures *why* a slot is missing without retry logic
/// here.
/// </para>
/// </summary>
[DisallowConcurrentExecution]
public class IvRecorderJob : IJob
{
    /// <summary>
    /// Key under which the trigger's <c>JobDataMap</c> carries the slot
    /// string (e.g. <c>"09:35"</c>).
    /// </summary>
    public const string SlotJobDataKey = "slot";

    private const string PythonClientName = "python";
    private const string SnapshotPath = "/api/iv-recorder/snapshot";

    private readonly IHttpClientFactory _httpFactory;
    private readonly IOptions<IvRecorderOptions> _options;
    private readonly ILogger<IvRecorderJob> _logger;

    public IvRecorderJob(
        IHttpClientFactory httpFactory,
        IOptions<IvRecorderOptions> options,
        ILogger<IvRecorderJob> logger)
    {
        _httpFactory = httpFactory ?? throw new ArgumentNullException(nameof(httpFactory));
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public async Task Execute(IJobExecutionContext context)
    {
        // Quartz's GetString throws KeyNotFoundException on a missing key, so
        // gate on ContainsKey first — a misconfigured trigger should log and
        // exit, not throw.
        var slot = context.MergedJobDataMap.ContainsKey(SlotJobDataKey)
            ? context.MergedJobDataMap.GetString(SlotJobDataKey)
            : null;
        if (string.IsNullOrWhiteSpace(slot))
        {
            _logger.LogError("[IvRecorder] trigger fired without a slot in JobDataMap");
            return;
        }

        var options = _options.Value;
        if (options.Tickers.Count == 0)
        {
            _logger.LogWarning("[IvRecorder] {Slot}: no tickers configured; skipping", slot);
            return;
        }

        var http = _httpFactory.CreateClient(PythonClientName);
        var ct = context.CancellationToken;

        foreach (var ticker in options.Tickers)
        {
            await PostOneAsync(http, ticker, slot, options.TargetCalendarDays, ct);
        }
    }

    private async Task PostOneAsync(
        HttpClient http,
        string ticker,
        string slot,
        int targetCalendarDays,
        CancellationToken ct)
    {
        var payload = JsonSerializer.Serialize(new
        {
            ticker,
            slot,
            target_calendar_days = targetCalendarDays,
        });

        try
        {
            using var content = new StringContent(payload, Encoding.UTF8, "application/json");
            var resp = await http.PostAsync(SnapshotPath, content, ct);

            if (resp.IsSuccessStatusCode)
            {
                _logger.LogInformation(
                    "[IvRecorder] {Ticker} {Slot}: ok ({Status})",
                    ticker, slot, (int)resp.StatusCode);
            }
            else
            {
                var body = await resp.Content.ReadAsStringAsync(ct);
                _logger.LogError(
                    "[IvRecorder] {Ticker} {Slot}: failed ({Status}) {Body}",
                    ticker, slot, (int)resp.StatusCode, body);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Host shutting down — propagate as cancellation.
            throw;
        }
        catch (Exception exc)
        {
            _logger.LogError(
                exc,
                "[IvRecorder] {Ticker} {Slot}: exception during dispatch",
                ticker, slot);
        }
    }
}
