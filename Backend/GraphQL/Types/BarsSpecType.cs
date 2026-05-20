namespace Backend.GraphQL.Types;

/// <summary>
/// PR B (2026-05-19) — Polygon-style (timespan, multiplier) pair carrying a
/// single timeframe. Mirrors the Python ``BarsSpec`` dataclass and the
/// TypeScript ``BarsSpec`` interface so the wire shape stays identical across
/// all three layers (no normalization in v1: ``BarsSpec("minute", 60)`` is
/// NOT equal to ``BarsSpec("hour", 1)``).
/// </summary>
public sealed record BarsSpecType(string Timespan, int Multiplier);
