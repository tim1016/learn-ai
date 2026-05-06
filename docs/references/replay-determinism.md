# Replay determinism

**Concept**: Same input → same output invariant for the backtest engine. Running the engine twice over identical inputs (bars, parameters, strategy code, seed) produces identical outputs (trades, equity curve, statistics).

**Reference**: Internal invariant — no external paper. This is a property the engine must satisfy, not a formula it computes.

**Canonical implementation**: `PythonDataService/app/engine/` (the entire event-driven engine, by construction). Test exists in `Backend.Tests/Unit/Services/ReplayDeterminismTests.cs` (the test currently lives in .NET because Strategy Lab's deprecated runBacktest path was the original determinism surface; the test belongs in Python once Phase 3 of the migration plan completes — see `docs/architecture/numerical-authority-migration-plan.md`).

**Status note**: `canonical-supporting`. The math-correctness of the engine is captured in per-component tests (indicators, fill model, statistics); replay determinism is the cross-cutting invariant that any deterministic implementation should hold.

**Validation**: `Backend.Tests/Unit/Services/ReplayDeterminismTests.cs`. Should be ported to Python with the rest of Phase 3.

**Anti-patterns that break determinism (must not appear in the engine)**:
- `random.random()` without explicit seed
- `dict` iteration that depends on insertion order (Python 3.7+ this is stable, but be explicit)
- File-system or wall-clock dependent code paths
- Floating-point accumulation in non-deterministic order (e.g., parallel reduce without ordered combine)
