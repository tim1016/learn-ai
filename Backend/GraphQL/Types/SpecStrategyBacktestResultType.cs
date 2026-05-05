using Backend.Models.DTOs;

namespace Backend.GraphQL.Types;

public class SpecStrategyTradeType
{
    public int TradeNumber { get; set; }
    /// <summary>Entry fill time as int64 ms since Unix epoch UTC.</summary>
    public long EntryTime { get; set; }
    public decimal EntryPrice { get; set; }
    /// <summary>Exit fill time as int64 ms since Unix epoch UTC.</summary>
    public long ExitTime { get; set; }
    public decimal ExitPrice { get; set; }
    public Dictionary<string, decimal> Indicators { get; set; } = [];
    public decimal PnlPts { get; set; }
    public decimal PnlPct { get; set; }
    public string Result { get; set; } = string.Empty;
    public string SignalReason { get; set; } = string.Empty;

    public static SpecStrategyTradeType FromDto(SpecTradeDto dto) => new()
    {
        TradeNumber = dto.TradeNumber,
        EntryTime = dto.EntryTime,
        EntryPrice = dto.EntryPrice,
        ExitTime = dto.ExitTime,
        ExitPrice = dto.ExitPrice,
        Indicators = dto.Indicators,
        PnlPts = dto.PnlPts,
        PnlPct = dto.PnlPct,
        Result = dto.Result,
        SignalReason = dto.SignalReason,
    };
}

public class SpecStrategyBacktestResultType
{
    public bool Success { get; set; }
    public string StrategyName { get; set; } = string.Empty;
    public decimal InitialCash { get; set; }
    public decimal FinalEquity { get; set; }
    public decimal NetProfit { get; set; }
    public decimal TotalFees { get; set; }
    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }
    public decimal WinRate { get; set; }
    public List<SpecStrategyTradeType> Trades { get; set; } = [];
    public List<string> LogLines { get; set; } = [];
    public string? Error { get; set; }

    public static SpecStrategyBacktestResultType FromDto(SpecBacktestResponseDto dto) => new()
    {
        Success = dto.Success,
        StrategyName = dto.StrategyName,
        InitialCash = dto.InitialCash,
        FinalEquity = dto.FinalEquity,
        NetProfit = dto.NetProfit,
        TotalFees = dto.TotalFees,
        TotalTrades = dto.TotalTrades,
        WinningTrades = dto.WinningTrades,
        LosingTrades = dto.LosingTrades,
        WinRate = dto.WinRate,
        Trades = dto.Trades.Select(SpecStrategyTradeType.FromDto).ToList(),
        LogLines = dto.LogLines,
        Error = dto.Error,
    };
}
