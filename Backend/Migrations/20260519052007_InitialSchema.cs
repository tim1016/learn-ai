using System;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class InitialSchema : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "Accounts",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    Name = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    Type = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    BaseCurrency = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    InitialCash = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Cash = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Accounts", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "DataLabSessions",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    Name = table.Column<string>(type: "character varying(300)", maxLength: 300, nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    UpdatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Ticker = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    FromDate = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    ToDate = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Session = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    ForwardFill = table.Column<bool>(type: "boolean", nullable: false),
                    Adjusted = table.Column<bool>(type: "boolean", nullable: false),
                    EntriesJson = table.Column<string>(type: "jsonb", nullable: false),
                    ChartSnapshotJson = table.Column<string>(type: "jsonb", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_DataLabSessions", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "Tickers",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    Symbol = table.Column<string>(type: "character varying(50)", maxLength: 50, nullable: false),
                    Name = table.Column<string>(type: "character varying(500)", maxLength: 500, nullable: false),
                    Market = table.Column<string>(type: "character varying(50)", maxLength: 50, nullable: false),
                    Locale = table.Column<string>(type: "text", nullable: true),
                    PrimaryExchange = table.Column<string>(type: "text", nullable: true),
                    Type = table.Column<string>(type: "text", nullable: true),
                    Active = table.Column<bool>(type: "boolean", nullable: false),
                    CurrencySymbol = table.Column<string>(type: "text", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    UpdatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    SanitizationSummary = table.Column<string>(type: "text", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Tickers", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "PortfolioSnapshots",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Equity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Cash = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    MarketValue = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    MarginUsed = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    UnrealizedPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    RealizedPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    NetDelta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    NetGamma = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    NetTheta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    NetVega = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_PortfolioSnapshots", x => x.Id);
                    table.ForeignKey(
                        name: "FK_PortfolioSnapshots_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "RiskRules",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    RuleType = table.Column<string>(type: "character varying(30)", maxLength: 30, nullable: false),
                    Threshold = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Action = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Severity = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Enabled = table.Column<bool>(type: "boolean", nullable: false),
                    LastTriggered = table.Column<DateTime>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RiskRules", x => x.Id);
                    table.ForeignKey(
                        name: "FK_RiskRules_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "OptionContracts",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    UnderlyingTickerId = table.Column<int>(type: "integer", nullable: false),
                    Symbol = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Strike = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Expiration = table.Column<DateOnly>(type: "date", nullable: false),
                    OptionType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Multiplier = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_OptionContracts", x => x.Id);
                    table.ForeignKey(
                        name: "FK_OptionContracts_Tickers_UnderlyingTickerId",
                        column: x => x.UnderlyingTickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "OptionsIvSnapshots",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    TradingDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Iv30dAtm = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Iv30dPut = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Iv30dCall = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    StockClose = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    DteLow = table.Column<int>(type: "integer", nullable: true),
                    DteHigh = table.Column<int>(type: "integer", nullable: true),
                    PriceSource = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Source = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_OptionsIvSnapshots", x => x.Id);
                    table.ForeignKey(
                        name: "FK_OptionsIvSnapshots_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "Quotes",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    BidPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AskPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BidSize = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AskSize = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    BidExchange = table.Column<long>(type: "bigint", nullable: true),
                    AskExchange = table.Column<long>(type: "bigint", nullable: true),
                    SequenceNumber = table.Column<long>(type: "bigint", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Quotes", x => x.Id);
                    table.ForeignKey(
                        name: "FK_Quotes_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "ReferenceData",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    DataType = table.Column<string>(type: "text", nullable: false),
                    EventDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ExecutionDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    CashAmount = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    DeclarationDate = table.Column<string>(type: "text", nullable: true),
                    SplitFrom = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    SplitTo = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    MetadataJson = table.Column<string>(type: "text", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_ReferenceData", x => x.Id);
                    table.ForeignKey(
                        name: "FK_ReferenceData_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "ResearchExperiments",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    FeatureName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    StartDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    EndDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    BarsUsed = table.Column<int>(type: "integer", nullable: false),
                    MeanIC = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ICTStat = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ICPValue = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AdfPValue = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    KpssPValue = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    IsStationary = table.Column<bool>(type: "boolean", nullable: false),
                    PassedValidation = table.Column<bool>(type: "boolean", nullable: false),
                    MonotonicityRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    IsMonotonic = table.Column<bool>(type: "boolean", nullable: false),
                    JsonReport = table.Column<string>(type: "text", nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_ResearchExperiments", x => x.Id);
                    table.ForeignKey(
                        name: "FK_ResearchExperiments_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "SignalExperiments",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    FeatureName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    StartDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    EndDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    BarsUsed = table.Column<int>(type: "integer", nullable: false),
                    OverallGrade = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    StatusLabel = table.Column<string>(type: "character varying(50)", maxLength: 50, nullable: false),
                    OverallPassed = table.Column<bool>(type: "boolean", nullable: false),
                    MeanOosSharpe = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BestThreshold = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BestCostBps = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    FlipSign = table.Column<bool>(type: "boolean", nullable: false),
                    RegimeGateEnabled = table.Column<bool>(type: "boolean", nullable: false),
                    JsonReport = table.Column<string>(type: "text", nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_SignalExperiments", x => x.Id);
                    table.ForeignKey(
                        name: "FK_SignalExperiments_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "StockAggregates",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    Open = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    High = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Low = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Close = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Volume = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    VolumeWeightedAveragePrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Timespan = table.Column<string>(type: "text", nullable: false),
                    Multiplier = table.Column<int>(type: "integer", nullable: false),
                    TransactionCount = table.Column<long>(type: "bigint", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StockAggregates", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StockAggregates_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "StrategyExecutions",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    StrategyName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Parameters = table.Column<string>(type: "text", nullable: false),
                    StartDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    EndDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Timespan = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Multiplier = table.Column<int>(type: "integer", nullable: false),
                    TotalTrades = table.Column<int>(type: "integer", nullable: false),
                    WinningTrades = table.Column<int>(type: "integer", nullable: false),
                    LosingTrades = table.Column<int>(type: "integer", nullable: false),
                    TotalPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    MaxDrawdown = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    SharpeRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    InitialCash = table.Column<decimal>(type: "numeric(18,2)", precision: 18, scale: 2, nullable: false),
                    FinalEquity = table.Column<decimal>(type: "numeric(18,2)", precision: 18, scale: 2, nullable: false),
                    TotalFees = table.Column<decimal>(type: "numeric(18,4)", precision: 18, scale: 4, nullable: false),
                    WinRate = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    CompoundingAnnualReturn = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    SortinoRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ProbabilisticSharpeRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ProfitFactor = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Alpha = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Beta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    InformationRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    TrackingError = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    TreynorRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ValueAtRisk95 = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ValueAtRisk99 = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AnnualStandardDeviation = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    DrawdownRecoveryDays = table.Column<int>(type: "integer", nullable: false),
                    LeanStatisticsJson = table.Column<string>(type: "jsonb", nullable: true),
                    Source = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Notes = table.Column<string>(type: "text", nullable: true),
                    FillMode = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    ExecutedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    DurationMs = table.Column<long>(type: "bigint", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyExecutions", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyExecutions_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "TechnicalIndicators",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    IndicatorType = table.Column<string>(type: "text", nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Timespan = table.Column<string>(type: "text", nullable: false),
                    Window = table.Column<int>(type: "integer", nullable: false),
                    Value = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Signal = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Histogram = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    ValuesJson = table.Column<string>(type: "text", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_TechnicalIndicators", x => x.Id);
                    table.ForeignKey(
                        name: "FK_TechnicalIndicators_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "Trades",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    Price = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Size = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Exchange = table.Column<long>(type: "bigint", nullable: true),
                    Conditions = table.Column<string>(type: "text", nullable: true),
                    SequenceNumber = table.Column<long>(type: "bigint", nullable: true),
                    TradeId = table.Column<string>(type: "text", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Trades", x => x.Id);
                    table.ForeignKey(
                        name: "FK_Trades_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "Orders",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    Side = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    OrderType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    LimitPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Status = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    AssetType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    OptionContractId = table.Column<Guid>(type: "uuid", nullable: true),
                    SubmittedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    FilledAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Orders", x => x.Id);
                    table.ForeignKey(
                        name: "FK_Orders_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_Orders_OptionContracts_OptionContractId",
                        column: x => x.OptionContractId,
                        principalTable: "OptionContracts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_Orders_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "Positions",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    AssetType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    OptionContractId = table.Column<Guid>(type: "uuid", nullable: true),
                    NetQuantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AvgCostBasis = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    RealizedPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Status = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    OpenedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ClosedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    LastUpdated = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Positions", x => x.Id);
                    table.ForeignKey(
                        name: "FK_Positions_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_Positions_OptionContracts_OptionContractId",
                        column: x => x.OptionContractId,
                        principalTable: "OptionContracts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_Positions_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "BacktestTrades",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false),
                    TradeType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    EntryTimestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ExitTimestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    EntryPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ExitPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    PnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    CumulativePnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    SignalReason = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_BacktestTrades", x => x.Id);
                    table.ForeignKey(
                        name: "FK_BacktestTrades_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "StrategyAllocations",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false),
                    CapitalAllocated = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    StartDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    EndDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyAllocations", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyAllocations_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_StrategyAllocations_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "PortfolioTrades",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    OrderId = table.Column<Guid>(type: "uuid", nullable: false),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    Side = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Price = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Fees = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AssetType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false),
                    OptionContractId = table.Column<Guid>(type: "uuid", nullable: true),
                    Multiplier = table.Column<int>(type: "integer", nullable: false),
                    ExecutionTimestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_PortfolioTrades", x => x.Id);
                    table.ForeignKey(
                        name: "FK_PortfolioTrades_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_PortfolioTrades_OptionContracts_OptionContractId",
                        column: x => x.OptionContractId,
                        principalTable: "OptionContracts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_PortfolioTrades_Orders_OrderId",
                        column: x => x.OrderId,
                        principalTable: "Orders",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_PortfolioTrades_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "OptionLegs",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    TradeId = table.Column<Guid>(type: "uuid", nullable: false),
                    OptionContractId = table.Column<Guid>(type: "uuid", nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    EntryIV = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    EntryDelta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    EntryGamma = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    EntryTheta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    EntryVega = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_OptionLegs", x => x.Id);
                    table.ForeignKey(
                        name: "FK_OptionLegs_OptionContracts_OptionContractId",
                        column: x => x.OptionContractId,
                        principalTable: "OptionContracts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_OptionLegs_PortfolioTrades_TradeId",
                        column: x => x.TradeId,
                        principalTable: "PortfolioTrades",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "PositionLots",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    PositionId = table.Column<Guid>(type: "uuid", nullable: false),
                    TradeId = table.Column<Guid>(type: "uuid", nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    EntryPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    RemainingQuantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    RealizedPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    OpenedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ClosedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_PositionLots", x => x.Id);
                    table.ForeignKey(
                        name: "FK_PositionLots_PortfolioTrades_TradeId",
                        column: x => x.TradeId,
                        principalTable: "PortfolioTrades",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_PositionLots_Positions_PositionId",
                        column: x => x.PositionId,
                        principalTable: "Positions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "StrategyTradeLinks",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    TradeId = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyTradeLinks", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyTradeLinks_PortfolioTrades_TradeId",
                        column: x => x.TradeId,
                        principalTable: "PortfolioTrades",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_StrategyTradeLinks_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_BacktestTrades_StrategyExecutionId",
                table: "BacktestTrades",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_DataLabSessions_Ticker",
                table: "DataLabSessions",
                column: "Ticker");

            migrationBuilder.CreateIndex(
                name: "IX_DataLabSessions_UpdatedAt",
                table: "DataLabSessions",
                column: "UpdatedAt");

            migrationBuilder.CreateIndex(
                name: "IX_OptionContracts_Symbol",
                table: "OptionContracts",
                column: "Symbol",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_OptionContracts_UnderlyingTickerId_Strike_Expiration_Option~",
                table: "OptionContracts",
                columns: new[] { "UnderlyingTickerId", "Strike", "Expiration", "OptionType" });

            migrationBuilder.CreateIndex(
                name: "IX_OptionLegs_OptionContractId",
                table: "OptionLegs",
                column: "OptionContractId");

            migrationBuilder.CreateIndex(
                name: "IX_OptionLegs_TradeId",
                table: "OptionLegs",
                column: "TradeId",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_OptionsIvSnapshots_TickerId_TradingDate",
                table: "OptionsIvSnapshots",
                columns: new[] { "TickerId", "TradingDate" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_Orders_AccountId_Status",
                table: "Orders",
                columns: new[] { "AccountId", "Status" });

            migrationBuilder.CreateIndex(
                name: "IX_Orders_OptionContractId",
                table: "Orders",
                column: "OptionContractId");

            migrationBuilder.CreateIndex(
                name: "IX_Orders_TickerId",
                table: "Orders",
                column: "TickerId");

            migrationBuilder.CreateIndex(
                name: "IX_PortfolioSnapshots_AccountId_Timestamp",
                table: "PortfolioSnapshots",
                columns: new[] { "AccountId", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_PortfolioTrades_AccountId_ExecutionTimestamp",
                table: "PortfolioTrades",
                columns: new[] { "AccountId", "ExecutionTimestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_PortfolioTrades_OptionContractId",
                table: "PortfolioTrades",
                column: "OptionContractId");

            migrationBuilder.CreateIndex(
                name: "IX_PortfolioTrades_OrderId",
                table: "PortfolioTrades",
                column: "OrderId");

            migrationBuilder.CreateIndex(
                name: "IX_PortfolioTrades_TickerId",
                table: "PortfolioTrades",
                column: "TickerId");

            migrationBuilder.CreateIndex(
                name: "IX_PositionLots_PositionId_OpenedAt",
                table: "PositionLots",
                columns: new[] { "PositionId", "OpenedAt" });

            migrationBuilder.CreateIndex(
                name: "IX_PositionLots_TradeId",
                table: "PositionLots",
                column: "TradeId");

            migrationBuilder.CreateIndex(
                name: "IX_Positions_AccountId_TickerId_Status",
                table: "Positions",
                columns: new[] { "AccountId", "TickerId", "Status" });

            migrationBuilder.CreateIndex(
                name: "IX_Positions_OptionContractId",
                table: "Positions",
                column: "OptionContractId");

            migrationBuilder.CreateIndex(
                name: "IX_Positions_TickerId",
                table: "Positions",
                column: "TickerId");

            migrationBuilder.CreateIndex(
                name: "IX_Quotes_TickerId_Timestamp",
                table: "Quotes",
                columns: new[] { "TickerId", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_ReferenceData_TickerId_DataType_EventDate",
                table: "ReferenceData",
                columns: new[] { "TickerId", "DataType", "EventDate" });

            migrationBuilder.CreateIndex(
                name: "IX_ResearchExperiments_TickerId_FeatureName_CreatedAt",
                table: "ResearchExperiments",
                columns: new[] { "TickerId", "FeatureName", "CreatedAt" });

            migrationBuilder.CreateIndex(
                name: "IX_RiskRules_AccountId_Enabled",
                table: "RiskRules",
                columns: new[] { "AccountId", "Enabled" });

            migrationBuilder.CreateIndex(
                name: "IX_SignalExperiments_TickerId_FeatureName_CreatedAt",
                table: "SignalExperiments",
                columns: new[] { "TickerId", "FeatureName", "CreatedAt" });

            migrationBuilder.CreateIndex(
                name: "IX_StockAggregates_TickerId_Timestamp_Timespan",
                table: "StockAggregates",
                columns: new[] { "TickerId", "Timestamp", "Timespan" });

            migrationBuilder.CreateIndex(
                name: "IX_StockAggregates_Timestamp",
                table: "StockAggregates",
                column: "Timestamp");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyAllocations_AccountId_StrategyExecutionId",
                table: "StrategyAllocations",
                columns: new[] { "AccountId", "StrategyExecutionId" });

            migrationBuilder.CreateIndex(
                name: "IX_StrategyAllocations_StrategyExecutionId",
                table: "StrategyAllocations",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_ExecutedAt",
                table: "StrategyExecutions",
                column: "ExecutedAt");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_Source",
                table: "StrategyExecutions",
                column: "Source");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_TickerId_StrategyName",
                table: "StrategyExecutions",
                columns: new[] { "TickerId", "StrategyName" });

            migrationBuilder.CreateIndex(
                name: "IX_StrategyTradeLinks_StrategyExecutionId",
                table: "StrategyTradeLinks",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyTradeLinks_TradeId",
                table: "StrategyTradeLinks",
                column: "TradeId");

            migrationBuilder.CreateIndex(
                name: "IX_TechnicalIndicators_TickerId_IndicatorType_Timestamp",
                table: "TechnicalIndicators",
                columns: new[] { "TickerId", "IndicatorType", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_Tickers_Symbol",
                table: "Tickers",
                column: "Symbol");

            migrationBuilder.CreateIndex(
                name: "IX_Tickers_Symbol_Market",
                table: "Tickers",
                columns: new[] { "Symbol", "Market" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_Trades_TickerId_Timestamp",
                table: "Trades",
                columns: new[] { "TickerId", "Timestamp" });
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "BacktestTrades");

            migrationBuilder.DropTable(
                name: "DataLabSessions");

            migrationBuilder.DropTable(
                name: "OptionLegs");

            migrationBuilder.DropTable(
                name: "OptionsIvSnapshots");

            migrationBuilder.DropTable(
                name: "PortfolioSnapshots");

            migrationBuilder.DropTable(
                name: "PositionLots");

            migrationBuilder.DropTable(
                name: "Quotes");

            migrationBuilder.DropTable(
                name: "ReferenceData");

            migrationBuilder.DropTable(
                name: "ResearchExperiments");

            migrationBuilder.DropTable(
                name: "RiskRules");

            migrationBuilder.DropTable(
                name: "SignalExperiments");

            migrationBuilder.DropTable(
                name: "StockAggregates");

            migrationBuilder.DropTable(
                name: "StrategyAllocations");

            migrationBuilder.DropTable(
                name: "StrategyTradeLinks");

            migrationBuilder.DropTable(
                name: "TechnicalIndicators");

            migrationBuilder.DropTable(
                name: "Trades");

            migrationBuilder.DropTable(
                name: "Positions");

            migrationBuilder.DropTable(
                name: "PortfolioTrades");

            migrationBuilder.DropTable(
                name: "StrategyExecutions");

            migrationBuilder.DropTable(
                name: "Orders");

            migrationBuilder.DropTable(
                name: "Accounts");

            migrationBuilder.DropTable(
                name: "OptionContracts");

            migrationBuilder.DropTable(
                name: "Tickers");
        }
    }
}
