namespace Backend.Models.DTOs;

/// <summary>
/// A single market data record for sanitization (Massive.com/Polygon.io schema).
/// Timestamps are Unix milliseconds for lossless C# ↔ Python serialization.
/// </summary>
public record MarketDataRecord(
    string Symbol,
    decimal Open,
    decimal High,
    decimal Low,
    decimal Close,
    decimal Volume,
    long Timestamp
);

/// <summary>
/// Request DTO sent to the Python /api/sanitize endpoint.
/// </summary>
public record SanitizeRequestDto(
    List<MarketDataRecord> Data,
    double Quantile = 0.99
);

/// <summary>
/// Response DTO from the Python /api/sanitize endpoint.
/// </summary>
public record SanitizeResponseDto(
    bool Success,
    List<MarketDataRecord> Data,
    SanitizeSummary? Summary,
    string? Error
);

public record SanitizeSummary(
    int OriginalCount,
    int CleanedCount,
    int RemovedCount,
    decimal? RemovalPercentage
);
