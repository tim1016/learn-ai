using HotChocolate;

namespace Backend.GraphQL.Types;

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
    public long CreatedAt { get; set; }
}
