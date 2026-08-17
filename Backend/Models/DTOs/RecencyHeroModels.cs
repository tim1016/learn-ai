namespace Backend.Models.DTOs;

public sealed record RecencyHeroTradeRequest(long EntryMs, decimal Pnl);

public sealed record RecencyHeroCandidateRequest(
    int RecencyRunId,
    string Symbol,
    string StrategyKey,
    string ParamsHash,
    IReadOnlyList<RecencyHeroTradeRequest> Trades);

public sealed record RecencyHeroRequest(
    long FromMs,
    long ToMs,
    IReadOnlyList<RecencyHeroCandidateRequest> Candidates);

public sealed record RecencyHeroResponse(IReadOnlyList<RecencyHeroResponseItem> Heroes);

public sealed record RecencyHeroResponseItem(
    int RecencyRunId,
    string Symbol,
    string StrategyKey,
    string ParamsHash,
    decimal TotalPnl);
