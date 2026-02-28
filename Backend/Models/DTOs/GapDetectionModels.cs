namespace Backend.Models.DTOs;

public class GapDetectionResult
{
    public int TotalWeekdays { get; set; }
    public int DaysWithData { get; set; }
    public int MissingDays { get; set; }
    public int PartialDays { get; set; }
    public decimal CoveragePercent { get; set; }
    public int ExpectedBars { get; set; }
    public int ActualBars { get; set; }
    public List<string> MissingDates { get; set; } = [];
    public List<string> PartialDates { get; set; } = [];
    public List<WindowFetchStatus>? WindowStatuses { get; set; }
}

public class WindowFetchStatus
{
    public string FromDate { get; set; } = "";
    public string ToDate { get; set; } = "";
    public bool Success { get; set; }
    public int BarsFetched { get; set; }
    public string? Error { get; set; }
}
