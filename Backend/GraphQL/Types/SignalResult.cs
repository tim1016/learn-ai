using HotChocolate;

namespace Backend.GraphQL.Types;

public class SignalEngineResultType
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }
    public bool FlipSign { get; set; }
    public List<double> ThresholdsTested { get; set; } = [];
    public List<double> CostBpsOptions { get; set; } = [];
    public double BestThreshold { get; set; }
    public double BestCostBps { get; set; }
    public List<SignalBacktestResultType> BacktestGrid { get; set; } = [];
    public WalkForwardResultType? WalkForward { get; set; }
    public GraduationResultType? Graduation { get; set; }
    public SignalDiagnosticsType? SignalDiagnostics { get; set; }
    public DataSufficiencyType? DataSufficiency { get; set; }
    public EffectiveSampleSizeType? EffectiveSample { get; set; }
    public List<RegimeCoverageEntryType> RegimeCoverage { get; set; } = [];
    public string ResearchLog { get; set; } = "";
    public string? Error { get; set; }
}

public class SignalBacktestResultType
{
    public double Threshold { get; set; }
    public double CostBps { get; set; }
    public List<string> Dates { get; set; } = [];
    public List<double> CumulativeReturns { get; set; } = [];
    public List<double> Positions { get; set; } = [];
    public double GrossSharpe { get; set; }
    public double NetSharpe { get; set; }
    public double MaxDrawdown { get; set; }
    public double AnnualizedTurnover { get; set; }
    public double AvgHoldingBars { get; set; }
    public double WinRate { get; set; }
    public double AvgWinLossRatio { get; set; }
    public int TotalTrades { get; set; }
    public double NetTotalReturn { get; set; }
    public double GrossTotalReturn { get; set; }
}

public class WalkForwardResultType
{
    public List<WalkForwardWindowType> Windows { get; set; } = [];
    [GraphQLName("meanOosSharpe")]
    public double MeanOosSharpe { get; set; }
    [GraphQLName("stdOosSharpe")]
    public double StdOosSharpe { get; set; }
    [GraphQLName("medianOosSharpe")]
    public double MedianOosSharpe { get; set; }
    public double PctWindowsProfitable { get; set; }
    public double PctWindowsPositiveSharpe { get; set; }
    public double WorstWindowSharpe { get; set; }
    public double BestWindowSharpe { get; set; }
    [GraphQLName("totalOosBars")]
    public int TotalOosBars { get; set; }
    [GraphQLName("combinedOosDates")]
    public List<string> CombinedOosDates { get; set; } = [];
    [GraphQLName("combinedOosCumulativeReturns")]
    public List<double> CombinedOosCumulativeReturns { get; set; } = [];
    [GraphQLName("oosSharpeTrendSlope")]
    public double OosSharpeTrendSlope { get; set; }
}

public class WalkForwardWindowType
{
    public int FoldIndex { get; set; }
    public string TrainStart { get; set; } = "";
    public string TrainEnd { get; set; } = "";
    public string TestStart { get; set; } = "";
    public string TestEnd { get; set; } = "";
    public int TrainBars { get; set; }
    public int TestBars { get; set; }
    public double Mu { get; set; }
    public double Sigma { get; set; }
    public double BestThreshold { get; set; }
    [GraphQLName("oosNetSharpe")]
    public double OosNetSharpe { get; set; }
    [GraphQLName("oosGrossSharpe")]
    public double OosGrossSharpe { get; set; }
    [GraphQLName("oosMaxDrawdown")]
    public double OosMaxDrawdown { get; set; }
    [GraphQLName("oosNetReturn")]
    public double OosNetReturn { get; set; }
    [GraphQLName("oosWinRate")]
    public double OosWinRate { get; set; }
    [GraphQLName("oosTotalTrades")]
    public int OosTotalTrades { get; set; }
    [GraphQLName("oosDates")]
    public List<string> OosDates { get; set; } = [];
    [GraphQLName("oosCumulativeReturns")]
    public List<double> OosCumulativeReturns { get; set; } = [];
}

public class GraduationResultType
{
    public List<GraduationCriterionType> Criteria { get; set; } = [];
    public bool OverallPassed { get; set; }
    public string OverallGrade { get; set; } = "F";
    public string Summary { get; set; } = "";
    public string StatusLabel { get; set; } = "Exploratory";
    public ParameterStabilityType? ParameterStability { get; set; }
}

public class GraduationCriterionType
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public bool Passed { get; set; }
    public double Value { get; set; }
    public double Threshold { get; set; }
    public string Label { get; set; } = "Fail";
    public string FailureReason { get; set; } = "";
}

public class ParameterStabilityType
{
    public List<ThresholdSharpeEntryType> SharpeValuesByThreshold { get; set; } = [];
    public double StabilityScore { get; set; }
    public string StabilityLabel { get; set; } = "Fragile";
}

public class ThresholdSharpeEntryType
{
    public double Threshold { get; set; }
    public double Sharpe { get; set; }
}

public class SignalDiagnosticsType
{
    public double SignalMean { get; set; }
    public double SignalStd { get; set; }
    public double PctTimeActive { get; set; }
    public double AvgAbsSignal { get; set; }
    public double PctFilteredByThreshold { get; set; }
    public double PctGatedByRegime { get; set; }
}

public class DataSufficiencyType
{
    public int TotalBars { get; set; }
    public int TrainBars { get; set; }
    public int TestBars { get; set; }
    public int WalkForwardFolds { get; set; }
    [GraphQLName("effectiveOosBars")]
    public int EffectiveOosBars { get; set; }
    public int RegimesCovered { get; set; }
    public List<RegimeCoverageEntryType> RegimeCoverage { get; set; } = [];
    public List<string> CoverageWarnings { get; set; } = [];
}

public class EffectiveSampleSizeType
{
    public int RawN { get; set; }
    public double EffectiveN { get; set; }
    public double AutocorrelationLag1 { get; set; }
    public int IndependentBets { get; set; }
}

public class RegimeCoverageEntryType
{
    public string Regime { get; set; } = "";
    public int Count { get; set; }
}
