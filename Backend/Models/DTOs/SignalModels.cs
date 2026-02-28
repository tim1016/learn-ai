namespace Backend.Models.DTOs;

#region Signal Engine Request DTOs

public class RunSignalEngineRequest
{
    public required string Ticker { get; set; }
    public string FeatureName { get; set; } = "momentum_5m";
    public required List<OhlcvBarDto> Bars { get; set; }
    public required string StartDate { get; set; }
    public required string EndDate { get; set; }
    public bool FlipSign { get; set; } = true;
    public bool RegimeGateEnabled { get; set; } = true;
}

#endregion

#region Signal Experiment DTOs

public class SignalExperimentDto
{
    public int Id { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }
    public string OverallGrade { get; set; } = "F";
    public string StatusLabel { get; set; } = "Exploratory";
    public bool OverallPassed { get; set; }
    public double MeanOosSharpe { get; set; }
    public double BestThreshold { get; set; }
    public double BestCostBps { get; set; }
    public bool FlipSign { get; set; }
    public bool RegimeGateEnabled { get; set; }
    public DateTime CreatedAt { get; set; }
}

#endregion

#region Signal Engine Response DTOs

public class SignalEngineReportDto
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string FeatureName { get; set; } = "";
    public string StartDate { get; set; } = "";
    public string EndDate { get; set; } = "";
    public int BarsUsed { get; set; }
    public bool FlipSign { get; set; } = true;
    public List<double> ThresholdsTested { get; set; } = [];
    public List<double> CostBpsOptions { get; set; } = [];
    public double BestThreshold { get; set; }
    public double BestCostBps { get; set; }
    public List<BacktestResultDto> BacktestGrid { get; set; } = [];
    public WalkForwardResultDto? WalkForward { get; set; }
    public GraduationResultDto? Graduation { get; set; }
    public SignalDiagnosticsDto? SignalDiagnostics { get; set; }
    public DataSufficiencyDto? DataSufficiency { get; set; }
    public EffectiveSampleSizeDto? EffectiveSample { get; set; }
    public Dictionary<string, int> RegimeCoverage { get; set; } = new();
    public SignalBehaviorMetricsDto? SignalBehavior { get; set; }
    public MethodologyDto? Methodology { get; set; }
    public string ResearchLog { get; set; } = "";
    public string? Error { get; set; }
}

public class BacktestResultDto
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

public class WalkForwardWindowDto
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
    public double OosNetSharpe { get; set; }
    public double OosGrossSharpe { get; set; }
    public double OosMaxDrawdown { get; set; }
    public double OosNetReturn { get; set; }
    public double OosWinRate { get; set; }
    public int OosTotalTrades { get; set; }
    public List<string> OosDates { get; set; } = [];
    public List<double> OosCumulativeReturns { get; set; } = [];
}

public class WalkForwardResultDto
{
    public List<WalkForwardWindowDto> Windows { get; set; } = [];
    public double MeanOosSharpe { get; set; }
    public double StdOosSharpe { get; set; }
    public double MedianOosSharpe { get; set; }
    public double PctWindowsProfitable { get; set; }
    public double PctWindowsPositiveSharpe { get; set; }
    public double WorstWindowSharpe { get; set; }
    public double BestWindowSharpe { get; set; }
    public int TotalOosBars { get; set; }
    public List<string> CombinedOosDates { get; set; } = [];
    public List<double> CombinedOosCumulativeReturns { get; set; } = [];
    public double OosSharpeTrendSlope { get; set; }
    public AlphaDecayStatsDto? AlphaDecay { get; set; }
}

public class GraduationCriterionDto
{
    public string Name { get; set; } = "";
    public string Description { get; set; } = "";
    public bool Passed { get; set; }
    public double Value { get; set; }
    public double Threshold { get; set; }
    public string Label { get; set; } = "Fail";
    public string FailureReason { get; set; } = "";
}

public class ParameterStabilityDto
{
    public Dictionary<double, double> SharpeValuesByThreshold { get; set; } = new();
    public double StabilityScore { get; set; }
    public string StabilityLabel { get; set; } = "Fragile";
}

public class GraduationResultDto
{
    public List<GraduationCriterionDto> Criteria { get; set; } = [];
    public bool OverallPassed { get; set; }
    public string OverallGrade { get; set; } = "F";
    public string Summary { get; set; } = "";
    public string StatusLabel { get; set; } = "Exploratory";
    public ParameterStabilityDto? ParameterStability { get; set; }
}

public class SignalDiagnosticsDto
{
    public double SignalMean { get; set; }
    public double SignalStd { get; set; }
    public double PctTimeActive { get; set; }
    public double AvgAbsSignal { get; set; }
    public double PctFilteredByThreshold { get; set; }
    public double PctGatedByRegime { get; set; }
}

public class DataSufficiencyDto
{
    public int TotalBars { get; set; }
    public int TrainBars { get; set; }
    public int TestBars { get; set; }
    public int WalkForwardFolds { get; set; }
    public int EffectiveOosBars { get; set; }
    public int RegimesCovered { get; set; }
    public Dictionary<string, int> RegimeCoverage { get; set; } = new();
    public List<string> CoverageWarnings { get; set; } = [];
}

public class EffectiveSampleSizeDto
{
    public int RawN { get; set; }
    public double EffectiveN { get; set; }
    public double AutocorrelationLag1 { get; set; }
    public int IndependentBets { get; set; }
    public int MaxLagUsed { get; set; }
    public double RhoSum { get; set; }
}

public class AlphaDecayStatsDto
{
    public double Slope { get; set; }
    public double Intercept { get; set; }
    public double TStat { get; set; }
    public double PValue { get; set; }
    public double RSquared { get; set; }
}

public class SignalBehaviorMetricsDto
{
    public double AvgForwardReturnWhenActive { get; set; }
    public double SkewnessActiveReturns { get; set; }
    public double AvgWinReturn { get; set; }
    public double AvgLossReturn { get; set; }
    public double HitRate { get; set; }
}

public class MethodologyDto
{
    public int TrainMonths { get; set; }
    public int TestMonths { get; set; }
    public string WindowType { get; set; } = "rolling";
    public string OptimizationTarget { get; set; } = "net_sharpe";
    public int AnnualizationFactor { get; set; }
    public int BarsPerDay { get; set; }
    public int Horizon { get; set; }
    public double DefaultCostBps { get; set; }
    public int MinBarsForSignal { get; set; }
    public bool FlipSign { get; set; }
    public bool RegimeGateEnabled { get; set; }
    public List<double> Thresholds { get; set; } = [];
    public List<double> CostBpsOptions { get; set; } = [];
}

#endregion
