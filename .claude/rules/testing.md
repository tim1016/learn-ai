# Testing rules

## Philosophy

- **Write tests with the feature, not after.** Tests are not optional work deferred to "when there's time".
- **Tests are first-class code**: same naming conventions, same review standards, same refactoring discipline.
- **Prefer fast isolated unit tests.** Use integration tests only for cross-boundary concerns (API, DB).
- **Test behavior, not implementation.** Assert what the user / caller observes, not internal state.

## The test bar

A test earns its place when it proves an outcome a user or caller sees that no other test already proves. Five kinds fail the bar: (1) trivial, (2) copy and doc pins, (3) duplicates — keep the strongest, (4) mock theater, (5) retired features. A test that fails the bar is deleted outright — git history is the archive; no skip markers, quarantine folders or moves to the daily run. The same bar governs every new test.

How the bar reads in practice:
- A test whose subject is a state branch stays even when its only visible sign is a short label. Only a test whose subject is the copy itself is a copy pin.
- A shared formatter's own spec owns its wording. A component test that only repeats it is a duplicate.
- A test that asserts only the order of a fencing step stays when the order is the safety property and the end state looks the same either way.
- An absence check ("never says X") stays when it catches a specific false claim.
- A lint written as a test stays while the rule it enforces is in force.
- A tombstone test — one that only checks a retired feature stays gone — fails as kind 5.
- A parity test that has never run (collects nothing, or reads a missing path) proves nothing and goes.
- A golden fixture that records an outside fact (frozen vendor data) stays with its test, even when the test runs no app code. A fixture's regeneration script is part of the fixture.
- Text and AST pins on vendored reference algorithms are math-parity plumbing, not copy pins.
- On the money path, when unsure, keep.

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
5. Untested existing math gets its test when it is touched. Until then its provenance block says `Validated against: NONE — pending fixture`.

## Angular (Vitest)

- **Angular Testing Library** for component tests: `render()` + `screen` queries.
- **Mock services at the DI level** via `providers: [...]`.
- **Test rendered output**, not private signal values.
- **Name**: `*.component.spec.ts`, `*.service.spec.ts`.
- **User events**: `@testing-library/user-event` for clicks, typing, etc. More realistic than raw event dispatching.

## .NET (xUnit)

- **`[Fact]`** for single-case tests. **`[Theory, InlineData(...)]`** for parameterized.
- **Arrange → Act → Assert** with blank-line separation. One logical assertion per test.
- **Mock interfaces** with Moq. Never mock concrete classes.
- **Async tests** return `Task`. Never `.Result`, `.Wait()`, or `.GetAwaiter().GetResult()`.
- **Name**: `MethodName_Scenario_ExpectedResult` (e.g., `GetAggregates_EmptyResponse_ReturnsEmptyList`).
- **GraphQL resolver tests**: use `IRequestExecutor` against the schema.

## Python (pytest)

- **pytest-asyncio** for async tests. `asyncio_mode = auto` in `pytest.ini`.
- **Function-scoped fixtures** by default for isolation. Module/session scope only when initialization is genuinely expensive.
- **`httpx.AsyncClient` with `ASGITransport(app=app)`** for FastAPI endpoint tests.
- **`respx`** to mock external HTTP.
- **Name**: `test_<function>_<scenario>`.
- **Parameterize** with `@pytest.mark.parametrize` for edge-case sweeps.

## Fixtures and test data

- **Synthetic data** for unit tests: prefer generated data with a fixed seed over large files.
- **Real market data** for integration tests: pin to specific date ranges that are known-stable (no corporate actions, no halts) or document the known anomalies.

## Coverage

- **No arbitrary coverage targets.** 100% coverage is a smell (often testing implementation details) and 0% is obviously bad.
- Aim for **every branch in business logic** tested. Don't chase coverage on boilerplate.
- Coverage reports are diagnostic, not a goal.

## Running tests

- Locally, run the test that proves your change — and, when you edit a shared helper, the tests of what imports it. CI runs every quick test; the daily run runs everything (AGENTS.md → Gates).
  - Python: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/<targeted paths>` (host venv)
  - Frontend: `npx ng test --include='src/app/<exact>.spec.ts'` (exact spec file, never a directory glob)
- Mark a test `@pytest.mark.slow` when it needs heavy infrastructure or pushes a CI job past its 2-minute cap. It runs daily. Never delete numerical-parity or regression coverage to fit the cap.
- Before calling a failure "not mine", run the same command on `origin/<base-branch>`. A pre-existing failure goes in the PR description.
- **Container-state hygiene** when iterating with `podman cp`: copy specific files (`podman cp local/path/file.py container:/app/path/file.py`), never directories with trailing `/` or container destinations that already exist as a directory — `podman cp src/ container:/app/dst` will create `/app/dst/src/` if `/app/dst/` exists, polluting test discovery. After a long iteration session, `rm -rf` any duplicated paths you created (or rebuild the container) before treating a test-suite run as authoritative.
- **The strategy-validation flag ledger needs no manual isolation step.** `PythonDataService/tests/conftest.py`'s autouse `_isolate_strategy_validation_flag_ledger` fixture points every test's `DEFAULT_FLAG_EVENTS_PATH` at an empty tmp-path location, so a developer's real, gitignored `artifacts/strategy_validation/flag_events.json` never affects test outcomes — don't `mv` it aside before a run.

## Reconciliation tests

When a port has been reconciled against a reference (see `reconcile-backtest` skill), the test lives in `tests/integration/reconciliation/test_<name>.py` and:

- Loads both our engine's output and the reference output as fixtures
- Asserts signal-by-signal equivalence at the strategy level
- Asserts trade-by-trade equivalence if the commission and fill models match
- Documents any accepted divergence inline in the test and in the port's module docstring
