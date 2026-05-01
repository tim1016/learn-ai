using HotChocolate;

namespace Backend.GraphQL.Types;

public class ResearchResultType
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }

    [GraphQLName("meanIC")]
    public double MeanIC { get; set; }

    [GraphQLName("icTStat")]
    public double ICTStat { get; set; }

    [GraphQLName("icPValue")]
    public double ICPValue { get; set; }

    [GraphQLName("icValues")]
    public List<double> ICValues { get; set; } = [];

    [GraphQLName("icDates")]
    public List<string> ICDates { get; set; } = [];

    [GraphQLName("nwTStat")]
    public double NwTStat { get; set; }

    [GraphQLName("nwPValue")]
    public double NwPValue { get; set; }

    public double EffectiveN { get; set; }

    [GraphQLName("adfPvalue")]
    public double AdfPvalue { get; set; }

    [GraphQLName("kpssPvalue")]
    public double KpssPvalue { get; set; }

    public bool IsStationary { get; set; }

    public List<QuantileBinType> QuantileBins { get; set; } = [];
    public bool IsMonotonic { get; set; }
    public double MonotonicityRatio { get; set; }

    public bool PassedValidation { get; set; }
    public RobustnessType? Robustness { get; set; }
    public FeatureValidationSpecType? FeatureSpec { get; set; }
    public TargetMetadataType? TargetMetadata { get; set; }
    public FeatureValidationVerdictType? ValidationVerdict { get; set; }
    public string? Error { get; set; }
}

public class FeatureValidationSpecType
{
    public string FeatureName { get; set; } = "";
    public string DefaultTarget { get; set; } = "";
    public string ExpectedDirection { get; set; } = "unknown";
    public string ExpectedShape { get; set; } = "none";
    public bool StationarityRequired { get; set; }
    public bool MonotonicityRequired { get; set; }
    public bool IsSignedTargetAppropriate { get; set; } = true;
    public string Intent { get; set; } = "";
    public List<string> Notes { get; set; } = [];
}

public class TargetMetadataType
{
    public string TargetName { get; set; } = "forward_log_return_15m";
    public int HorizonMinutes { get; set; } = 15;
    public int HorizonBars { get; set; } = 15;
    public int BarMinutes { get; set; } = 1;
    public string Timezone { get; set; } = "America/New_York";
    public int ValidCount { get; set; }
    public int TotalCount { get; set; }
    public double ValidRatio { get; set; }

    // Hot Chocolate v15 exposes Dictionary<string, int> as
    // KeyValuePairOfStringAndInt32 which forces clients to select
    // sub-fields. Project to a list-of-DTO at the GraphQL boundary,
    // matching the established pattern for RegimeCoverageEntry.
    public List<InvalidReasonCountType> InvalidReasonCounts { get; set; } = [];
}

public class InvalidReasonCountType
{
    public string Reason { get; set; } = "";
    public int Count { get; set; }
}

public class IcCiType
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

public class MultipleTestingWarningType
{
    [GraphQLName("rawNwPValue")]
    public double RawNwPValue { get; set; } = 1.0;

    [GraphQLName("holmPValue")]
    public double HolmPValue { get; set; } = 1.0;

    public int NFamily { get; set; }
    public string Note { get; set; } = "";
}

public class CostViabilityType
{
    public double GrossSpreadBpsSigned { get; set; }
    public double DirectionalSpreadBps { get; set; }
    public double CostAssumptionOneWayBps { get; set; } = 1.0;
    public double CostErasureOneWayBps { get; set; }
    public double NetSpreadBpsAtAssumption { get; set; }
    public bool ViableAtAssumption { get; set; }
    public string SpecDirection { get; set; } = "unknown";
    public string Note { get; set; } = "";
}

public class ValidationScreenType
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public bool Passed { get; set; }
    public bool RequiredForStage1 { get; set; }
    public List<string> FailureReasons { get; set; } = [];
}

public class FeatureStageCriterionType
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public double CurrentValue { get; set; }
    public string RequiredRepr { get; set; } = "";
    public bool Met { get; set; }
}

public class FeatureStageInfoType
{
    public int Stage { get; set; }
    public string Label { get; set; } = "Rejected";
    public string Description { get; set; } = "";
    public string NextStageLabel { get; set; } = "";
    public List<FeatureStageCriterionType> AdvanceCriteria { get; set; } = [];
    public List<string> FailedScreens { get; set; } = [];
}

public class FeatureValidationVerdictType
{
    public ValidationScreenType StatisticalScreen { get; set; } = new();
    public ValidationScreenType EconomicScreen { get; set; } = new();
    public ValidationScreenType OosScreen { get; set; } = new();
    public ValidationScreenType MultipleTestingScreen { get; set; } = new();
    public ValidationScreenType RegimeStabilityScreen { get; set; } = new();
    public MultipleTestingWarningType MultipleTesting { get; set; } = new();
    public CostViabilityType CostViability { get; set; } = new();
    public IcCiType IcCi { get; set; } = new();
    public bool DirectionMatchesSpec { get; set; } = true;
    public bool TargetSignedAppropriate { get; set; } = true;
    public FeatureStageInfoType StageInfo { get; set; } = new();
    public string FinalDecision { get; set; } = "";
}

public class MonthlyICBreakdownType
{
    public string Month { get; set; } = "";

    [GraphQLName("meanIC")]
    public double MeanIC { get; set; }

    [GraphQLName("tStat")]
    public double TStat { get; set; }

    public int ObservationCount { get; set; }
}

public class RollingTStatPointType
{
    public string Month { get; set; } = "";

    [GraphQLName("tStatSmoothed")]
    public double TStatSmoothed { get; set; }
}

public class RegimeICType
{
    public string RegimeLabel { get; set; } = "";

    [GraphQLName("meanIC")]
    public double MeanIC { get; set; }

    [GraphQLName("tStat")]
    public double TStat { get; set; }

    public int ObservationCount { get; set; }
}

public class TrainTestSplitType
{
    public string TrainStart { get; set; } = "";
    public string TrainEnd { get; set; } = "";
    public string TestStart { get; set; } = "";
    public string TestEnd { get; set; } = "";

    [GraphQLName("trainMeanIC")]
    public double TrainMeanIC { get; set; }

    [GraphQLName("trainTStat")]
    public double TrainTStat { get; set; }

    public int TrainDays { get; set; }

    [GraphQLName("testMeanIC")]
    public double TestMeanIC { get; set; }

    [GraphQLName("testTStat")]
    public double TestTStat { get; set; }

    public int TestDays { get; set; }
    public bool OverfitFlag { get; set; }
    public double OosRetention { get; set; }
    public string OosRetentionLabel { get; set; } = "Unknown";
}

public class StructuralBreakPointType
{
    public string Date { get; set; } = "";

    [GraphQLName("icBefore")]
    public double IcBefore { get; set; }

    [GraphQLName("icAfter")]
    public double IcAfter { get; set; }

    [GraphQLName("tStat")]
    public double TStat { get; set; }

    public bool Significant { get; set; }
}

public class RobustnessType
{
    public List<MonthlyICBreakdownType> MonthlyBreakdown { get; set; } = [];
    public double PctPositiveMonths { get; set; }
    public double PctSignificantMonths { get; set; }

    [GraphQLName("bestMonthIC")]
    public double BestMonthIC { get; set; }

    [GraphQLName("worstMonthIC")]
    public double WorstMonthIC { get; set; }

    public string StabilityLabel { get; set; } = "Unknown";
    public double PctSignConsistentMonths { get; set; }
    public string SignConsistentStabilityLabel { get; set; } = "Unknown";
    public List<RollingTStatPointType> RollingTStat { get; set; } = [];
    public List<RegimeICType> VolatilityRegimes { get; set; } = [];
    public List<RegimeICType> TrendRegimes { get; set; } = [];
    public TrainTestSplitType? TrainTest { get; set; }
    public List<StructuralBreakPointType> StructuralBreaks { get; set; } = [];
}

public class QuantileBinType
{
    public int BinNumber { get; set; }
    public double LowerBound { get; set; }
    public double UpperBound { get; set; }
    public double MeanReturn { get; set; }
    public int Count { get; set; }
}

public class ResearchExperimentType
{
    public int Id { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }

    [GraphQLName("meanIC")]
    public double MeanIC { get; set; }

    [GraphQLName("icTStat")]
    public double ICTStat { get; set; }

    [GraphQLName("icPValue")]
    public double ICPValue { get; set; }

    [GraphQLName("adfPValue")]
    public double AdfPValue { get; set; }

    [GraphQLName("kpssPValue")]
    public double KpssPValue { get; set; }

    public bool IsStationary { get; set; }
    public bool PassedValidation { get; set; }
    public double MonotonicityRatio { get; set; }
    public bool IsMonotonic { get; set; }
    public DateTime CreatedAt { get; set; }
}
