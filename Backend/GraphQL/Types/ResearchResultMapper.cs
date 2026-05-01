using Backend.Models.DTOs;

namespace Backend.GraphQL.Types;

public static class ResearchResultMapper
{
    public static ResearchResultType ToGraphQL(ResearchReportDto report)
    {
        return new ResearchResultType
        {
            Success = report.Success,
            Ticker = report.Ticker,
            FeatureName = report.FeatureName,
            StartDate = report.StartDate,
            EndDate = report.EndDate,
            BarsUsed = report.BarsUsed,
            MeanIC = report.MeanIc,
            ICTStat = report.IcTStat,
            ICPValue = report.IcPValue,
            NwTStat = report.NwTStat,
            NwPValue = report.NwPValue,
            EffectiveN = report.EffectiveN,
            ICValues = report.IcValues,
            ICDates = report.IcDates,
            AdfPvalue = report.AdfPvalue,
            KpssPvalue = report.KpssPvalue,
            IsStationary = report.IsStationary,
            QuantileBins = report.QuantileBins.Select(b => new QuantileBinType
            {
                BinNumber = b.BinNumber,
                LowerBound = b.LowerBound,
                UpperBound = b.UpperBound,
                MeanReturn = b.MeanReturn,
                Count = b.Count,
            }).ToList(),
            IsMonotonic = report.IsMonotonic,
            MonotonicityRatio = report.MonotonicityRatio,
            PassedValidation = report.PassedValidation,
            Robustness = MapRobustness(report.Robustness),
            FeatureSpec = MapFeatureSpec(report.FeatureSpec),
            TargetMetadata = MapTargetMetadata(report.TargetMetadata),
            ValidationVerdict = MapValidationVerdict(report.ValidationVerdict),
            Error = report.Error,
        };
    }

    private static TargetMetadataType? MapTargetMetadata(TargetMetadataDto? dto)
    {
        if (dto is null) return null;
        return new TargetMetadataType
        {
            TargetName = dto.TargetName,
            HorizonMinutes = dto.HorizonMinutes,
            HorizonBars = dto.HorizonBars,
            BarMinutes = dto.BarMinutes,
            Timezone = dto.Timezone,
            ValidCount = dto.ValidCount,
            TotalCount = dto.TotalCount,
            ValidRatio = dto.ValidRatio,
            InvalidReasonCounts = new Dictionary<string, int>(dto.InvalidReasonCounts),
        };
    }

    private static RobustnessType? MapRobustness(RobustnessDto? dto)
    {
        if (dto is null) return null;
        return new RobustnessType
        {
            MonthlyBreakdown = dto.MonthlyBreakdown.Select(m => new MonthlyICBreakdownType
            {
                Month = m.Month,
                MeanIC = m.MeanIc,
                TStat = m.TStat,
                ObservationCount = m.ObservationCount,
            }).ToList(),
            PctPositiveMonths = dto.PctPositiveMonths,
            PctSignificantMonths = dto.PctSignificantMonths,
            BestMonthIC = dto.BestMonthIc,
            WorstMonthIC = dto.WorstMonthIc,
            StabilityLabel = dto.StabilityLabel,
            PctSignConsistentMonths = dto.PctSignConsistentMonths,
            SignConsistentStabilityLabel = dto.SignConsistentStabilityLabel,
            RollingTStat = dto.RollingTStat.Select(r => new RollingTStatPointType
            {
                Month = r.Month,
                TStatSmoothed = r.TStatSmoothed,
            }).ToList(),
            VolatilityRegimes = dto.VolatilityRegimes.Select(r => new RegimeICType
            {
                RegimeLabel = r.RegimeLabel,
                MeanIC = r.MeanIc,
                TStat = r.TStat,
                ObservationCount = r.ObservationCount,
            }).ToList(),
            TrendRegimes = dto.TrendRegimes.Select(r => new RegimeICType
            {
                RegimeLabel = r.RegimeLabel,
                MeanIC = r.MeanIc,
                TStat = r.TStat,
                ObservationCount = r.ObservationCount,
            }).ToList(),
            TrainTest = dto.TrainTest is null ? null : new TrainTestSplitType
            {
                TrainStart = dto.TrainTest.TrainStart,
                TrainEnd = dto.TrainTest.TrainEnd,
                TestStart = dto.TrainTest.TestStart,
                TestEnd = dto.TrainTest.TestEnd,
                TrainMeanIC = dto.TrainTest.TrainMeanIc,
                TrainTStat = dto.TrainTest.TrainTStat,
                TrainDays = dto.TrainTest.TrainDays,
                TestMeanIC = dto.TrainTest.TestMeanIc,
                TestTStat = dto.TrainTest.TestTStat,
                TestDays = dto.TrainTest.TestDays,
                OverfitFlag = dto.TrainTest.OverfitFlag,
                OosRetention = dto.TrainTest.OosRetention,
                OosRetentionLabel = dto.TrainTest.OosRetentionLabel,
            },
            StructuralBreaks = dto.StructuralBreaks.Select(b => new StructuralBreakPointType
            {
                Date = b.Date,
                IcBefore = b.IcBefore,
                IcAfter = b.IcAfter,
                TStat = b.TStat,
                Significant = b.Significant,
            }).ToList(),
        };
    }

    private static FeatureValidationSpecType? MapFeatureSpec(FeatureValidationSpecDto? dto)
    {
        if (dto is null) return null;
        return new FeatureValidationSpecType
        {
            FeatureName = dto.FeatureName,
            DefaultTarget = dto.DefaultTarget,
            ExpectedDirection = dto.ExpectedDirection,
            ExpectedShape = dto.ExpectedShape,
            StationarityRequired = dto.StationarityRequired,
            MonotonicityRequired = dto.MonotonicityRequired,
            IsSignedTargetAppropriate = dto.IsSignedTargetAppropriate,
            Intent = dto.Intent,
            Notes = [.. dto.Notes],
        };
    }

    private static FeatureValidationVerdictType? MapValidationVerdict(FeatureValidationVerdictDto? dto)
    {
        if (dto is null) return null;
        return new FeatureValidationVerdictType
        {
            StatisticalScreen = MapScreen(dto.StatisticalScreen),
            EconomicScreen = MapScreen(dto.EconomicScreen),
            OosScreen = MapScreen(dto.OosScreen),
            MultipleTestingScreen = MapScreen(dto.MultipleTestingScreen),
            RegimeStabilityScreen = MapScreen(dto.RegimeStabilityScreen),
            MultipleTesting = new MultipleTestingWarningType
            {
                RawNwPValue = dto.MultipleTesting.RawNwPValue,
                HolmPValue = dto.MultipleTesting.HolmPValue,
                NFamily = dto.MultipleTesting.NFamily,
                Note = dto.MultipleTesting.Note,
            },
            CostViability = new CostViabilityType
            {
                GrossSpreadBpsSigned = dto.CostViability.GrossSpreadBpsSigned,
                DirectionalSpreadBps = dto.CostViability.DirectionalSpreadBps,
                CostAssumptionOneWayBps = dto.CostViability.CostAssumptionOneWayBps,
                CostErasureOneWayBps = dto.CostViability.CostErasureOneWayBps,
                NetSpreadBpsAtAssumption = dto.CostViability.NetSpreadBpsAtAssumption,
                ViableAtAssumption = dto.CostViability.ViableAtAssumption,
                SpecDirection = dto.CostViability.SpecDirection,
                Note = dto.CostViability.Note,
            },
            DirectionMatchesSpec = dto.DirectionMatchesSpec,
            TargetSignedAppropriate = dto.TargetSignedAppropriate,
            IcCi = new IcCiType
            {
                Point = dto.IcCi.Point,
                Se = dto.IcCi.Se,
                CiLower = dto.IcCi.CiLower,
                CiUpper = dto.IcCi.CiUpper,
                ConfidenceLevel = dto.IcCi.ConfidenceLevel,
                NEffUsed = dto.IcCi.NEffUsed,
                Valid = dto.IcCi.Valid,
                SeApproximationNote = dto.IcCi.SeApproximationNote,
            },
            StageInfo = new FeatureStageInfoType
            {
                Stage = dto.StageInfo.Stage,
                Label = dto.StageInfo.Label,
                Description = dto.StageInfo.Description,
                NextStageLabel = dto.StageInfo.NextStageLabel,
                AdvanceCriteria = dto.StageInfo.AdvanceCriteria.Select(c => new FeatureStageCriterionType
                {
                    Name = c.Name,
                    Description = c.Description,
                    CurrentValue = c.CurrentValue,
                    RequiredRepr = c.RequiredRepr,
                    Met = c.Met,
                }).ToList(),
                FailedScreens = [.. dto.StageInfo.FailedScreens],
            },
            FinalDecision = dto.FinalDecision,
        };
    }

    private static ValidationScreenType MapScreen(ValidationScreenDto dto) => new()
    {
        Name = dto.Name,
        Description = dto.Description,
        Passed = dto.Passed,
        RequiredForStage1 = dto.RequiredForStage1,
        FailureReasons = [.. dto.FailureReasons],
    };
}
