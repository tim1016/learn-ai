using Backend.Models.DataLab;
using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Microsoft.EntityFrameworkCore;

namespace Backend.Data;

public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options)
    {
    }

    // Market data models
    public DbSet<Ticker> Tickers => Set<Ticker>();
    public DbSet<StockAggregate> StockAggregates => Set<StockAggregate>();
    public DbSet<Trade> Trades => Set<Trade>();
    public DbSet<Quote> Quotes => Set<Quote>();
    public DbSet<TechnicalIndicator> TechnicalIndicators => Set<TechnicalIndicator>();
    public DbSet<ReferenceData> ReferenceData => Set<ReferenceData>();

    // Backtesting models
    public DbSet<StrategyExecution> StrategyExecutions => Set<StrategyExecution>();
    public DbSet<BacktestTrade> BacktestTrades => Set<BacktestTrade>();

    // Data lake catalog (Slice 1a)
    public DbSet<DataLakeArtifact> DataLakeArtifacts => Set<DataLakeArtifact>();
    public DbSet<DataLakeRun> DataLakeRuns => Set<DataLakeRun>();

    // Research models
    public DbSet<ResearchExperiment> ResearchExperiments => Set<ResearchExperiment>();
    public DbSet<SignalExperiment> SignalExperiments => Set<SignalExperiment>();

    // Options IV cache
    public DbSet<OptionsIvSnapshot> OptionsIvSnapshots => Set<OptionsIvSnapshot>();

    // Data Lab models
    public DbSet<DataLabSession> DataLabSessions => Set<DataLabSession>();

    // Portfolio models
    public DbSet<Account> Accounts => Set<Account>();
    public DbSet<Order> Orders => Set<Order>();
    public DbSet<PortfolioTrade> PortfolioTrades => Set<PortfolioTrade>();
    public DbSet<Position> Positions => Set<Position>();
    public DbSet<PositionLot> PositionLots => Set<PositionLot>();
    public DbSet<OptionContract> OptionContracts => Set<OptionContract>();
    public DbSet<OptionLeg> OptionLegs => Set<OptionLeg>();
    public DbSet<PortfolioSnapshot> PortfolioSnapshots => Set<PortfolioSnapshot>();
    public DbSet<RiskRule> RiskRules => Set<RiskRule>();
    public DbSet<StrategyAllocation> StrategyAllocations => Set<StrategyAllocation>();
    public DbSet<StrategyTradeLink> StrategyTradeLinks => Set<StrategyTradeLink>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        // Market Data Configurations
        ConfigureMarketDataModels(modelBuilder);

        // Data Lab Configurations
        ConfigureDataLabModels(modelBuilder);

        // Portfolio Configurations
        ConfigurePortfolioModels(modelBuilder);

        // Data Lake Catalog (Slice 1a)
        ConfigureDataLakeModels(modelBuilder);
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
            entity.Property(t => t.Symbol).IsRequired().HasMaxLength(50);
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

        // StrategyExecution configuration
        modelBuilder.Entity<StrategyExecution>(entity =>
        {
            entity.HasKey(e => e.Id);
            entity.Property(e => e.TotalPnL).HasPrecision(18, 8);
            entity.Property(e => e.MaxDrawdown).HasPrecision(18, 8);
            entity.Property(e => e.SharpeRatio).HasPrecision(18, 8);
            // LEAN-parity KPI precision
            entity.Property(e => e.InitialCash).HasPrecision(18, 2);
            entity.Property(e => e.FinalEquity).HasPrecision(18, 2);
            entity.Property(e => e.TotalFees).HasPrecision(18, 4);
            entity.Property(e => e.WinRate).HasPrecision(18, 8);
            entity.Property(e => e.CompoundingAnnualReturn).HasPrecision(18, 8);
            entity.Property(e => e.SortinoRatio).HasPrecision(18, 8);
            entity.Property(e => e.ProbabilisticSharpeRatio).HasPrecision(18, 8);
            entity.Property(e => e.ProfitFactor).HasPrecision(18, 8);
            entity.Property(e => e.Alpha).HasPrecision(18, 8);
            entity.Property(e => e.Beta).HasPrecision(18, 8);
            entity.Property(e => e.InformationRatio).HasPrecision(18, 8);
            entity.Property(e => e.TrackingError).HasPrecision(18, 8);
            entity.Property(e => e.TreynorRatio).HasPrecision(18, 8);
            entity.Property(e => e.ValueAtRisk95).HasPrecision(18, 8);
            entity.Property(e => e.ValueAtRisk99).HasPrecision(18, 8);
            entity.Property(e => e.AnnualStandardDeviation).HasPrecision(18, 8);
            entity.Property(e => e.LeanRunId).HasMaxLength(128);
            entity.HasOne(e => e.Ticker)
                  .WithMany()
                  .HasForeignKey(e => e.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(e => new { e.TickerId, e.StrategyName });
            entity.HasIndex(e => e.ExecutedAt);
            entity.HasIndex(e => e.Source);
            entity.HasIndex(e => new { e.Source, e.LeanRunId })
                  .IsUnique()
                  .HasFilter("\"LeanRunId\" IS NOT NULL");
        });

        // BacktestTrade configuration
        modelBuilder.Entity<BacktestTrade>(entity =>
        {
            entity.HasKey(t => t.Id);
            entity.Property(t => t.EntryPrice).HasPrecision(18, 8);
            entity.Property(t => t.ExitPrice).HasPrecision(18, 8);
            entity.Property(t => t.Quantity).HasPrecision(18, 8);
            entity.Property(t => t.PnL).HasPrecision(18, 8);
            entity.Property(t => t.CumulativePnL).HasPrecision(18, 8);
            entity.Property(t => t.IsSyntheticExit).HasDefaultValue(false);
            entity.HasOne(t => t.StrategyExecution)
                  .WithMany(e => e.Trades)
                  .HasForeignKey(t => t.StrategyExecutionId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(t => t.StrategyExecutionId);
        });

        // ResearchExperiment configuration
        modelBuilder.Entity<ResearchExperiment>(entity =>
        {
            entity.HasKey(e => e.Id);
            entity.Property(e => e.MeanIC).HasPrecision(18, 8);
            entity.Property(e => e.ICTStat).HasPrecision(18, 8);
            entity.Property(e => e.ICPValue).HasPrecision(18, 8);
            entity.Property(e => e.AdfPValue).HasPrecision(18, 8);
            entity.Property(e => e.KpssPValue).HasPrecision(18, 8);
            entity.Property(e => e.MonotonicityRatio).HasPrecision(18, 8);
            entity.HasOne(e => e.Ticker)
                  .WithMany()
                  .HasForeignKey(e => e.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(e => new { e.TickerId, e.FeatureName, e.CreatedAt });
        });

        // SignalExperiment configuration
        modelBuilder.Entity<SignalExperiment>(entity =>
        {
            entity.HasKey(e => e.Id);
            entity.Property(e => e.MeanOosSharpe).HasPrecision(18, 8);
            entity.Property(e => e.BestThreshold).HasPrecision(18, 8);
            entity.Property(e => e.BestCostBps).HasPrecision(18, 8);
            entity.HasOne(e => e.Ticker)
                  .WithMany()
                  .HasForeignKey(e => e.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(e => new { e.TickerId, e.FeatureName, e.CreatedAt });
        });

        // OptionsIvSnapshot configuration
        modelBuilder.Entity<OptionsIvSnapshot>(entity =>
        {
            entity.HasKey(e => e.Id);
            entity.Property(e => e.Iv30dAtm).HasPrecision(18, 8);
            entity.Property(e => e.Iv30dPut).HasPrecision(18, 8);
            entity.Property(e => e.Iv30dCall).HasPrecision(18, 8);
            entity.Property(e => e.StockClose).HasPrecision(18, 8);
            entity.HasOne(e => e.Ticker)
                  .WithMany()
                  .HasForeignKey(e => e.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(e => new { e.TickerId, e.TradingDate }).IsUnique();
        });
    }

    private static void ConfigureDataLabModels(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<DataLabSession>(entity =>
        {
            entity.HasKey(s => s.Id);
            entity.Property(s => s.Name).IsRequired().HasMaxLength(300);
            entity.Property(s => s.Ticker).IsRequired().HasMaxLength(20);
            entity.Property(s => s.FromDate).IsRequired().HasMaxLength(10);
            entity.Property(s => s.ToDate).IsRequired().HasMaxLength(10);
            entity.Property(s => s.Session).IsRequired().HasMaxLength(10);
            entity.Property(s => s.EntriesJson).IsRequired().HasColumnType("jsonb");
            entity.Property(s => s.ChartSnapshotJson).HasColumnType("jsonb");
            entity.HasIndex(s => s.UpdatedAt);
            entity.HasIndex(s => s.Ticker);
        });
    }

    private static void ConfigurePortfolioModels(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Account>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.Name).IsRequired().HasMaxLength(200);
            entity.Property(a => a.BaseCurrency).HasMaxLength(10);
            entity.Property(a => a.InitialCash).HasPrecision(18, 8);
            entity.Property(a => a.Cash).HasPrecision(18, 8);
            entity.Property(a => a.Type).HasConversion<string>().HasMaxLength(20);
        });

        modelBuilder.Entity<Order>(entity =>
        {
            entity.HasKey(o => o.Id);
            entity.Property(o => o.Quantity).HasPrecision(18, 8);
            entity.Property(o => o.LimitPrice).HasPrecision(18, 8);
            entity.Property(o => o.Side).HasConversion<string>().HasMaxLength(10);
            entity.Property(o => o.OrderType).HasConversion<string>().HasMaxLength(10);
            entity.Property(o => o.Status).HasConversion<string>().HasMaxLength(20);
            entity.Property(o => o.AssetType).HasConversion<string>().HasMaxLength(10);
            entity.HasOne(o => o.Account)
                  .WithMany(a => a.Orders)
                  .HasForeignKey(o => o.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(o => o.Ticker)
                  .WithMany()
                  .HasForeignKey(o => o.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(o => o.OptionContract)
                  .WithMany()
                  .HasForeignKey(o => o.OptionContractId)
                  .OnDelete(DeleteBehavior.SetNull);
            entity.HasIndex(o => new { o.AccountId, o.Status });
        });

        modelBuilder.Entity<PortfolioTrade>(entity =>
        {
            entity.HasKey(t => t.Id);
            entity.Property(t => t.Quantity).HasPrecision(18, 8);
            entity.Property(t => t.Price).HasPrecision(18, 8);
            entity.Property(t => t.Fees).HasPrecision(18, 8);
            entity.Property(t => t.Side).HasConversion<string>().HasMaxLength(10);
            entity.Property(t => t.AssetType).HasConversion<string>().HasMaxLength(10);
            entity.HasOne(t => t.Account)
                  .WithMany(a => a.Trades)
                  .HasForeignKey(t => t.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(t => t.Order)
                  .WithMany(o => o.Trades)
                  .HasForeignKey(t => t.OrderId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(t => t.Ticker)
                  .WithMany()
                  .HasForeignKey(t => t.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(t => t.OptionContract)
                  .WithMany()
                  .HasForeignKey(t => t.OptionContractId)
                  .OnDelete(DeleteBehavior.SetNull);
            entity.HasIndex(t => new { t.AccountId, t.ExecutionTimestamp });
        });

        modelBuilder.Entity<Position>(entity =>
        {
            entity.HasKey(p => p.Id);
            entity.Property(p => p.NetQuantity).HasPrecision(18, 8);
            entity.Property(p => p.AvgCostBasis).HasPrecision(18, 8);
            entity.Property(p => p.RealizedPnL).HasPrecision(18, 8);
            entity.Property(p => p.AssetType).HasConversion<string>().HasMaxLength(10);
            entity.Property(p => p.Status).HasConversion<string>().HasMaxLength(10);
            entity.HasOne(p => p.Account)
                  .WithMany(a => a.Positions)
                  .HasForeignKey(p => p.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(p => p.Ticker)
                  .WithMany()
                  .HasForeignKey(p => p.TickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(p => p.OptionContract)
                  .WithMany()
                  .HasForeignKey(p => p.OptionContractId)
                  .OnDelete(DeleteBehavior.SetNull);
            entity.HasIndex(p => new { p.AccountId, p.TickerId, p.Status });
        });

        modelBuilder.Entity<PositionLot>(entity =>
        {
            entity.HasKey(l => l.Id);
            entity.Property(l => l.Quantity).HasPrecision(18, 8);
            entity.Property(l => l.EntryPrice).HasPrecision(18, 8);
            entity.Property(l => l.RemainingQuantity).HasPrecision(18, 8);
            entity.Property(l => l.RealizedPnL).HasPrecision(18, 8);
            entity.HasOne(l => l.Position)
                  .WithMany(p => p.Lots)
                  .HasForeignKey(l => l.PositionId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(l => l.Trade)
                  .WithMany(t => t.Lots)
                  .HasForeignKey(l => l.TradeId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(l => new { l.PositionId, l.OpenedAt });
        });

        modelBuilder.Entity<OptionContract>(entity =>
        {
            entity.HasKey(c => c.Id);
            entity.Property(c => c.Symbol).IsRequired().HasMaxLength(100);
            entity.Property(c => c.Strike).HasPrecision(18, 8);
            entity.Property(c => c.OptionType).HasConversion<string>().HasMaxLength(10);
            entity.HasOne(c => c.UnderlyingTicker)
                  .WithMany()
                  .HasForeignKey(c => c.UnderlyingTickerId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(c => new { c.UnderlyingTickerId, c.Strike, c.Expiration, c.OptionType });
            entity.HasIndex(c => c.Symbol).IsUnique();
        });

        modelBuilder.Entity<OptionLeg>(entity =>
        {
            entity.HasKey(l => l.Id);
            entity.Property(l => l.Quantity).HasPrecision(18, 8);
            entity.Property(l => l.EntryIV).HasPrecision(18, 8);
            entity.Property(l => l.EntryDelta).HasPrecision(18, 8);
            entity.Property(l => l.EntryGamma).HasPrecision(18, 8);
            entity.Property(l => l.EntryTheta).HasPrecision(18, 8);
            entity.Property(l => l.EntryVega).HasPrecision(18, 8);
            entity.HasOne(l => l.Trade)
                  .WithOne(t => t.OptionLeg)
                  .HasForeignKey<OptionLeg>(l => l.TradeId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(l => l.OptionContract)
                  .WithMany()
                  .HasForeignKey(l => l.OptionContractId)
                  .OnDelete(DeleteBehavior.Cascade);
        });

        modelBuilder.Entity<PortfolioSnapshot>(entity =>
        {
            entity.HasKey(s => s.Id);
            entity.Property(s => s.Equity).HasPrecision(18, 8);
            entity.Property(s => s.Cash).HasPrecision(18, 8);
            entity.Property(s => s.MarketValue).HasPrecision(18, 8);
            entity.Property(s => s.MarginUsed).HasPrecision(18, 8);
            entity.Property(s => s.UnrealizedPnL).HasPrecision(18, 8);
            entity.Property(s => s.RealizedPnL).HasPrecision(18, 8);
            entity.Property(s => s.NetDelta).HasPrecision(18, 8);
            entity.Property(s => s.NetGamma).HasPrecision(18, 8);
            entity.Property(s => s.NetTheta).HasPrecision(18, 8);
            entity.Property(s => s.NetVega).HasPrecision(18, 8);
            entity.HasOne(s => s.Account)
                  .WithMany()
                  .HasForeignKey(s => s.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(s => new { s.AccountId, s.Timestamp });
        });

        modelBuilder.Entity<RiskRule>(entity =>
        {
            entity.HasKey(r => r.Id);
            entity.Property(r => r.Threshold).HasPrecision(18, 8);
            entity.Property(r => r.RuleType).HasConversion<string>().HasMaxLength(30);
            entity.Property(r => r.Action).HasConversion<string>().HasMaxLength(10);
            entity.Property(r => r.Severity).HasConversion<string>().HasMaxLength(10);
            entity.HasOne(r => r.Account)
                  .WithMany()
                  .HasForeignKey(r => r.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(r => new { r.AccountId, r.Enabled });
        });

        modelBuilder.Entity<StrategyAllocation>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.CapitalAllocated).HasPrecision(18, 8);
            entity.HasOne(a => a.Account)
                  .WithMany()
                  .HasForeignKey(a => a.AccountId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(a => a.StrategyExecution)
                  .WithMany()
                  .HasForeignKey(a => a.StrategyExecutionId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(a => new { a.AccountId, a.StrategyExecutionId });
        });

        modelBuilder.Entity<StrategyTradeLink>(entity =>
        {
            entity.HasKey(l => l.Id);
            entity.HasOne(l => l.Trade)
                  .WithMany()
                  .HasForeignKey(l => l.TradeId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasOne(l => l.StrategyExecution)
                  .WithMany()
                  .HasForeignKey(l => l.StrategyExecutionId)
                  .OnDelete(DeleteBehavior.Cascade);
            entity.HasIndex(l => l.StrategyExecutionId);
            entity.HasIndex(l => l.TradeId);
        });
    }

    /// <summary>
    /// Configure the data lake catalog tables. The CHECK constraints and
    /// partial unique indexes are declared in raw SQL because EF Core's
    /// fluent API does not express them natively. The migration's Up()
    /// emits the same SQL — this configuration block is the authoritative
    /// EF model state used by the schema-drift test on the Python side.
    /// </summary>
    private static void ConfigureDataLakeModels(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<DataLakeArtifact>(entity =>
        {
            entity.HasKey(a => a.Id);
            entity.Property(a => a.ArtifactKind).IsRequired().HasMaxLength(40);
            entity.Property(a => a.Provider).IsRequired().HasMaxLength(40);
            entity.Property(a => a.ProviderParams).IsRequired().HasColumnType("jsonb");
            entity.Property(a => a.DataContractHash).IsRequired().HasMaxLength(64).IsFixedLength();
            entity.Property(a => a.FilePath).IsRequired();
            entity.Property(a => a.Status).IsRequired().HasMaxLength(20);
            entity.Property(a => a.FetchedAtMs).IsRequired();
            entity.Property(a => a.AttemptCount).IsRequired().HasDefaultValue(0);

            entity.Property(a => a.FileSha256).HasMaxLength(64).IsFixedLength();
            entity.Property(a => a.CorpActionRevision).HasMaxLength(64).IsFixedLength();

            // Hot-path coverage lookup (partial indexes added via raw SQL in the migration).
            entity.HasIndex(a => new { a.Market, a.Symbol, a.Resolution, a.DataType, a.TradingDate });
        });

        modelBuilder.Entity<DataLakeRun>(entity =>
        {
            entity.HasKey(r => r.Id);
            entity.Property(r => r.RunType).IsRequired().HasMaxLength(20);
            entity.Property(r => r.RunSpec).IsRequired().HasColumnType("jsonb");
            entity.Property(r => r.RequestedAtMs).IsRequired();

            entity.Property(r => r.EnsureDataResponse).HasColumnType("jsonb");
            entity.Property(r => r.ManifestSha256).HasMaxLength(64).IsFixedLength();
            entity.Property(r => r.DataAvailabilityHash).HasMaxLength(64).IsFixedLength();

            entity.HasOne(r => r.StrategyExecution)
                  .WithMany()
                  .HasForeignKey(r => r.StrategyExecutionId)
                  .OnDelete(DeleteBehavior.SetNull);

            entity.HasIndex(r => r.StrategyExecutionId);
        });
    }
}
