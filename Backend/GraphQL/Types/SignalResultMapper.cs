using Backend.Models.DTOs;

namespace Backend.GraphQL.Types;

public static class SignalResultMapper
{
    public static SignalEngineResultType ToGraphQL(SignalEngineReportDto report)
    {
        return new SignalEngineResultType
        {
            Success = report.Success,
            Ticker = report.Ticker,
            FeatureName = report.FeatureName,
            StartDate = report.StartDate,
            EndDate = report.EndDate,
            BarsUsed = report.BarsUsed,
            FlipSign = report.FlipSign,
            ThresholdsTested = report.ThresholdsTested,
            CostBpsOptions = report.CostBpsOptions,
            BestThreshold = report.BestThreshold,
            BestCostBps = report.BestCostBps,
            BacktestGrid = report.BacktestGrid.Select(bt => new SignalBacktestResultType
            {
                Threshold = bt.Threshold,
                CostBps = bt.CostBps,
                Dates = bt.Dates,
                CumulativeReturns = bt.CumulativeReturns,
                Positions = bt.Positions,
                GrossSharpe = bt.GrossSharpe,
                NetSharpe = bt.NetSharpe,
                MaxDrawdown = bt.MaxDrawdown,
                AnnualizedTurnover = bt.AnnualizedTurnover,
                AvgHoldingBars = bt.AvgHoldingBars,
                WinRate = bt.WinRate,
                AvgWinLossRatio = bt.AvgWinLossRatio,
                TotalTrades = bt.TotalTrades,
                NetTotalReturn = bt.NetTotalReturn,
                GrossTotalReturn = bt.GrossTotalReturn,
            }).ToList(),
            WalkForward = report.WalkForward != null ? new WalkForwardResultType
            {
                Windows = report.WalkForward.Windows.Select(w => new WalkForwardWindowType
                {
                    FoldIndex = w.FoldIndex,
                    TrainStart = w.TrainStart,
                    TrainEnd = w.TrainEnd,
                    TestStart = w.TestStart,
                    TestEnd = w.TestEnd,
                    TrainBars = w.TrainBars,
                    TestBars = w.TestBars,
                    Mu = w.Mu,
                    Sigma = w.Sigma,
                    BestThreshold = w.BestThreshold,
                    OosNetSharpe = w.OosNetSharpe,
                    OosGrossSharpe = w.OosGrossSharpe,
                    OosMaxDrawdown = w.OosMaxDrawdown,
                    OosNetReturn = w.OosNetReturn,
                    OosWinRate = w.OosWinRate,
                    OosTotalTrades = w.OosTotalTrades,
                    OosDates = w.OosDates,
                    OosCumulativeReturns = w.OosCumulativeReturns,
                }).ToList(),
                MeanOosSharpe = report.WalkForward.MeanOosSharpe,
                StdOosSharpe = report.WalkForward.StdOosSharpe,
                MedianOosSharpe = report.WalkForward.MedianOosSharpe,
                PctWindowsProfitable = report.WalkForward.PctWindowsProfitable,
                PctWindowsPositiveSharpe = report.WalkForward.PctWindowsPositiveSharpe,
                WorstWindowSharpe = report.WalkForward.WorstWindowSharpe,
                BestWindowSharpe = report.WalkForward.BestWindowSharpe,
                TotalOosBars = report.WalkForward.TotalOosBars,
                CombinedOosDates = report.WalkForward.CombinedOosDates,
                CombinedOosCumulativeReturns = report.WalkForward.CombinedOosCumulativeReturns,
                OosSharpeTrendSlope = report.WalkForward.OosSharpeTrendSlope,
                AlphaDecay = report.WalkForward.AlphaDecay != null
                    ? new AlphaDecayStatsType
                    {
                        Slope = report.WalkForward.AlphaDecay.Slope,
                        Intercept = report.WalkForward.AlphaDecay.Intercept,
                        TStat = report.WalkForward.AlphaDecay.TStat,
                        PValue = report.WalkForward.AlphaDecay.PValue,
                        RSquared = report.WalkForward.AlphaDecay.RSquared,
                        NFoldsUsed = report.WalkForward.AlphaDecay.NFoldsUsed,
                        IsTestValid = report.WalkForward.AlphaDecay.IsTestValid,
                        IsSignificant = report.WalkForward.AlphaDecay.IsSignificant,
                    } : null,
            } : null,
            Graduation = report.Graduation != null ? new GraduationResultType
            {
                Criteria = report.Graduation.Criteria.Select(c => new GraduationCriterionType
                {
                    Name = c.Name,
                    Description = c.Description,
                    Passed = c.Passed,
                    Value = c.Value,
                    Threshold = c.Threshold,
                    Label = c.Label,
                    FailureReason = c.FailureReason,
                }).ToList(),
                OverallPassed = report.Graduation.OverallPassed,
                OverallGrade = report.Graduation.OverallGrade,
                Summary = report.Graduation.Summary,
                StatusLabel = report.Graduation.StatusLabel,
                ParameterStability = report.Graduation.ParameterStability != null
                    ? new ParameterStabilityType
                    {
                        SharpeValuesByThreshold = report.Graduation.ParameterStability
                            .SharpeValuesByThreshold
                            .Select(kv => new ThresholdSharpeEntryType
                            {
                                Threshold = kv.Key,
                                Sharpe = kv.Value,
                            }).ToList(),
                        StabilityScore = report.Graduation.ParameterStability.StabilityScore,
                        StabilityLabel = report.Graduation.ParameterStability.StabilityLabel,
                    } : null,
                Stage0Rejection = report.Graduation.Stage0Rejection != null
                    ? new Stage0RejectionType
                    {
                        Rejected = report.Graduation.Stage0Rejection.Rejected,
                        FailedCriteria = report.Graduation.Stage0Rejection.FailedCriteria
                            .Select(f => new Stage0FailureType
                            {
                                CriterionName = f.CriterionName,
                                Value = f.Value,
                                ThresholdRepr = f.ThresholdRepr,
                                Message = f.Message,
                            }).ToList(),
                    } : null,
                StageInfo = report.Graduation.StageInfo != null
                    ? new GraduationStageInfoType
                    {
                        Stage = report.Graduation.StageInfo.Stage,
                        Label = report.Graduation.StageInfo.Label,
                        Description = report.Graduation.StageInfo.Description,
                        NextStageLabel = report.Graduation.StageInfo.NextStageLabel,
                        AdvanceCriteria = report.Graduation.StageInfo.AdvanceCriteria
                            .Select(ac => new StageAdvanceCriterionType
                            {
                                Name = ac.Name,
                                Description = ac.Description,
                                CurrentValue = ac.CurrentValue,
                                RequiredRepr = ac.RequiredRepr,
                                Met = ac.Met,
                            }).ToList(),
                    } : null,
            } : null,
            SignalDiagnostics = report.SignalDiagnostics != null ? new SignalDiagnosticsType
            {
                SignalMean = report.SignalDiagnostics.SignalMean,
                SignalStd = report.SignalDiagnostics.SignalStd,
                PctTimeActive = report.SignalDiagnostics.PctTimeActive,
                AvgAbsSignal = report.SignalDiagnostics.AvgAbsSignal,
                PctFilteredByThreshold = report.SignalDiagnostics.PctFilteredByThreshold,
                PctGatedByRegime = report.SignalDiagnostics.PctGatedByRegime,
            } : null,
            DataSufficiency = report.DataSufficiency != null ? new DataSufficiencyType
            {
                TotalBars = report.DataSufficiency.TotalBars,
                TrainBars = report.DataSufficiency.TrainBars,
                TestBars = report.DataSufficiency.TestBars,
                WalkForwardFolds = report.DataSufficiency.WalkForwardFolds,
                EffectiveOosBars = report.DataSufficiency.EffectiveOosBars,
                RegimesCovered = report.DataSufficiency.RegimesCovered,
                RegimeCoverage = report.DataSufficiency.RegimeCoverage
                    .Select(kv => new RegimeCoverageEntryType { Regime = kv.Key, Count = kv.Value })
                    .ToList(),
                CoverageWarnings = report.DataSufficiency.CoverageWarnings,
            } : null,
            EffectiveSample = report.EffectiveSample != null ? new EffectiveSampleSizeType
            {
                RawN = report.EffectiveSample.RawN,
                EffectiveN = report.EffectiveSample.EffectiveN,
                AutocorrelationLag1 = report.EffectiveSample.AutocorrelationLag1,
                IndependentBets = report.EffectiveSample.IndependentBets,
                MaxLagUsed = report.EffectiveSample.MaxLagUsed,
                RhoSum = report.EffectiveSample.RhoSum,
            } : null,
            RegimeCoverage = report.RegimeCoverage
                .Select(kv => new RegimeCoverageEntryType { Regime = kv.Key, Count = kv.Value })
                .ToList(),
            JointRegimeCoverage = report.JointRegimeCoverage.Select(b => new RegimeBucketType
            {
                VolLabel = b.VolLabel,
                TrendLabel = b.TrendLabel,
                Days = b.Days,
                EffectiveTrades = b.EffectiveTrades,
                Badge = b.Badge,
            }).ToList(),
            SignalBehavior = report.SignalBehavior != null ? new SignalBehaviorMetricsType
            {
                AvgForwardReturnWhenActive = report.SignalBehavior.AvgForwardReturnWhenActive,
                SkewnessActiveReturns = report.SignalBehavior.SkewnessActiveReturns,
                AvgWinReturn = report.SignalBehavior.AvgWinReturn,
                AvgLossReturn = report.SignalBehavior.AvgLossReturn,
                HitRate = report.SignalBehavior.HitRate,
            } : null,
            OosSharpeCi = report.OosSharpeCi != null ? new SharpeCiType
            {
                Point = report.OosSharpeCi.Point,
                Se = report.OosSharpeCi.Se,
                CiLower = report.OosSharpeCi.CiLower,
                CiUpper = report.OosSharpeCi.CiUpper,
                ConfidenceLevel = report.OosSharpeCi.ConfidenceLevel,
                NEffUsed = report.OosSharpeCi.NEffUsed,
                Valid = report.OosSharpeCi.Valid,
            } : null,
            DeflatedSharpe = report.DeflatedSharpe != null ? new DeflatedSharpeType
            {
                RawSharpe = report.DeflatedSharpe.RawSharpe,
                ExpectedMaxUnderNull = report.DeflatedSharpe.ExpectedMaxUnderNull,
                DsrProbability = report.DeflatedSharpe.DsrProbability,
                NTrials = report.DeflatedSharpe.NTrials,
                Skewness = report.DeflatedSharpe.Skewness,
                Kurtosis = report.DeflatedSharpe.Kurtosis,
                Valid = report.DeflatedSharpe.Valid,
            } : null,
            Methodology = report.Methodology != null ? new MethodologyType
            {
                TrainMonths = report.Methodology.TrainMonths,
                TestMonths = report.Methodology.TestMonths,
                WindowType = report.Methodology.WindowType,
                OptimizationTarget = report.Methodology.OptimizationTarget,
                AnnualizationFactor = report.Methodology.AnnualizationFactor,
                BarsPerDay = report.Methodology.BarsPerDay,
                Horizon = report.Methodology.Horizon,
                DefaultCostBps = report.Methodology.DefaultCostBps,
                MinBarsForSignal = report.Methodology.MinBarsForSignal,
                FlipSign = report.Methodology.FlipSign,
                RegimeGateEnabled = report.Methodology.RegimeGateEnabled,
                Thresholds = report.Methodology.Thresholds,
                CostBpsOptions = report.Methodology.CostBpsOptions,
            } : null,
            ResearchLog = report.ResearchLog,
            Error = report.Error,
        };
    }
}
