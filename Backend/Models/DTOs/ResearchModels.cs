namespace Backend.Models.DTOs;

#region Request DTOs (sent to Python service)

public class RunFeatureResearchRequest
{
    public required string Ticker { get; set; }
    public required string FeatureName { get; set; }
    public required List<OhlcvBarDto> Bars { get; set; }
    public required string StartDate { get; set; }
    public required string EndDate { get; set; }
}

#endregion

#region Response DTOs (received from Python service)

public class ResearchReportDto
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }

    public double MeanIc { get; set; }
    public double IcTStat { get; set; }
    public double IcPValue { get; set; }
    public List<double> IcValues { get; set; } = [];
    public List<string> IcDates { get; set; } = [];

    public double AdfPvalue { get; set; }
    public double KpssPvalue { get; set; }
    public bool IsStationary { get; set; }

    public List<QuantileBinDto> QuantileBins { get; set; } = [];
    public bool IsMonotonic { get; set; }
    public double MonotonicityRatio { get; set; }

    public bool PassedValidation { get; set; }
    public RobustnessDto? Robustness { get; set; }
    public string? Error { get; set; }
}

public class MonthlyICBreakdownDto
{
    public string Month { get; set; } = "";
    public double MeanIc { get; set; }
    public double TStat { get; set; }
    public int ObservationCount { get; set; }
}

public class RollingTStatPointDto
{
    public string Month { get; set; } = "";
    public double TStatSmoothed { get; set; }
}

public class RegimeICDto
{
    public string RegimeLabel { get; set; } = "";
    public double MeanIc { get; set; }
    public double TStat { get; set; }
    public int ObservationCount { get; set; }
}

public class TrainTestSplitDto
{
    public string TrainStart { get; set; } = "";
    public string TrainEnd { get; set; } = "";
    public string TestStart { get; set; } = "";
    public string TestEnd { get; set; } = "";
    public double TrainMeanIc { get; set; }
    public double TrainTStat { get; set; }
    public int TrainDays { get; set; }
    public double TestMeanIc { get; set; }
    public double TestTStat { get; set; }
    public int TestDays { get; set; }
    public bool OverfitFlag { get; set; }
}

public class RobustnessDto
{
    public List<MonthlyICBreakdownDto> MonthlyBreakdown { get; set; } = [];
    public double PctPositiveMonths { get; set; }
    public double PctSignificantMonths { get; set; }
    public double BestMonthIc { get; set; }
    public double WorstMonthIc { get; set; }
    public string StabilityLabel { get; set; } = "Unknown";
    public List<RollingTStatPointDto> RollingTStat { get; set; } = [];
    public List<RegimeICDto> VolatilityRegimes { get; set; } = [];
    public List<RegimeICDto> TrendRegimes { get; set; } = [];
    public TrainTestSplitDto? TrainTest { get; set; }
}

public class QuantileBinDto
{
    public int BinNumber { get; set; }
    public double LowerBound { get; set; }
    public double UpperBound { get; set; }
    public double MeanReturn { get; set; }
    public int Count { get; set; }
}

#endregion

#region GraphQL DTOs

public class ResearchExperimentDto
{
    public int Id { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }
    public double MeanIC { get; set; }
    public double ICTStat { get; set; }
    public double ICPValue { get; set; }
    public double AdfPValue { get; set; }
    public double KpssPValue { get; set; }
    public bool IsStationary { get; set; }
    public bool PassedValidation { get; set; }
    public double MonotonicityRatio { get; set; }
    public bool IsMonotonic { get; set; }
    public DateTime CreatedAt { get; set; }
}

#endregion
