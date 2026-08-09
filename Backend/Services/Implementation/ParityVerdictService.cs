using System.Text.Json;
using Backend.Data;
using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

/// <summary>
/// Freezes the cross-engine ParityVerdict for a parity group when the
/// LEAN companion run lands. Trade reconciliation is delegated to the
/// Python compare endpoint (via <see cref="IComparisonService"/>). The
/// service also verifies the Python-authored readiness envelopes and the
/// persisted LEAN-native metric-reproduction receipt; it never recomputes
/// either numerical namespace.
///
/// State machine: <c>pending → agree | diverged | unavailable</c> here;
/// <c>pending → run_failed | persist_failed</c> via the mark-failed
/// endpoint. All transitions are conditional on <c>pending</c> — the
/// first terminal state wins and is never overwritten.
/// </summary>
public class ParityVerdictService : IParityVerdictService
{
    public const int VerdictVersion = 2;
    private const string FillPriceAtol = "0.01";
    private static readonly JsonSerializerOptions VerdictJsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };
    private static readonly string[] ReadinessFields =
    [
        "verdict_version",
        "status",
        "composite",
        "grade",
        "signal",
        "red_flags",
        "dimensions",
        "missing_metrics",
        "missing_required_metrics",
        "available_required_metrics",
        "required_metrics",
        "normalized_weights",
    ];
    private static readonly string[] DataPolicyFields =
    [
        "source",
        "symbol",
        "adjusted",
        "session",
        "input_bars",
        "strategy_bars",
        "timestamp_policy",
        "timezone",
        "provider_kind",
        "fixture_id",
        "fixture_sha256",
    ];

    private readonly AppDbContext _db;
    private readonly IComparisonService _comparison;
    private readonly ILogger<ParityVerdictService> _logger;

    public ParityVerdictService(
        AppDbContext db,
        IComparisonService comparison,
        ILogger<ParityVerdictService> logger)
    {
        _db = db;
        _comparison = comparison;
        _logger = logger;
    }

    public async Task ComputeForLeanRunAsync(int rightExecutionId, string parityGroupId, CancellationToken ct)
    {
        var right = await _db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Trades)
            .FirstOrDefaultAsync(e => e.Id == rightExecutionId, ct);
        var left = await _db.StrategyExecutions
            .AsNoTracking()
            .Include(e => e.Trades)
            .Where(e => e.ParityGroupId == parityGroupId && e.Source == "engine")
            .OrderBy(e => e.Id)
            .FirstOrDefaultAsync(ct);

        if (right is null || left is null)
        {
            _logger.LogWarning(
                "[PARITY] Cannot compute verdict for group {Group}: left={LeftFound} right={RightFound}",
                parityGroupId, left is not null, right is not null);
            return;
        }

        var comparison = await _comparison.CompareTradesAsync(
            new CompareTradesRequest(
                LeftTrades: ToComparePayload(left.Trades),
                RightTrades: ToComparePayload(right.Trades)),
            ct);

        var nativeMetricParity = ReadNativeMetricParity(right.LeanStatisticsJson);
        var readinessParity = CompareReadiness(left.RunVerdictJson, right.RunVerdictJson);
        var inputParity = CompareInputs(left, right);
        var status = ResolveStatus(
            comparison.Divergences.Count,
            nativeMetricParity.Status,
            readinessParity.Status,
            inputParity.Status);
        var reason = ResolveReason(
            status,
            nativeMetricParity.Status,
            readinessParity.Status,
            inputParity.Status);
        var verdictJson = JsonSerializer.Serialize(new
        {
            schema_version = 2,
            parity_group_id = parityGroupId,
            left_execution_id = left.Id,
            right_execution_id = right.Id,
            engines = new { left = "python", right = "lean" },
            status,
            reason,
            tolerances = new { fill_price_atol = FillPriceAtol },
            native_metric_parity = nativeMetricParity,
            readiness_parity = readinessParity,
            input_parity = inputParity,
            divergences = comparison.Divergences.Select(d => new
            {
                category = d.Category,
                trade_number = d.TradeNumber,
                ms_utc = d.MsUtc,
                message = d.Message,
            }),
            counts_by_category = comparison.Divergences
                .GroupBy(d => d.Category)
                .ToDictionary(g => g.Key, g => g.Count()),
            computed_at_ms = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        }, VerdictJsonOptions);

        var row = await _db.ParityVerdicts.FirstOrDefaultAsync(v => v.ParityGroupId == parityGroupId, ct);
        if (row is null)
        {
            // The pending row from run time was lost (best-effort create) —
            // recover by inserting the terminal verdict directly.
            _db.ParityVerdicts.Add(new ParityVerdict
            {
                LeftExecutionId = left.Id,
                RightExecutionId = right.Id,
                ParityGroupId = parityGroupId,
                VerdictVersion = VerdictVersion,
                Status = status,
                VerdictJson = verdictJson,
                // CreatedAtUtc is a DateTime column; use UtcDateTime from DateTimeOffset
                // to guarantee Kind=Utc (avoids Local-kind ambiguity, temporal-rigor.md).
                CreatedAtUtc = DateTimeOffset.UtcNow.UtcDateTime,
            });
        }
        else if (row.Status == "pending")
        {
            row.RightExecutionId = right.Id;
            row.VerdictVersion = VerdictVersion;
            row.Status = status;
            row.VerdictJson = verdictJson;
        }
        else
        {
            _logger.LogInformation(
                "[PARITY] Verdict for group {Group} already terminal ({Status}); not overwriting",
                parityGroupId, row.Status);
            return;
        }

        await _db.SaveChangesAsync(ct);
        _logger.LogInformation(
            "[PARITY] Frozen verdict for group {Group}: {Status} ({Count} trade divergences, native={NativeStatus}, readiness={ReadinessStatus}, input={InputStatus})",
            parityGroupId,
            status,
            comparison.Divergences.Count,
            nativeMetricParity.Status,
            readinessParity.Status,
            inputParity.Status);
    }

    private static string ResolveStatus(
        int tradeDivergenceCount,
        string nativeStatus,
        string readinessStatus,
        string inputStatus)
    {
        if (tradeDivergenceCount > 0 || nativeStatus == "mismatch" ||
            readinessStatus == "mismatch" || inputStatus == "mismatch")
            return "diverged";
        if (nativeStatus != "match" || readinessStatus != "match" || inputStatus != "match")
            return "unavailable";
        return "agree";
    }

    private static string? ResolveReason(
        string status,
        string nativeStatus,
        string readinessStatus,
        string inputStatus)
    {
        if (status == "agree")
            return null;
        if (nativeStatus == "mismatch")
            return "lean_native_metric_mismatch";
        if (readinessStatus == "mismatch")
            return "production_readiness_mismatch";
        if (inputStatus == "mismatch")
            return "compatibility_input_mismatch";
        if (nativeStatus != "match")
            return "lean_native_metric_parity_unavailable";
        if (readinessStatus != "match")
            return "production_readiness_parity_unavailable";
        if (inputStatus != "match")
            return "compatibility_input_parity_unavailable";
        return "trade_reconciliation_diverged";
    }

    private static NativeMetricParityReceipt ReadNativeMetricParity(string? leanStatisticsJson)
    {
        if (string.IsNullOrWhiteSpace(leanStatisticsJson))
            return NativeMetricParityReceipt.Unavailable("lean_statistics_missing");

        try
        {
            using var document = JsonDocument.Parse(leanStatisticsJson);
            if (!document.RootElement.TryGetProperty("namespaces", out var receipt) ||
                receipt.ValueKind != JsonValueKind.Object)
            {
                return NativeMetricParityReceipt.Unavailable("lean_native_metric_receipt_missing");
            }

            var status = ReadString(receipt, "status") ?? "unavailable";
            var divergences = receipt.TryGetProperty("divergences", out var divergenceElement) &&
                divergenceElement.ValueKind == JsonValueKind.Array
                    ? divergenceElement.GetArrayLength()
                    : 0;
            return new NativeMetricParityReceipt(
                Status: status,
                Reason: ReadString(receipt, "reason"),
                ContractId: ReadString(receipt, "contract_id"),
                SourceCommit: ReadString(receipt, "source_commit"),
                AbsoluteTolerance: ReadNumber(receipt, "absolute_tolerance"),
                NativeMetricCount: ReadInteger(receipt, "native_metric_count"),
                FormattedMetricCount: ReadInteger(receipt, "formatted_metric_count"),
                DivergenceCount: divergences);
        }
        catch (JsonException)
        {
            return NativeMetricParityReceipt.Unavailable("lean_statistics_unreadable");
        }
    }

    private static ReadinessParityReceipt CompareReadiness(string? leftJson, string? rightJson)
    {
        if (string.IsNullOrWhiteSpace(leftJson) || string.IsNullOrWhiteSpace(rightJson))
            return ReadinessParityReceipt.Unavailable("run_verdict_missing");

        try
        {
            using var left = JsonDocument.Parse(leftJson);
            using var right = JsonDocument.Parse(rightJson);
            var hasLeftSignature = left.RootElement.TryGetProperty("parity_signature", out var leftSignature) &&
                leftSignature.ValueKind == JsonValueKind.Object;
            var hasRightSignature = right.RootElement.TryGetProperty("parity_signature", out var rightSignature) &&
                rightSignature.ValueKind == JsonValueKind.Object;
            if (hasLeftSignature || hasRightSignature)
            {
                var matches = hasLeftSignature && hasRightSignature &&
                    JsonElement.DeepEquals(leftSignature, rightSignature);
                var requiredMetricCount = hasRightSignature
                    ? ReadInteger(rightSignature, "required_metrics")
                    : hasLeftSignature ? ReadInteger(leftSignature, "required_metrics") : 0;
                return new ReadinessParityReceipt(
                    Status: matches ? "match" : "mismatch",
                    Reason: matches ? null : "readiness_signature_differs",
                    ComparedFieldCount: requiredMetricCount,
                    MismatchedFields: matches ? [] : ["parity_signature"]);
            }

            // Legacy verdicts predate the Python-authored, tolerance-pinned
            // receipt. Preserve their comparison path for historical rows.
            var mismatches = ReadinessFields
                .Where(field => !PropertiesEqual(left.RootElement, right.RootElement, field))
                .ToArray();
            return new ReadinessParityReceipt(
                Status: mismatches.Length == 0 ? "match" : "mismatch",
                Reason: mismatches.Length == 0 ? null : "readiness_fields_differ",
                ComparedFieldCount: ReadinessFields.Length,
                MismatchedFields: mismatches);
        }
        catch (JsonException)
        {
            return ReadinessParityReceipt.Unavailable("run_verdict_unreadable");
        }
    }

    private static InputParityReceipt CompareInputs(StrategyExecution left, StrategyExecution right)
    {
        if (string.IsNullOrWhiteSpace(left.DataPolicyJson) || string.IsNullOrWhiteSpace(right.DataPolicyJson))
            return InputParityReceipt.Unavailable("data_policy_missing");

        try
        {
            using var leftPolicy = JsonDocument.Parse(left.DataPolicyJson);
            using var rightPolicy = JsonDocument.Parse(right.DataPolicyJson);
            var mismatches = DataPolicyFields
                .Where(field => !PropertiesEqual(leftPolicy.RootElement, rightPolicy.RootElement, field))
                .ToList();
            if (left.InitialCash != right.InitialCash)
                mismatches.Add("initial_cash");
            if (left.StartDate != right.StartDate)
                mismatches.Add("start_date");
            if (left.EndDate != right.EndDate)
                mismatches.Add("end_date");
            if (left.FillMode != right.FillMode)
                mismatches.Add("fill_mode");
            return new InputParityReceipt(
                Status: mismatches.Count == 0 ? "match" : "mismatch",
                Reason: mismatches.Count == 0 ? null : "compatibility_inputs_differ",
                ComparedFieldCount: DataPolicyFields.Length + 4,
                FixtureId: ReadString(rightPolicy.RootElement, "fixture_id"),
                FixtureSha256: ReadString(rightPolicy.RootElement, "fixture_sha256"),
                MismatchedFields: mismatches);
        }
        catch (JsonException)
        {
            return InputParityReceipt.Unavailable("data_policy_unreadable");
        }
    }

    private static bool PropertiesEqual(JsonElement left, JsonElement right, string propertyName)
    {
        var hasLeft = left.TryGetProperty(propertyName, out var leftValue);
        var hasRight = right.TryGetProperty(propertyName, out var rightValue);
        return hasLeft == hasRight && (!hasLeft || JsonElement.DeepEquals(leftValue, rightValue));
    }

    private static string? ReadString(JsonElement parent, string name) =>
        parent.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static decimal? ReadNumber(JsonElement parent, string name) =>
        parent.TryGetProperty(name, out var value) && value.TryGetDecimal(out var number)
            ? number
            : null;

    private static int ReadInteger(JsonElement parent, string name) =>
        parent.TryGetProperty(name, out var value) && value.TryGetInt32(out var number)
            ? number
            : 0;

    private sealed record NativeMetricParityReceipt(
        string Status,
        string? Reason,
        string? ContractId,
        string? SourceCommit,
        decimal? AbsoluteTolerance,
        int NativeMetricCount,
        int FormattedMetricCount,
        int DivergenceCount)
    {
        public static NativeMetricParityReceipt Unavailable(string reason) =>
            new("unavailable", reason, null, null, null, 0, 0, 0);
    }

    private sealed record ReadinessParityReceipt(
        string Status,
        string? Reason,
        int ComparedFieldCount,
        IReadOnlyList<string> MismatchedFields)
    {
        public static ReadinessParityReceipt Unavailable(string reason) =>
            new("unavailable", reason, 0, []);
    }

    private sealed record InputParityReceipt(
        string Status,
        string? Reason,
        int ComparedFieldCount,
        string? FixtureId,
        string? FixtureSha256,
        IReadOnlyList<string> MismatchedFields)
    {
        public static InputParityReceipt Unavailable(string reason) =>
            new("unavailable", reason, 0, null, null, []);
    }

    private static List<PersistLeanTradePayload> ToComparePayload(IEnumerable<BacktestTrade> trades)
    {
        return trades
            .OrderBy(t => t.EntryTimestamp)
            .Select((t, index) => new PersistLeanTradePayload(
                TradeNumber: index + 1,
                EntryMsUtc: new DateTimeOffset(t.EntryTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                ExitMsUtc: new DateTimeOffset(t.ExitTimestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                EntryPrice: t.EntryPrice,
                ExitPrice: t.ExitPrice,
                Quantity: t.Quantity,
                Pnl: t.PnL,
                SignalReason: t.SignalReason,
                IsSyntheticExit: t.IsSyntheticExit))
            .ToList();
    }
}
