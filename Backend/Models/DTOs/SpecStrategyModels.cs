using System.Text.Json;
using System.Text.Json.Nodes;

namespace Backend.Models.DTOs;

/// <summary>
/// Request for /api/spec-strategy/backtest. The <c>Spec</c> is a pre-
/// validated JSON string carrying a serialized <c>StrategySpec</c>;
/// Backend passes it through to Python without re-validating because
/// the Pydantic schema in PythonDataService is the source of truth.
/// </summary>
public record SpecBacktestRequestDto(
    string Spec,
    string StartDate,
    string EndDate,
    decimal InitialCash = 100000m,
    string FillMode = "signal_bar_close",
    decimal CommissionPerOrder = 0m
);

/// <summary>
/// Single trade emitted by a spec backtest.
///
/// Timestamps are int64 ms UTC (per the repo-wide wire-format rule —
/// see .claude/rules/numerical-rigor.md § "Timestamp rigor"). UI callers
/// convert to local-time strings at the display boundary; intermediate
/// layers (this DTO, the GraphQL output type, the TS service) all carry
/// the canonical long form.
/// </summary>
public record SpecTradeDto(
    int TradeNumber,
    long EntryTime,
    decimal EntryPrice,
    long ExitTime,
    decimal ExitPrice,
    Dictionary<string, decimal> Indicators,
    decimal PnlPts,
    decimal PnlPct,
    string Result,
    string SignalReason
);

/// <summary>
/// Response shape for /api/spec-strategy/backtest. Mirrors
/// SpecBacktestResponse on the Python side.
/// </summary>
public record SpecBacktestResponseDto(
    bool Success,
    string StrategyName,
    decimal InitialCash,
    decimal FinalEquity,
    decimal NetProfit,
    decimal TotalFees,
    int TotalTrades,
    int WinningTrades,
    int LosingTrades,
    decimal WinRate,
    List<SpecTradeDto> Trades,
    List<string> LogLines,
    string? Error
);
