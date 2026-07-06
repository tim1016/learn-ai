using Backend.GraphQL;

namespace Backend.GraphQL.Comparison;

public record RunComparisonResult(
    BacktestRunNodeType Left,
    BacktestRunNodeType Right,
    ComparisonGuardrails Guardrails,
    ComparisonSummary Summary,
    IReadOnlyList<TradeDivergence> Divergences,
    long? FirstDivergenceMsUtc);

public record ComparisonGuardrails(
    bool SameAlgorithm,
    bool SameSymbol,
    bool SameWindow,
    bool SameParameters,
    IReadOnlyList<string> Warnings);

public record ComparisonSummary(
    decimal PnlDelta,
    int TradeCountDelta,
    double WinRateDelta,
    decimal FeesDelta,
    decimal FinalEquityDelta);

public record TradeDivergence(
    DivergenceCategory Category,
    int? TradeNumber,
    long? MsUtc,
    string Message,
    decimal? LeftFillPrice,
    decimal? RightFillPrice,
    decimal? LeftQuantity,
    decimal? RightQuantity);

public enum DivergenceCategory
{
    DECISION_MISMATCH,
    DIRECTION_MISMATCH,
    QUANTITY_MISMATCH,
    FILL_PRICE_DRIFT,
    COMMISSION_DRIFT,
    PNL_DRIFT,
    ORDER_TYPE_MISMATCH,
    FIXTURE_INSUFFICIENT,
}
