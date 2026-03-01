namespace Backend.Models.DTOs;

#region Request DTOs

public class RunOptionsFeatureResearchRequest
{
    public required string Ticker { get; set; }
    public required string FeatureName { get; set; }
    public required List<IvDataPointDto> IvData { get; set; }
    public required List<OhlcvBarDto> StockDailyBars { get; set; }
    public required string StartDate { get; set; }
    public required string EndDate { get; set; }
    public string TargetType { get; set; } = "directional";
}

public class IvDataPointDto
{
    public string Date { get; set; } = "";
    public double? AtmIv { get; set; }
    public double? IvOtmPut { get; set; }
    public double? IvOtmCall { get; set; }
    public double? StockClose { get; set; }
}

public class RunBatchOptionsResearchRequest
{
    public required string FeatureName { get; set; }
    public required List<string> Tickers { get; set; }
    public required string StartDate { get; set; }
    public required string EndDate { get; set; }
    public string TargetType { get; set; } = "directional";
}

public class BuildIvHistoryRequest
{
    public required string UnderlyingTicker { get; set; }
    public required string StartDate { get; set; }
    public required string EndDate { get; set; }
}

#endregion

#region Response DTOs

public class BuildIvHistoryResponseDto
{
    public bool Success { get; set; }
    public string UnderlyingTicker { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int DataPoints { get; set; }
    public List<Dictionary<string, object?>> IvData { get; set; } = [];
    public IvDiagnosticsDto? Diagnostics { get; set; }
    public string? Error { get; set; }
}

public class IvDiagnosticsDto
{
    public bool Valid { get; set; }
    public double MissingPct { get; set; }
    public int TotalTradingDays { get; set; }
    public int ValidIvDays { get; set; }
    public string? FirstDate { get; set; }
    public string? LastDate { get; set; }
    public int Gaps { get; set; }
    public int DteSpikes { get; set; }
    public double? IvMean { get; set; }
    public double? IvStd { get; set; }
    public double? IvMin { get; set; }
    public double? IvMax { get; set; }
    public double? IvSkewness { get; set; }
    public int Discontinuities { get; set; }
    public List<string> Warnings { get; set; } = [];
}

public class BatchResearchResultDto
{
    public bool Success { get; set; }
    public string FeatureName { get; set; } = "";
    public int TickersTested { get; set; }
    public int TickersPassed { get; set; }
    public double PassRate { get; set; }
    public bool CrossSectionalConsistent { get; set; }
    public double AggregateIc { get; set; }
    public List<TickerBatchResultDto> TickerResults { get; set; } = [];
    public string Summary { get; set; } = "";
    public string? Error { get; set; }
}

public class TickerBatchResultDto
{
    public string Ticker { get; set; } = "";
    public double MeanIc { get; set; }
    public double IcTStat { get; set; }
    public double IcPValue { get; set; } = 1.0;
    public double NwTStat { get; set; }
    public double NwPValue { get; set; } = 1.0;
    public double EffectiveN { get; set; }
    public bool IsStationary { get; set; }
    public bool PassedValidation { get; set; }
    public int DataPoints { get; set; }
    public string? Error { get; set; }
}

#endregion
