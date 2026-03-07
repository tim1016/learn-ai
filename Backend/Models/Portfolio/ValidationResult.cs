namespace Backend.Models.Portfolio;

public class ValidationSuiteResult
{
    public string AccountId { get; set; } = null!;
    public DateTime StartedAt { get; set; }
    public DateTime CompletedAt { get; set; }
    public double DurationMs { get; set; }
    public int TotalTests { get; set; }
    public int Passed { get; set; }
    public int Failed { get; set; }
    public List<ValidationTestResult> Tests { get; set; } = [];
}

public class ValidationTestResult
{
    public int TestNumber { get; set; }
    public string Name { get; set; } = null!;
    public string Category { get; set; } = null!;
    public string Objective { get; set; } = null!;
    public bool Passed { get; set; }
    public double DurationMs { get; set; }
    public string? Error { get; set; }
    public List<ValidationAssertion> Assertions { get; set; } = [];
}

public class ValidationAssertion
{
    public string Label { get; set; } = null!;
    public string Expected { get; set; } = null!;
    public string Actual { get; set; } = null!;
    public bool Passed { get; set; }
    public decimal? Tolerance { get; set; }
}
