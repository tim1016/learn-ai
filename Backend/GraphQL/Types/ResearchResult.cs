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
    public string? Error { get; set; }
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
    public List<RollingTStatPointType> RollingTStat { get; set; } = [];
    public List<RegimeICType> VolatilityRegimes { get; set; } = [];
    public List<RegimeICType> TrendRegimes { get; set; } = [];
    public TrainTestSplitType? TrainTest { get; set; }
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
