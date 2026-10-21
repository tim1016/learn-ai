using System.Net;
using System.Text.Json;
using Backend.Configuration;
using Backend.Jobs;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Moq;
using Quartz;

namespace Backend.Tests.Unit.Jobs;

/// <summary>
/// Unit tests for the IV recorder job and its slot-to-cron helper.
/// Pins per-ticker dispatch, error tolerance, and JobDataMap parsing.
/// </summary>
public class IvRecorderJobTests
{
    [Fact]
    public async Task Execute_PostsToPythonOncePerTicker()
    {
        var handler = RecordingHttpMessageHandler.AlwaysSucceeds("{\"success\":true}");
        var sut = CreateJob(handler, new IvRecorderOptions
        {
            Enabled = true,
            Tickers = new() { "SPY", "QQQ" },
            Slots = new() { "09:35" },
            TargetCalendarDays = 30,
        });

        await sut.Execute(BuildContext(slot: "09:35"));

        Assert.Equal(2, handler.RequestCount);
        Assert.All(handler.RequestUris, uri =>
            Assert.EndsWith("/api/iv-recorder/snapshot", uri.AbsolutePath));
        Assert.All(handler.RequestMethods, method =>
            Assert.Equal(HttpMethod.Post, method));

        var bodies = handler.RequestBodies
            .Where(b => b is not null)
            .Select(b => JsonSerializer.Deserialize<JsonElement>(b!))
            .ToList();
        Assert.Contains(bodies, b => b.GetProperty("ticker").GetString() == "SPY");
        Assert.Contains(bodies, b => b.GetProperty("ticker").GetString() == "QQQ");
        Assert.All(bodies, b =>
        {
            Assert.Equal("09:35", b.GetProperty("slot").GetString());
            Assert.Equal(30, b.GetProperty("target_calendar_days").GetInt32());
        });
    }

    [Fact]
    public async Task Execute_LogsAndContinues_WhenOnePostFails()
    {
        // First ticker fails with 500, second succeeds. The job should
        // still dispatch to every ticker — one bad symbol does not abort
        // the slot.
        var responses = new Queue<HttpResponseMessage>();
        responses.Enqueue(new HttpResponseMessage(HttpStatusCode.InternalServerError)
        { Content = new StringContent("boom") });
        responses.Enqueue(new HttpResponseMessage(HttpStatusCode.OK)
        { Content = new StringContent("{\"success\":true}") });
        var handler = new RecordingHttpMessageHandler(responses);

        var sut = CreateJob(handler, new IvRecorderOptions
        {
            Enabled = true,
            Tickers = new() { "BAD", "GOOD" },
            Slots = new() { "12:30" },
            TargetCalendarDays = 30,
        });

        await sut.Execute(BuildContext(slot: "12:30"));

        Assert.Equal(2, handler.RequestCount);
    }

    [Fact]
    public async Task Execute_DoesNothing_WhenSlotMissing()
    {
        var handler = RecordingHttpMessageHandler.AlwaysSucceeds("{}");
        var sut = CreateJob(handler, new IvRecorderOptions
        {
            Enabled = true,
            Tickers = new() { "SPY" },
        });

        await sut.Execute(BuildContext(slot: null));

        Assert.Equal(0, handler.RequestCount);
    }

    [Fact]
    public async Task Execute_DoesNothing_WhenNoTickers()
    {
        var handler = RecordingHttpMessageHandler.AlwaysSucceeds("{}");
        var sut = CreateJob(handler, new IvRecorderOptions
        {
            Enabled = true,
            Tickers = new(),
        });

        await sut.Execute(BuildContext(slot: "09:35"));

        Assert.Equal(0, handler.RequestCount);
    }

    [Fact]
    public async Task Execute_PropagatesShutdownCancellation()
    {
        var handler = RecordingHttpMessageHandler.AlwaysSucceeds("{}");
        var sut = CreateJob(handler, new IvRecorderOptions
        {
            Enabled = true,
            Tickers = new() { "SPY" },
            Slots = new() { "09:35" },
        });
        using var cts = new CancellationTokenSource();
        cts.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            sut.Execute(BuildContext(slot: "09:35", ct: cts.Token)));
    }

    private static IvRecorderJob CreateJob(HttpMessageHandler handler, IvRecorderOptions options)
    {
        var http = new HttpClient(handler) { BaseAddress = new Uri("http://python-service:8000") };
        var factory = new Mock<IHttpClientFactory>();
        factory.Setup(f => f.CreateClient(It.IsAny<string>())).Returns(http);

        var opts = Options.Create(options);
        var logger = Mock.Of<ILogger<IvRecorderJob>>();
        return new IvRecorderJob(factory.Object, opts, logger);
    }

    private static IJobExecutionContext BuildContext(string? slot, CancellationToken ct = default)
    {
        var ctx = new Mock<IJobExecutionContext>();
        var data = new JobDataMap();
        if (slot is not null)
        {
            data.Put(IvRecorderJob.SlotJobDataKey, slot);
        }
        ctx.SetupGet(c => c.MergedJobDataMap).Returns(data);
        ctx.SetupGet(c => c.CancellationToken).Returns(ct);
        return ctx.Object;
    }
}

public class IvRecorderRegistrationTests
{
    [Theory]
    [InlineData("09:35", "0 35 9 ? * MON-FRI")]
    [InlineData("12:30", "0 30 12 ? * MON-FRI")]
    [InlineData("16:00", "0 0 16 ? * MON-FRI")]
    [InlineData("00:00", "0 0 0 ? * MON-FRI")]
    [InlineData("23:59", "0 59 23 ? * MON-FRI")]
    [InlineData("9:35", "0 35 9 ? * MON-FRI")]  // single-digit hour also accepted
    public void SlotToCronExpression_Valid_ReturnsExpectedCron(string slot, string expected)
    {
        Assert.Equal(expected, IvRecorderRegistration.SlotToCronExpression(slot));
    }

    [Theory]
    [InlineData("0935")]
    [InlineData("9:35:00")]
    [InlineData("nope")]
    [InlineData("24:00")]
    [InlineData("12:60")]
    [InlineData("-1:00")]
    public void SlotToCronExpression_Invalid_Throws(string slot)
    {
        Assert.Throws<ArgumentException>(() =>
            IvRecorderRegistration.SlotToCronExpression(slot));
    }
}

/// <summary>
/// Records every request and returns responses from a queue.
/// One-response constructor is a convenience for tests that only need
/// the same response for every request.
/// </summary>
internal class RecordingHttpMessageHandler : HttpMessageHandler
{
    private readonly Queue<HttpResponseMessage>? _responses;
    private readonly Func<HttpResponseMessage>? _factory;

    public List<Uri> RequestUris { get; } = new();
    public List<HttpMethod> RequestMethods { get; } = new();
    public List<string?> RequestBodies { get; } = new();
    public int RequestCount => RequestUris.Count;

    public RecordingHttpMessageHandler(Queue<HttpResponseMessage> responses)
    {
        _responses = responses;
    }

    private RecordingHttpMessageHandler(Func<HttpResponseMessage> factory)
    {
        _factory = factory;
    }

    public static RecordingHttpMessageHandler AlwaysSucceeds(string body) =>
        new(() => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json"),
        });

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (request.RequestUri is not null) RequestUris.Add(request.RequestUri);
        RequestMethods.Add(request.Method);
        if (request.Content is not null)
        {
            RequestBodies.Add(await request.Content.ReadAsStringAsync(cancellationToken));
        }
        else
        {
            RequestBodies.Add(null);
        }
        return _factory?.Invoke() ?? _responses!.Dequeue();
    }
}
