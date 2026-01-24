using Backend.Data;
using Backend.GraphQL;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

// Add CORS
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
    {
        policy.WithOrigins("http://localhost:4200")
              .AllowAnyHeader()
              .AllowAnyMethod();
    });
});

// Add DbContext
builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(builder.Configuration.GetConnectionString("DefaultConnection")));

// Add GraphQL services
builder.Services
    .AddGraphQLServer()
    .AddQueryType<Query>()
    .AddMutationType<Mutation>()
    .AddProjections()
    .AddFiltering()
    .AddSorting();

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
