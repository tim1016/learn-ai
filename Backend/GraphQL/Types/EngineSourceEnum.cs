namespace Backend.GraphQL.Types;

public enum EngineSource
{
    ENGINE,
    STRATEGY_LAB,
    LEAN_SIDECAR,
}

public static class EngineSourceExtensions
{
    public static string ToDbValue(this EngineSource engine) => engine switch
    {
        EngineSource.ENGINE => "engine",
        EngineSource.STRATEGY_LAB => "strategy-lab",
        EngineSource.LEAN_SIDECAR => "lean-sidecar",
        _ => throw new ArgumentOutOfRangeException(nameof(engine)),
    };
}
