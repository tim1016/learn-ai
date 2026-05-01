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
    public double NwTStat { get; set; }
    public double NwPValue { get; set; } = 1.0;
    public double EffectiveN { get; set; }
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
    public FeatureValidationSpecDto? FeatureSpec { get; set; }
    public FeatureValidationVerdictDto? ValidationVerdict { get; set; }
    public string? Error { get; set; }
}

public class FeatureValidationSpecDto
{
    public string FeatureName { get; set; } = "";
    public string DefaultTarget { get; set; } = "";
    public string ExpectedDirection { get; set; } = "unknown";
    public string ExpectedShape { get; set; } = "none";
    public bool StationarityRequired { get; set; }
    public bool MonotonicityRequired { get; set; }
    public string Intent { get; set; } = "";
    public List<string> Notes { get; set; } = [];
}

public class IcCiDto
{
    public double Point { get; set; }
    public double Se { get; set; }
    public double CiLower { get; set; }
    public double CiUpper { get; set; }
    public double ConfidenceLevel { get; set; } = 0.95;
    public double NEffUsed { get; set; }
    public bool Valid { get; set; }
    public string SeApproximationNote { get; set; } = "";
}

public class MultipleTestingWarningDto
{
    public double RawNwPValue { get; set; } = 1.0;
    public double HolmPValue { get; set; } = 1.0;
    public int NFamily { get; set; }
    public string Note { get; set; } = "";
}

public class CostViabilityDto
{
    public double GrossSpreadBps { get; set; }
    public double CostAssumptionOneWayBps { get; set; } = 1.0;
    public double CostErasureOneWayBps { get; set; }
    public double NetSpreadBpsAtAssumption { get; set; }
    public bool ViableAtAssumption { get; set; }
    public string Note { get; set; } = "";
}

public class ValidationScreenDto
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public bool Passed { get; set; }
    public bool RequiredForStage1 { get; set; }
    public List<string> FailureReasons { get; set; } = [];
}

public class FeatureStageCriterionDto
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public double CurrentValue { get; set; }
    public string RequiredRepr { get; set; } = "";
    public bool Met { get; set; }
}

public class FeatureStageInfoDto
{
    public int Stage { get; set; }
    public string Label { get; set; } = "Rejected";
    public string Description { get; set; } = "";
    public string NextStageLabel { get; set; } = "";
    public List<FeatureStageCriterionDto> AdvanceCriteria { get; set; } = [];
    public List<string> FailedScreens { get; set; } = [];
}

public class FeatureValidationVerdictDto
{
    public ValidationScreenDto StatisticalScreen { get; set; } = new();
    public ValidationScreenDto EconomicScreen { get; set; } = new();
    public ValidationScreenDto OosScreen { get; set; } = new();
    public ValidationScreenDto MultipleTestingScreen { get; set; } = new();
    public MultipleTestingWarningDto MultipleTesting { get; set; } = new();
    public CostViabilityDto CostViability { get; set; } = new();
    public IcCiDto IcCi { get; set; } = new();
    public FeatureStageInfoDto StageInfo { get; set; } = new();
    public string FinalDecision { get; set; } = "";
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
    public double OosRetention { get; set; }
    public string OosRetentionLabel { get; set; } = "Unknown";
}

public class StructuralBreakPointDto
{
    public string Date { get; set; } = "";
    public double IcBefore { get; set; }
    public double IcAfter { get; set; }
    public double TStat { get; set; }
    public bool Significant { get; set; }
}

public class RobustnessDto
{
    public List<MonthlyICBreakdownDto> MonthlyBreakdown { get; set; } = [];
    public double PctPositiveMonths { get; set; }
    public double PctSignificantMonths { get; set; }
    public double BestMonthIc { get; set; }
    public double WorstMonthIc { get; set; }
    public string StabilityLabel { get; set; } = "Unknown";
    public double PctSignConsistentMonths { get; set; }
    public string SignConsistentStabilityLabel { get; set; } = "Unknown";
    public List<RollingTStatPointDto> RollingTStat { get; set; } = [];
    public List<RegimeICDto> VolatilityRegimes { get; set; } = [];
    public List<RegimeICDto> TrendRegimes { get; set; } = [];
    public TrainTestSplitDto? TrainTest { get; set; }
    public List<StructuralBreakPointDto> StructuralBreaks { get; set; } = [];
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
