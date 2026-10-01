namespace Backend.Models.DTOs;

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
    public long CreatedAt { get; set; }
}

#endregion
