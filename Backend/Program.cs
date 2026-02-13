using Backend.Configuration;
using Backend.Data;
using Backend.GraphQL;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Polly;
using Polly.Extensions.Http;

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

// Add HttpClient with Polly for resilience (testable with mocked HttpClient)
builder.Services.AddHttpClient<IPolygonService, PolygonService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(120);
})
.AddPolicyHandler(HttpPolicyExtensions
    .HandleTransientHttpError()
    .WaitAndRetryAsync(3, retryAttempt => TimeSpan.FromSeconds(Math.Pow(2, retryAttempt))))
.AddPolicyHandler(HttpPolicyExtensions
    .HandleTransientHttpError()
    .CircuitBreakerAsync(5, TimeSpan.FromSeconds(30)));

// Add HttpClient for SanitizationService (same Python service, same resilience policies)
builder.Services.AddHttpClient<ISanitizationService, SanitizationService>(client =>
{
    var baseUrl = builder.Configuration["PolygonService:BaseUrl"] ?? "http://python-service:8000";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(120);
})
.AddPolicyHandler(HttpPolicyExtensions
    .HandleTransientHttpError()
    .WaitAndRetryAsync(3, retryAttempt => TimeSpan.FromSeconds(Math.Pow(2, retryAttempt))))
.AddPolicyHandler(HttpPolicyExtensions
    .HandleTransientHttpError()
    .CircuitBreakerAsync(5, TimeSpan.FromSeconds(30)));

// Register business services (testable via interfaces)
builder.Services.AddScoped<IMarketDataService, MarketDataService>();

// Add GraphQL services
builder.Services
    .AddGraphQLServer()
    .AddQueryType<Query>()
    .AddMutationType<Mutation>()
    .AddProjections()
    .AddFiltering()
    .AddSorting()
    .ModifyRequestOptions(opt => opt.IncludeExceptionDetails = true);

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
        catch (Exception ex) when (i < maxRetries - 1)
        {
            Console.WriteLine($"Waiting for database... attempt {i + 1}/{maxRetries}");
            await Task.Delay(2000);
        }
    }
}

app.UseCors();
app.MapGraphQL();

app.Run();
