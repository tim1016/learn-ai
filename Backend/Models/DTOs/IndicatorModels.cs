namespace Backend.Models.DTOs;

public record OhlcvBarDto(
    long Timestamp,
    decimal Open,
    decimal High,
    decimal Low,
    decimal Close,
    decimal Volume
);

public record IndicatorConfigDto(
    string Name,
    int Window = 14
);

public record CalculateIndicatorsRequestDto(
    string Ticker,
    List<OhlcvBarDto> Bars,
    List<IndicatorConfigDto> Indicators
);

public record IndicatorDataPointDto(
    long Timestamp,
    decimal? Value,
    decimal? Signal,
    decimal? Histogram,
    decimal? Upper,
    decimal? Lower
);

public record IndicatorResultDto(
    string Name,
    int Window,
    List<IndicatorDataPointDto> Data
);

public record CalculateIndicatorsResponseDto(
    bool Success,
    string Ticker,
    List<IndicatorResultDto> Indicators,
    string? Error
);

// ------------------------------------------------------------------
// Indicator Table (TradingView-style full table generation)
// ------------------------------------------------------------------

public record IndicatorTableRequestDto(
    // Renamed from Ticker → Symbol in PR (ii) to align with the Python
    // TickerRequest schema base. Wire serialization uses snake_case
    // (JsonNamingPolicy.SnakeCaseLower → "symbol"), which matches the
    // canonical Python field name.
    string Symbol,
    string FromDate,
    string ToDate,
    int Multiplier = 1,
    string Timespan = "minute",
    List<int>? EmaPeriods = null,
    int BbLength = 20,
    double BbStd = 2.0,
    int SupertrendLength = 10,
    double SupertrendMultiplier = 3.0,
    int RsiLength = 14,
    int RsiMaLength = 14,
    int MacdFast = 12,
    int MacdSlow = 26,
    int MacdSignal = 9,
    int AdxLength = 14
);

public record IndicatorTableResponseDto(
    bool Success,
    string Ticker,
    int RowCount,
    List<string> Columns,
    List<Dictionary<string, object?>> Rows,
    string? Error
);

// ------------------------------------------------------------------
// Available Indicators & Dataset Generation
// ------------------------------------------------------------------

public record IndicatorInfoDto(
    string Name,
    string Category,
    string Description
);

public record AvailableIndicatorsResponseDto(
    bool Success,
    Dictionary<string, List<IndicatorInfoDto>> Categories,
    int Total,
    string? Error
);
