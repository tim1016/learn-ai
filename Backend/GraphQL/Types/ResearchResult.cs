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
    public string? Error { get; set; }
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
