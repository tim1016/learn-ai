using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http.HttpResults;
using StackExchange.Redis;

namespace Backend.Jobs;

/// <summary>
/// Long-running job orchestration. Public surface for the Frontend; the
/// math runs in PythonDataService. We mint the job id, persist its state
/// in Redis, kick off Python, and stream back progress events from a
/// Redis Stream the worker writes to.
///
/// Redis schema (shared with PythonDataService/app/jobs/progress.py):
///   job:{id}:state    Hash   id, type, status, params, created_at, ...
///   job:{id}:events   Stream { event: JSON } per entry
///   job:{id}:result   String JSON, set when the job completes
///   jobs:active       Set    job ids currently running or queued
/// </summary>
public static class JobsApi
{
    private const int JobTtlSeconds = 60 * 60 * 24;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
    };

    /// <summary>
    /// Map of public job type → Python-internal route. Add a row here
    /// when introducing a new job kind; everything else (state, events,
    /// cancel, result) is type-agnostic.
    /// </summary>
    private static readonly Dictionary<string, string> JobTypeRoutes = new()
    {
        ["backtest"] = "/api/jobs-internal/backtest",
        ["dataset-zip"] = "/api/jobs-internal/dataset-zip",
        ["engine_backtest"] = "/api/jobs-internal/engine-backtest",
        ["cross_sectional"] = "/api/jobs-internal/cross-sectional",
    };

    public static void MapJobsEndpoints(this WebApplication app)
    {
        // Public surface lives under /api/jobs so we nest cleanly inside
        // the existing /api proxy namespace. The SPA dev-server proxy
        // routes /api/jobs → backend (this), and /api → python for
        // everything else.
        var group = app.MapGroup("/api/jobs").WithTags("Jobs");

        group.MapPost("/{type}", StartJobAsync);
        group.MapGet("/{id}/events", StreamJobEventsAsync);
        group.MapGet("/{id}/result", GetJobResultAsync);
        group.MapGet("/{id}/download", DownloadJobBlobAsync);
        group.MapDelete("/{id}", CancelJobAsync);
        group.MapGet("/", ListJobsAsync);
    }

    // ---------------------------------------------------------------------
    // POST /api/jobs/{type}
    // ---------------------------------------------------------------------

    private static async Task<IResult> StartJobAsync(
        string type,
        HttpRequest request,
        IConnectionMultiplexer redis,
        IHttpClientFactory httpFactory,
        IConfiguration config,
        ILoggerFactory loggerFactory,
        CancellationToken ct)
    {
        var logger = loggerFactory.CreateLogger("JobsApi");
        if (!JobTypeRoutes.TryGetValue(type, out var pythonPath))
        {
            return Results.BadRequest(new
            {
                error = $"unsupported job type '{type}'",
                supported = JobTypeRoutes.Keys.ToArray(),
            });
        }

        // Parse the request body as a mutable JsonObject so we can
        // inject job_id without reshaping the rest. The framework is
        // generic; only Python knows each type's schema.
        var bodyNode = await JsonNode.ParseAsync(request.Body, cancellationToken: ct);
        var bodyObj = bodyNode as JsonObject ?? new JsonObject();

        var jobId = Guid.NewGuid().ToString();
        var db = redis.GetDatabase();
        var stateKey = StateKey(jobId);

        var paramsJson = bodyObj.ToJsonString();
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();

        // Pipeline: write state hash + active set + TTL in one round-trip batch.
        var batch = db.CreateBatch();
        var stateTask = batch.HashSetAsync(stateKey, new HashEntry[]
        {
            new("id", jobId),
            new("type", type),
            new("status", "queued"),
            new("params", paramsJson),
            new("created_at", nowMs),
            new("cancel_requested", "0"),
        });
        var ttlTask = batch.KeyExpireAsync(stateKey, TimeSpan.FromSeconds(JobTtlSeconds));
        var activeTask = batch.SetAddAsync(ActiveSetKey, jobId);
        batch.Execute();
        await Task.WhenAll(stateTask, ttlTask, activeTask);

        // Forward to Python with the original payload + job_id injected.
        // The internal /api/jobs-internal/* routes accept whatever per-type
        // schema they need plus a job_id; we don't transcode field names.
        var http = httpFactory.CreateClient("python");
        var pythonUrl = (config["PolygonService:BaseUrl"] ?? "http://python-service:8000")
            .TrimEnd('/') + pythonPath;
        bodyObj["job_id"] = jobId;
        var mergedJson = bodyObj.ToJsonString();
        try
        {
            using var content = new StringContent(mergedJson, Encoding.UTF8, "application/json");
            var resp = await http.PostAsync(pythonUrl, content, ct);
            if (!resp.IsSuccessStatusCode)
            {
                var msg = await resp.Content.ReadAsStringAsync(ct);
                logger.LogError("[Jobs] Python rejected start: {Status} {Body}", resp.StatusCode, msg);
                await db.HashSetAsync(stateKey, new HashEntry[]
                {
                    new("status", "failed"),
                    new("error_code", "PythonRejected"),
                    new("error_message", msg),
                });
                await db.SetRemoveAsync(ActiveSetKey, jobId);
                return Results.Problem(detail: msg, statusCode: (int)resp.StatusCode);
            }
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Jobs] Failed to dispatch job {JobId} to Python", jobId);
            await db.HashSetAsync(stateKey, new HashEntry[]
            {
                new("status", "failed"),
                new("error_code", ex.GetType().Name),
                new("error_message", ex.Message),
            });
            await db.SetRemoveAsync(ActiveSetKey, jobId);
            return Results.Problem(detail: ex.Message, statusCode: 502);
        }

        return Results.Accepted($"/api/jobs/{jobId}/events", new { id = jobId, status = "queued" });
    }

    // ---------------------------------------------------------------------
    // GET /api/jobs/{id}/events  (SSE)
    // ---------------------------------------------------------------------

    private static async Task StreamJobEventsAsync(
        string id,
        HttpContext ctx,
        IConnectionMultiplexer redis,
        ILoggerFactory loggerFactory)
    {
        var logger = loggerFactory.CreateLogger("JobsApi");
        var db = redis.GetDatabase();
        var streamKey = EventsKey(id);
        var stateKey = StateKey(id);

        // Last-Event-ID lets a reconnecting client resume from where it
        // dropped. The header value is exactly the Redis stream entry id
        // so we can XRANGE forward without translation.
        var lastId = ctx.Request.Headers["Last-Event-ID"].ToString();
        if (string.IsNullOrEmpty(lastId)) lastId = "0-0";

        ctx.Response.Headers["Content-Type"] = "text/event-stream";
        ctx.Response.Headers["Cache-Control"] = "no-cache";
        ctx.Response.Headers["X-Accel-Buffering"] = "no"; // disable proxy buffering

        var token = ctx.RequestAborted;
        var sb = new StringBuilder();

        // 1. Replay history past last-id. XRANGE is exclusive on the
        //    start id when prefixed with '(' but a fresh client uses
        //    '0-0' which we want inclusive — special-case that.
        var rangeStart = lastId == "0-0" ? "-" : $"({lastId}";
        var historical = await db.StreamRangeAsync(streamKey, rangeStart, "+");
        foreach (var entry in historical)
        {
            await WriteSseEntryAsync(ctx, sb, entry, token);
            lastId = entry.Id;
        }

        // 2. Tail. We block briefly, then check if the job has reached
        //    a terminal status; if so, exit. Otherwise keep streaming.
        while (!token.IsCancellationRequested)
        {
            var entries = await db.StreamReadAsync(streamKey, lastId, count: 64);
            if (entries.Length > 0)
            {
                foreach (var entry in entries)
                {
                    await WriteSseEntryAsync(ctx, sb, entry, token);
                    lastId = entry.Id;
                }
                continue;
            }

            // No new entries. If the job is terminal AND we've drained
            // everything, the stream is done.
            var status = await db.HashGetAsync(stateKey, "status");
            if (status.HasValue && IsTerminal(status!))
            {
                // One more drain to catch races between final emit and
                // status flip.
                var tail = await db.StreamRangeAsync(streamKey, $"({lastId}", "+");
                foreach (var entry in tail)
                {
                    await WriteSseEntryAsync(ctx, sb, entry, token);
                }
                break;
            }

            try
            {
                await Task.Delay(500, token);
            }
            catch (TaskCanceledException)
            {
                break;
            }
        }

        logger.LogDebug("[Jobs] SSE closed for {JobId}", id);
    }

    private static async Task WriteSseEntryAsync(HttpContext ctx, StringBuilder sb, StreamEntry entry, CancellationToken ct)
    {
        // Each entry has a single "event" field with the JSON body.
        var bodyValue = entry["event"];
        if (!bodyValue.HasValue) return;

        sb.Clear();
        sb.Append("id: ").Append(entry.Id).Append('\n');
        sb.Append("data: ").Append((string)bodyValue!).Append("\n\n");
        var bytes = Encoding.UTF8.GetBytes(sb.ToString());
        await ctx.Response.Body.WriteAsync(bytes, ct);
        await ctx.Response.Body.FlushAsync(ct);
    }

    private static bool IsTerminal(string status) =>
        status is "completed" or "failed" or "cancelled";

    // ---------------------------------------------------------------------
    // GET /api/jobs/{id}/result   (JSON-bearing jobs)
    // ---------------------------------------------------------------------

    private static async Task<IResult> GetJobResultAsync(
        string id,
        IConnectionMultiplexer redis)
    {
        var db = redis.GetDatabase();
        var raw = await db.StringGetAsync(ResultKey(id));
        if (!raw.HasValue)
        {
            return Results.NotFound(new { error = "result not found or expired" });
        }
        return Results.Content((string)raw!, "application/json");
    }

    // ---------------------------------------------------------------------
    // GET /api/jobs/{id}/download   (binary-bearing jobs — ZIP, PDF, ...)
    // ---------------------------------------------------------------------

    private static async Task<IResult> DownloadJobBlobAsync(
        string id,
        IConnectionMultiplexer redis)
    {
        var db = redis.GetDatabase();
        var meta = await db.HashGetAllAsync(ResultMetaKey(id));
        if (meta.Length == 0)
        {
            return Results.NotFound(new { error = "blob result not found or expired" });
        }
        var metaDict = meta.ToDictionary(e => (string)e.Name!, e => (string)e.Value!);
        var filename = metaDict.GetValueOrDefault("filename", $"job-{id}");
        var contentType = metaDict.GetValueOrDefault("content_type", "application/octet-stream");

        var blob = await db.StringGetAsync(ResultBlobKey(id));
        if (!blob.HasValue)
        {
            return Results.NotFound(new { error = "blob payload missing (meta present)" });
        }
        return Results.File((byte[])blob!, contentType, filename);
    }

    // ---------------------------------------------------------------------
    // DELETE /api/jobs/{id}
    // ---------------------------------------------------------------------

    private static async Task<IResult> CancelJobAsync(
        string id,
        IConnectionMultiplexer redis)
    {
        var db = redis.GetDatabase();
        var stateKey = StateKey(id);
        if (!await db.KeyExistsAsync(stateKey))
        {
            return Results.NotFound(new { error = "job not found" });
        }
        await db.HashSetAsync(stateKey, "cancel_requested", "1");
        return Results.Ok(new { id, cancel_requested = true });
    }

    // ---------------------------------------------------------------------
    // GET /api/jobs?active=true
    // ---------------------------------------------------------------------

    private static async Task<IResult> ListJobsAsync(
        IConnectionMultiplexer redis,
        bool active = true)
    {
        var db = redis.GetDatabase();
        if (!active)
        {
            // No global "all jobs" index in v1 — only the active set.
            // Frontend never asks for this; return empty.
            return Results.Ok(Array.Empty<object>());
        }

        var ids = await db.SetMembersAsync(ActiveSetKey);
        var jobs = new List<Dictionary<string, string>>();
        foreach (var idValue in ids)
        {
            var entries = await db.HashGetAllAsync(StateKey(idValue!));
            if (entries.Length == 0)
            {
                // State expired but id still in active set — clean up.
                await db.SetRemoveAsync(ActiveSetKey, idValue);
                continue;
            }
            var dict = entries.ToDictionary(e => (string)e.Name!, e => (string)e.Value!);
            jobs.Add(dict);
        }
        return Results.Ok(jobs);
    }

    // ---------------------------------------------------------------------
    // Keys
    // ---------------------------------------------------------------------

    private static string StateKey(string id) => $"job:{id}:state";
    private static string EventsKey(string id) => $"job:{id}:events";
    private static string ResultKey(string id) => $"job:{id}:result";
    private static string ResultBlobKey(string id) => $"job:{id}:result-blob";
    private static string ResultMetaKey(string id) => $"job:{id}:result-meta";
    private const string ActiveSetKey = "jobs:active";
}
