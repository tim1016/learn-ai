using System;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class DropUnwrittenMarketDataTables : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "OptionsIvSnapshots");

            migrationBuilder.DropTable(
                name: "Quotes");

            migrationBuilder.DropTable(
                name: "ReferenceData");

            migrationBuilder.DropTable(
                name: "TechnicalIndicators");

            migrationBuilder.DropTable(
                name: "Trades");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "OptionsIvSnapshots",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    DteHigh = table.Column<int>(type: "integer", nullable: true),
                    DteLow = table.Column<int>(type: "integer", nullable: true),
                    Iv30dAtm = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Iv30dCall = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Iv30dPut = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    PriceSource = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Source = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    StockClose = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    TradingDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
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
                    AskExchange = table.Column<long>(type: "bigint", nullable: true),
                    AskPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AskSize = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BidExchange = table.Column<long>(type: "bigint", nullable: true),
                    BidPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BidSize = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    SequenceNumber = table.Column<long>(type: "bigint", nullable: true),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
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
                    CashAmount = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    DataType = table.Column<string>(type: "text", nullable: false),
                    DeclarationDate = table.Column<string>(type: "text", nullable: true),
                    EventDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ExecutionDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    MetadataJson = table.Column<string>(type: "text", nullable: true),
                    SplitFrom = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    SplitTo = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true)
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
                name: "TechnicalIndicators",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Histogram = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    IndicatorType = table.Column<string>(type: "text", nullable: false),
                    Signal = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Timespan = table.Column<string>(type: "text", nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Value = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    ValuesJson = table.Column<string>(type: "text", nullable: true),
                    Window = table.Column<int>(type: "integer", nullable: false)
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
                    Conditions = table.Column<string>(type: "text", nullable: true),
                    CreatedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    Exchange = table.Column<long>(type: "bigint", nullable: true),
                    Price = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    SequenceNumber = table.Column<long>(type: "bigint", nullable: true),
                    Size = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Timestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    TradeId = table.Column<string>(type: "text", nullable: true)
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

            migrationBuilder.CreateIndex(
                name: "IX_OptionsIvSnapshots_TickerId_TradingDate",
                table: "OptionsIvSnapshots",
                columns: new[] { "TickerId", "TradingDate" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_Quotes_TickerId_Timestamp",
                table: "Quotes",
                columns: new[] { "TickerId", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_ReferenceData_TickerId_DataType_EventDate",
                table: "ReferenceData",
                columns: new[] { "TickerId", "DataType", "EventDate" });

            migrationBuilder.CreateIndex(
                name: "IX_TechnicalIndicators_TickerId_IndicatorType_Timestamp",
                table: "TechnicalIndicators",
                columns: new[] { "TickerId", "IndicatorType", "Timestamp" });

            migrationBuilder.CreateIndex(
                name: "IX_Trades_TickerId_Timestamp",
                table: "Trades",
                columns: new[] { "TickerId", "Timestamp" });
        }
    }
}
