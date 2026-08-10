namespace Backend.GraphQL.Types;

/// <summary>
/// Source-aware documentation context for a value displayed by a persisted
/// run. This is transport metadata only; it never chooses or computes a
/// metric value in the Backend.
/// </summary>
public sealed record MetricDocumentationContextType
{
    public string MetricId { get; init; } = "";
    public string VariantId { get; init; } = "";
    public string Producer { get; init; } = "";
    public string? ContractId { get; init; }
    public string ContractProvenance { get; init; } = "";

    public bool IsValid() =>
        !string.IsNullOrWhiteSpace(MetricId) &&
        !string.IsNullOrWhiteSpace(VariantId) &&
        Producer is "platform" or "lean_native" or "lean_runtime" or "validation_analytics" or "verdict_policy" &&
        ContractProvenance is "" or "recorded" or "inferred";
}
