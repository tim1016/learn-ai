using Backend;
using Backend.Configuration;
using Backend.Data;
using Backend.GraphQL;
using Backend.Jobs;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Polly;
using Polly.Extensions.Http;
using StackExchange.Redis;

var builder = WebApplication.CreateBuilder(args);

// Add CORS
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
    {
        policy.WithOrigins("http://localhost:4200", "http://127.0.0.1:4200")
              .AllowAnyHeader()
              .AllowAnyMethod();
    });
});

// Add DbContext
builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(builder.Configuration.GetConnectionString("DefaultConnection")));

// Configure PolygonService options (testable via IOptions)
builder.Services.Configure<PolygonServiceOptions>(
    builder.Configuration.GetSection(PolygonServiceOptions.SectionName));

// IV recorder cron — Step D follow-up of the IV-ownership plan. Opt-in
// via `IvRecorder:Enabled = true` in config; dev/CI default is off.
// Schedules one Quartz trigger per slot (default 09:35 / 12:30 / 15:55 /
// 16:00 ET; 15:55 runs alongside 16:00 for the trial-month experiment
// in research-doc §7.6 / §8.2.3) that POSTs to Python's
// /api/iv-recorder/snapshot per configured ticker.
builder.Services.AddIvRecorder(builder.Configuration);

// Shared Polly policies — generous thresholds to avoid tripping circuit on transient Polygon hiccups
var retryPolicy = HttpPolicyExtensions
    .HandleTransientHttpError()
    .WaitAndRetryAsync(3, retryAttempt => TimeSpan.FromSeconds(Math.Pow(2, retryAttempt)));
var circuitBreakerPolicy = HttpPolicyExtensions
    .HandleTransientHttpError()
    .CircuitBreakerAsync(
        handledEventsAllowedBeforeBreaking: 15,
        durationOfBreak: TimeSpan.FromSeconds(15));

// Add HttpClient with Polly for resilience (testable with mocked HttpClient)
// 300s timeout to accommodate heavy endpoints like /api/quantlib/compare (100 pts × 7 engines)
builder.Services.AddHttpClient<IPolygonService, PolygonService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(300);
})
.AddPolicyHandler(retryPolicy)
.AddPolicyHandler(circuitBreakerPolicy);

// Add HttpClient for SanitizationService (same Python service, same resilience policies)
builder.Services.AddHttpClient<ISanitizationService, SanitizationService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(120);
})
.AddPolicyHandler(retryPolicy)
.AddPolicyHandler(circuitBreakerPolicy);

// Add HttpClient for TechnicalAnalysisService (same Python service, same resilience policies)
builder.Services.AddHttpClient<ITechnicalAnalysisService, TechnicalAnalysisService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(120);
})
.AddPolicyHandler(retryPolicy)
.AddPolicyHandler(circuitBreakerPolicy);

// Add HttpClient for ResearchService — no retry policy.
// Research requests are expensive (minutes long, hundreds of MB payloads).
// If the first attempt fails, retrying burns another 3-10 minutes doing the
// same work with the same outcome. Fail fast and surface the error to the
// user instead. Circuit-breaker is retained so a truly-broken python service
// short-circuits cleanly.
builder.Services.AddHttpClient<IResearchService, ResearchService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(600);
})
.AddPolicyHandler(circuitBreakerPolicy);

// Redis — backing store for job state and SSE event streams. The same
// Redis instance is shared with PythonDataService; the schema is
// documented in Backend/Jobs/JobsApi.cs and PythonDataService/app/jobs/progress.py.
var redisUrl = builder.Configuration["REDIS_URL"]
    ?? Environment.GetEnvironmentVariable("REDIS_URL")
    ?? "redis:6379";
builder.Services.AddSingleton<IConnectionMultiplexer>(_ =>
    ConnectionMultiplexer.Connect(redisUrl));

// HttpClient used by JobsApi to dispatch work to Python. No retry — a
// long backtest must not silently spawn duplicate runs on a transient hiccup.
//
// Timeout: the Python /api/jobs-internal/* handlers spawn the actual
// work in a daemon thread (see app/jobs/runner.py) and return
// ``{"job_id": …, "status": "queued"}`` essentially synchronously, so
// the dispatch itself should complete in milliseconds. 15 s was tight
// when the python-service event loop was busy with a previous study's
// IC computations — uvicorn on a single worker can briefly starve a
// new request while another one is mid-flight, and we'd 502 the user
// even though the dispatch path itself is fire-and-forget. 60 s is
// pure headroom; if the dispatch ever takes that long, something
// else is genuinely wrong.
builder.Services.AddHttpClient("python", client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(60);
});

// Register business services (testable via interfaces)
builder.Services.AddScoped<IMarketDataService, MarketDataService>();
builder.Services.AddScoped<IBacktestService, BacktestService>();
builder.Services.AddScoped<IPositionEngine, PositionEngine>();
builder.Services.AddScoped<IPortfolioService, PortfolioService>();
builder.Services.AddScoped<IPortfolioValuationService, PortfolioValuationService>();
builder.Services.AddScoped<ISnapshotService, SnapshotService>();
builder.Services.AddScoped<IPortfolioRiskService, PortfolioRiskService>();
builder.Services.AddScoped<IPortfolioReconciliationService, PortfolioReconciliationService>();
builder.Services.AddScoped<IStrategyAttributionService, StrategyAttributionService>();
builder.Services.AddScoped<IPortfolioValidationService, PortfolioValidationService>();

// Add GraphQL services
builder.Services
    .AddGraphQLServer()
    .AddQueryType<Query>()
    .AddTypeExtension<PortfolioQuery>()
    .AddTypeExtension<DataLabQuery>()
    .AddMutationType<Mutation>()
    .AddTypeExtension<PortfolioMutation>()
    .AddTypeExtension<DataLabMutation>()
    .AddProjections()
    .AddFiltering()
    .AddSorting()
    .ModifyRequestOptions(opt =>
    {
        opt.IncludeExceptionDetails = true;
        opt.ExecutionTimeout = TimeSpan.FromMinutes(10);
    });

var app = builder.Build();

// Create database schema with retry for container startup
using (var scope = app.Services.CreateScope())
{
    var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    var maxRetries = 10;
    for (var i = 0; i < maxRetries; i++)
    {
        try
        {
            context.Database.EnsureCreated();
            Console.WriteLine("Database initialized successfully.");
            break;
        }
        catch (Exception) when (i < maxRetries - 1)
        {
            Console.WriteLine($"Waiting for database... attempt {i + 1}/{maxRetries}");
            await Task.Delay(2000);
        }
    }
}

app.UseCors();
app.MapGet("/health", () => Results.Ok(new { status = "healthy" }));
app.MapStudiesEndpoints();
app.MapJobsEndpoints();
app.MapGraphQL();

app.Run();
