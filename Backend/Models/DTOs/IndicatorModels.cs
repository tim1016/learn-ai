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
