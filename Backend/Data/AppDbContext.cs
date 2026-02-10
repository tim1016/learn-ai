using Backend.Models;
using Backend.Models.MarketData;
using Microsoft.EntityFrameworkCore;

namespace Backend.Data;

public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options)
    {
    }

    // Original demo models
    public DbSet<Author> Authors => Set<Author>();
    public DbSet<Book> Books => Set<Book>();

    // Market data models
    public DbSet<Ticker> Tickers => Set<Ticker>();
    public DbSet<StockAggregate> StockAggregates => Set<StockAggregate>();
    public DbSet<Trade> Trades => Set<Trade>();
    public DbSet<Quote> Quotes => Set<Quote>();
    public DbSet<TechnicalIndicator> TechnicalIndicators => Set<TechnicalIndicator>();
    public DbSet<ReferenceData> ReferenceData => Set<ReferenceData>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Author>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.Name).IsRequired().HasMaxLength(200);
            entity.Property(a => a.Bio).HasMaxLength(1000);
        });

        modelBuilder.Entity<Book>(entity =>
        {
            entity.HasKey(b => b.Id);
            entity.Property(b => b.Title).IsRequired().HasMaxLength(300);
            entity.HasOne(b => b.Author)
                  .WithMany(a => a.Books)
                  .HasForeignKey(b => b.AuthorId);
        });

        // Seed some initial data
        modelBuilder.Entity<Author>().HasData(
            new Author { Id = 1, Name = "George Orwell", Bio = "English novelist and essayist" },
            new Author { Id = 2, Name = "Jane Austen", Bio = "English novelist known for romantic fiction" }
        );

        modelBuilder.Entity<Book>().HasData(
            new Book { Id = 1, Title = "1984", PublishedYear = 1949, AuthorId = 1 },
            new Book { Id = 2, Title = "Animal Farm", PublishedYear = 1945, AuthorId = 1 },
            new Book { Id = 3, Title = "Pride and Prejudice", PublishedYear = 1813, AuthorId = 2 }
        );

        // Market Data Configurations
        ConfigureMarketDataModels(modelBuilder);
    }

    /// <summary>
    /// Configure market data entity models (testable configuration)
    /// Separated for readability and testability
    /// </summary>
    private static void ConfigureMarketDataModels(ModelBuilder modelBuilder)
    {
        // Ticker configuration
        modelBuilder.Entity<Ticker>(entity =>
        {
            entity.HasKey(t => t.Id);
            entity.Property(t => t.Symbol).IsRequired().HasMaxLength(20);
            entity.Property(t => t.Name).IsRequired().HasMaxLength(500);
            entity.Property(t => t.Market).IsRequired().HasMaxLength(50);
            entity.HasIndex(t => new { t.Symbol, t.Market }).IsUnique();
            entity.HasIndex(t => t.Symbol);
        });

        // StockAggregate configuration
        modelBuilder.Entity<StockAggregate>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.Open).HasPrecision(18, 8);
            entity.Property(a => a.High).HasPrecision(18, 8);
            entity.Property(a => a.Low).HasPrecision(18, 8);
            entity.Property(a => a.Close).HasPrecision(18, 8);
            entity.Property(a => a.Volume).HasPrecision(18, 8);
            entity.Property(a => a.VolumeWeightedAveragePrice).HasPrecision(18, 8);
            entity.HasOne(a => a.Ticker)
                  .WithMany(t => t.Aggregates)
                  .HasForeignKey(a => a.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(a => new { a.TickerId, a.Timestamp, a.Timespan });
            entity.HasIndex(a => a.Timestamp);
        });

        // Trade configuration
        modelBuilder.Entity<Trade>(entity =>
        {
            entity.HasKey(t => t.Id);
            entity.Property(t => t.Price).HasPrecision(18, 8);
            entity.Property(t => t.Size).HasPrecision(18, 8);
            entity.HasOne(t => t.Ticker)
                  .WithMany(tk => tk.Trades)
                  .HasForeignKey(t => t.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(t => new { t.TickerId, t.Timestamp });
        });

        // Quote configuration
        modelBuilder.Entity<Quote>(entity =>
        {
            entity.HasKey(q => q.Id);
            entity.Property(q => q.BidPrice).HasPrecision(18, 8);
            entity.Property(q => q.AskPrice).HasPrecision(18, 8);
            entity.Property(q => q.BidSize).HasPrecision(18, 8);
            entity.Property(q => q.AskSize).HasPrecision(18, 8);
            entity.HasOne(q => q.Ticker)
                  .WithMany(t => t.Quotes)
                  .HasForeignKey(q => q.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(q => new { q.TickerId, q.Timestamp });
        });

        // TechnicalIndicator configuration
        modelBuilder.Entity<TechnicalIndicator>(entity =>
        {
            entity.HasKey(i => i.Id);
            entity.Property(i => i.Value).HasPrecision(18, 8);
            entity.Property(i => i.Signal).HasPrecision(18, 8);
            entity.Property(i => i.Histogram).HasPrecision(18, 8);
            entity.HasOne(i => i.Ticker)
                  .WithMany(t => t.Indicators)
                  .HasForeignKey(i => i.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(i => new { i.TickerId, i.IndicatorType, i.Timestamp });
        });

        // ReferenceData configuration
        modelBuilder.Entity<ReferenceData>(entity =>
        {
            entity.HasKey(r => r.Id);
            entity.Property(r => r.CashAmount).HasPrecision(18, 8);
            entity.Property(r => r.SplitFrom).HasPrecision(18, 8);
            entity.Property(r => r.SplitTo).HasPrecision(18, 8);
            entity.HasOne(r => r.Ticker)
                  .WithMany()
                  .HasForeignKey(r => r.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(r => new { r.TickerId, r.DataType, r.EventDate });
        });
    }
}
