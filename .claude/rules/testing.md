# Testing rules

Cross-stack testing philosophy and per-stack conventions. Read before writing tests.

## Philosophy

- **Write tests with the feature, not after.** Tests are not optional work deferred to "when there's time".
- **Tests are first-class code**: same naming conventions, same review standards, same refactoring discipline.
- **Every bug fix includes a regression test** that fails before the fix and passes after. Commit the test first if possible.
- **Prefer fast isolated unit tests.** Use integration tests only for cross-boundary concerns (API, DB).
- **Test behavior, not implementation.** Assert what the user / caller observes, not internal state.

## Priority — what to test

In roughly this order of importance:

1. **Business logic and transformations** — pure functions, calculations, data mappings. This is where bugs hide and tests pay off most.
2. **Service methods** with branching or error handling.
3. **API endpoints and resolvers** — request → response contract validation.
4. **Component behavior** — user interactions, conditional rendering, form validation.
5. **Edge cases** — empty data, null inputs, boundary values, error responses.

## What NOT to test

- Framework boilerplate (Angular route configs, module declarations)
- Simple pass-through getters/setters with no logic
- Third-party library internals
- Exact CSS / styling (use visual regression tooling if needed)

## Numerical tests (specific to this repo)

Every port of mathematical logic from a reference source ships with:

1. **A golden fixture** in `PythonDataService/tests/fixtures/golden/<name>/` — deterministic input + reference output + source attribution.
2. **An equivalence test** that loads the fixture and asserts `np.allclose(our_output, reference_output, atol=..., rtol=...)` with **explicit** tolerances.
3. **Default tolerance: `atol=1e-9, rtol=0`**. Looser tolerances require a comment explaining why (e.g., "reference uses float32 internally, 1e-6 is the best achievable").
4. **Edge case tests**: empty input, single-value, NaN in input, warmup region, mid-series discontinuity if applicable.
5. **New-math-only rule, not a backfill**: every *new* mathematical function ships with a parity test on its first commit, and every *touched* math function gets its test updated in the same PR. Existing untested math in the repo is not backfilled on sight — it's tracked in `docs/math-sources-of-truth.md` under `Status: pending-fixture` and paid down on touch. The goal is to stop accruing debt, not to halt other work while old debt is serviced.

## Angular (Vitest)

- **Angular Testing Library** for component tests: `render()` + `screen` queries.
- **Mock services at the DI level** via `providers: [...]`.
- **Test rendered output**, not private signal values.
- **Name**: `*.component.spec.ts`, `*.service.spec.ts`.
- **User events**: `@testing-library/user-event` for clicks, typing, etc. More realistic than raw event dispatching.

## .NET (xUnit)

- **`[Fact]`** for single-case tests. **`[Theory, InlineData(...)]`** for parameterized.
- **Arrange → Act → Assert** with blank-line separation. One logical assertion per test.
- **Mock interfaces** with NSubstitute or Moq. Never mock concrete classes.
- **Async tests** return `Task`. Never `.Result`, `.Wait()`, or `.GetAwaiter().GetResult()`.
- **Name**: `MethodName_Scenario_ExpectedResult` (e.g., `GetAggregates_EmptyResponse_ReturnsEmptyList`).
- **GraphQL resolver tests**: use `IRequestExecutor` against the schema.

## Python (pytest)

- **pytest-asyncio** for async tests. `asyncio_mode = "auto"` in pyproject.toml.
- **Function-scoped fixtures** by default for isolation. Module/session scope only when initialization is genuinely expensive.
- **`httpx.AsyncClient` with `ASGITransport(app=app)`** for FastAPI endpoint tests.
- **`respx` or `pytest-httpx`** to mock external HTTP.
- **Name**: `test_<function>_<scenario>`.
- **Parameterize** with `@pytest.mark.parametrize` for edge-case sweeps.

## Fixtures and test data

- **Golden fixtures** for ported math: small, deterministic, with the source attributed in the filename or a sibling `README.md`.
- **Synthetic data** for unit tests: prefer generated data with a fixed seed over large files.
- **Real market data** for integration tests: pin to specific date ranges that are known-stable (no corporate actions, no halts) or document the known anomalies.

## Coverage

- **No arbitrary coverage targets.** 100% coverage is a smell (often testing implementation details) and 0% is obviously bad.
- Aim for **every branch in business logic** tested. Don't chase coverage on boilerplate.
- Coverage reports are diagnostic, not a goal.

## Pre-push test-suite hygiene

Before pushing or opening a PR, run the full per-stack test suite and **distinguish your work's failures from inherited failures**.

- **Run at project scope**, not just the files you touched. Tests for unrelated areas can break from a shared-helper edit, an env-var rename, or a Pydantic / pandas / SDK upgrade. Per-file pytest is for fast iteration; the project-scope run is what CI does and what reviewers will see.
  - Python: `podman exec polygon-data-service python -m pytest /app/tests`
  - .NET: `cd Backend.Tests && dotnet test`
  - Frontend: `podman exec my-frontend npx ng test --watch=false`
- **Establish a baseline before you treat a failure as "not mine."** Stash your changes, check out `origin/<base-branch>`, run the same command, and confirm the failure is pre-existing. Anything not on that pre-existing list is yours to fix or surface.
- **Pre-existing failures must be surfaced in the PR description**, not silently ignored. They're inherited tech debt, but they shouldn't mask your work's failures or make a reviewer wonder what's new.
- **Container-state hygiene** when iterating with `podman cp`: copy specific files (`podman cp local/path/file.py container:/app/path/file.py`), never directories with trailing `/` or container destinations that already exist as a directory — `podman cp src/ container:/app/dst` will create `/app/dst/src/` if `/app/dst/` exists, polluting test discovery. After a long iteration session, `rm -rf` any duplicated paths you created (or rebuild the container) before treating a test-suite run as authoritative.

## Reconciliation tests

When a port has been reconciled against a reference (see `reconcile-backtest` skill), the test lives in `tests/integration/reconciliation/test_<name>.py` and:

- Loads both our engine's output and the reference output as fixtures
- Asserts signal-by-signal equivalence at the strategy level
- Asserts trade-by-trade equivalence if the commission and fill models match
- Documents any accepted divergences inline with a reference to `docs/references/reconciliations/<name>.md`
