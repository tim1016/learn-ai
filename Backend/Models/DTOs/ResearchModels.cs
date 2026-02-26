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
    public string? Error { get; set; }
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
