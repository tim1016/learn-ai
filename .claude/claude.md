# Coding Guidelines

## Angular

### Signals & Reactivity (REQUIRED)
- Use `signal()` for component state, `computed()` for derived values, `effect()` for side effects
- Use `input()` and `output()` functions — NOT `@Input()` / `@Output()` decorators
- Use `input.required<T>()` when the input must be provided by the parent
- Use `inject()` for dependency injection — NOT constructor injection
- Access signal values with function calls: `{{ ticker() }}`, `this.loading()`
- Use `model()` for two-way binding instead of `input()` + `output()` pairs
- use @ let, @for, @switch in the template rather than creating too much complecity in the template.
- If type safety is not being met in the html file, try creating a getter in the TS file
- Never mutate signal values directly — use `set()`, `update()`, or `mutate()`

### Components
- All components must be `standalone: true` (no NgModules)
- Always use `ChangeDetectionStrategy.OnPush`
- Use modern control flow: `@if`, `@for` (with `track`), `@switch` — NOT `*ngIf`, `*ngFor`, `ngSwitch`
- `@for` blocks **must** include a `track` expression — prefer tracking by a unique ID, not `$index`
- Co-locate component files (`*.component.ts`, `.html`, `.scss`) in the same folder
- Keep templates under ~80 lines — extract child components when exceeded

### RxJS
- Try to use forward looking resource, if you can avoid RXJS altogether. 
- Use `firstValueFrom()` for one-shot observable-to-promise conversion
- Use `takeUntilDestroyed()` for automatic subscription cleanup — call it inside an injection context or pass a `DestroyRef`
- Avoid `.subscribe()` in components when `async` pipe or `toSignal()` can be used instead
- Prefer declarative streams (`combineLatest`, `switchMap`) over imperative subscribe-and-set patterns

### Routing
- Use functional route guards and resolvers — NOT class-based guards
- Prefer lazy-loaded routes via `loadComponent` / `loadChildren`

### File Naming
- Components: `kebab-case.component.ts` / `.html` / `.scss`
- Services: `kebab-case.service.ts`
- Models/types: `kebab-case.ts` in a `models/` folder or co-located
- Guards/resolvers: `kebab-case.guard.ts` / `kebab-case.resolver.ts`

---

## .NET

### Naming
- Classes, methods, properties: `PascalCase`
- Private fields: `_camelCase` with underscore prefix
- Async methods: suffix with `Async` (e.g., `FetchAndStoreAggregatesAsync`)
- Interfaces: prefix with `I` (e.g., `IMarketDataService`)
- Constants / static readonly: `PascalCase` (following .NET convention)

### Patterns
- Async/await everywhere for I/O — always pass `CancellationToken` through call chains
- Interface-based services (`IMarketDataService` → `MarketDataService`)
- Register services with the narrowest lifetime possible (`Scoped` > `Singleton` unless stateless)
- Structured logging with step tracing: `logger.LogInformation("[STEP X] ...")`
- Use `IOptions<T>` / `IOptionsSnapshot<T>` for configuration — NOT raw `IConfiguration` reads scattered through code
- Return early / guard clause pattern — avoid deep nesting

### Hot Chocolate (GraphQL)
- Always use `[GraphQLName("fieldName")]` to control exposed names (HC v15 strips "Get" prefix)
- Use `[Service]` attribute for resolver injection
- Use `DataLoader` for batched fetching to avoid N+1 resolver patterns
- Return concrete error types / union results instead of throwing exceptions from resolvers when representing domain errors

### Data
- Use `JsonNamingPolicy.SnakeCaseLower` when deserializing Python service responses
- Prefer batch operations over N+1 query patterns
- Use `AsNoTracking()` for read-only EF Core queries

---

## Python

### Style
- Type hints on all function signatures (params **and** return types)
- `async def` for all route handlers
- `snake_case` for functions/variables, `CONSTANT_CASE` for config
- Module-level service instantiation (singleton pattern)
- Use `from __future__ import annotations` for forward-reference support

### FastAPI
- Router pattern with `app.include_router()`
- Pydantic models for request/response schemas — use `model_validator` over `@validator` (Pydantic v2)
- `HTTPException` for error responses with meaningful status codes and detail messages
- Use `Depends()` for shared dependencies (DB sessions, auth, service instances)

### Data & ML
- Use `pandas` with explicit `dtype` specifications — avoid silent type coercion
- Seed random number generators for reproducibility in ML pipelines
- Separate feature engineering from model training into distinct, testable functions

---

## Testing

### Philosophy
- **Write tests before or alongside new features** — not as an afterthought
- Tests are first-class code: same naming conventions, same review standards
- Every bug fix should include a regression test that reproduces the bug before confirming the fix
- Prefer fast, isolated unit tests; use integration tests only for cross-boundary concerns (API calls, DB queries)

### Angular (Vitest / Jest + Angular Testing Library)
- Test **behavior**, not implementation — assert what the user sees/does, not internal signal values
- Use `Angular Testing Library` (`@testing-library/angular`) for component tests — prefer `render()` + `screen` queries over raw `TestBed` boilerplate
- Mock services at the injection level — provide fakes via `providers` in the test module
- For signal-heavy components: trigger changes, then assert rendered output — don't peek at private signals
- Name test files: `kebab-case.component.spec.ts`, `kebab-case.service.spec.ts`

### .NET (xUnit + NSubstitute / Moq)
- Use xUnit with `[Fact]` and `[Theory]` — prefer `[Theory, InlineData(...)]` for parameterized cases
- Follow **Arrange → Act → Assert** structure with blank-line separation
- Mock interfaces (not concrete classes) using NSubstitute or Moq
- Test async methods with real `async Task` test methods — never `.Result` or `.Wait()`
- Name pattern: `MethodName_Scenario_ExpectedResult` (e.g., `GetAggregatesAsync_EmptyResponse_ReturnsEmptyList`)
- Integration tests for Hot Chocolate resolvers: use `IRequestExecutor` to execute raw queries against the schema

### Python (pytest)
- Use `pytest` with `pytest-asyncio` for async test support
- Fixtures for shared setup — prefer function-scoped fixtures for isolation
- Use `httpx.AsyncClient` (via `ASGITransport`) for FastAPI endpoint tests — NOT `TestClient` for async routes
- Mock external API calls (Polygon, IBKR) at the HTTP layer with `respx` or `pytest-httpx`
- Name pattern: `test_<function>_<scenario>` (e.g., `test_fetch_aggregates_rate_limited`)
- For ML/data code: assert on shapes, dtypes, and value ranges — not exact floating-point values

### What to Test (Priority Order)
1. **Business logic & transformations** — pure functions, calculations, data mappings
2. **Service methods** — especially those with branching logic or error handling
3. **API endpoints / resolvers** — request → response contract validation
4. **Component behavior** — user interactions, conditional rendering, form validation
5. **Edge cases** — empty data, null inputs, boundary values, error responses

### What NOT to Test
- Framework boilerplate (Angular routing config, module declarations)
- Simple pass-through getters/setters with no logic
- Third-party library internals
- Exact CSS / styling (use visual regression tools if needed)

---

## General

### Do NOT
- Add unnecessary comments, docstrings, or type annotations to code you didn't change
- Over-engineer or add features beyond what was asked
- Create new files when editing existing ones would work
- Introduce new dependencies without justification
- Leave `console.log` / `print()` statements in committed code — use structured logging

### Do
- Keep changes minimal and focused on the task
- Follow existing patterns in the file you're editing
- Use structured logging when adding new backend logic
- Handle errors explicitly — no silent catches (`catch {}` / `except: pass`)
- Validate inputs at system boundaries (API endpoints, user inputs, external data)
- Write a test for any non-trivial new logic or bug fix