# Python rules

Targets Python 3.11+ with FastAPI, Pydantic v2, pandas. Read when writing or editing code under `PythonDataService/`.

**Authoritative references**:
- https://fastapi.tiangolo.com
- https://docs.pydantic.dev/latest/
- https://pandas.pydata.org/docs/

## Style

- **Type hints on every function signature.** Params and return types.
- **`from __future__ import annotations`** at the top of every module for forward references.
- **`async def`** for all route handlers and any function doing I/O.
- **`snake_case`** for functions, methods, variables. **`CONSTANT_CASE`** for module-level constants.
- **`PascalCase`** for classes.
- **ruff** for linting. Enforce zero warnings.

## FastAPI

- **Router pattern**: one router per domain, included in `app/main.py` via `app.include_router()`.
- **Pydantic v2** exclusively: `model_validator`, `field_validator`. No `@validator` (v1 pattern) or `Config` inner class.
- **`Depends()`** for shared dependencies (DB, auth, service instances).
- **`HTTPException`** with meaningful status codes for error responses. No silent 500s.
- **Schema separation**: Pydantic models in `app/schemas/<domain>.py`, not inline in routers.
- **Service separation**: business logic in `app/services/<domain>_service.py`. Routers are transport only.

## Pydantic v2

- `model_validator(mode='after')` for cross-field validation.
- `field_validator('field_name')` for single-field.
- `Field(...)` with constraints (`ge`, `le`, `pattern`, `max_length`).
- Use `from_engine_result` (or similar) classmethods to construct response models from service output.
- Response models use snake_case field names (the .NET consumer expects snake_case).

## pandas

- **Explicit dtypes** on DataFrame construction and reads. Avoid silent type coercion.
- **`DatetimeIndex` with timezone**. Trading logic is `America/New_York`; storage is UTC. Never naive datetimes.
- **Vectorized operations** preferred. Fall back to `.apply()` only when necessary and document why.
- **Copy-on-write behavior** (pandas 2.x default): be explicit about when you're mutating vs returning a new DataFrame.

## NumPy and numerical code

- **`numpy.float64`** default precision. If a port uses a different precision, document why.
- **Explicit `atol` and `rtol`** in every `np.allclose` / `np.isclose` call. Never rely on defaults.
- **Seed any RNG** used in tests or reproducible pipelines. Use `numpy.random.default_rng(seed=...)`.
- **Separate feature engineering from model training** into distinct, testable functions.

## Async and I/O

- **`httpx.AsyncClient`** for HTTP calls from services.
- **Async DB access** where the driver supports it (asyncpg, aiosqlite).
- **Never mix** `asyncio.run` with an existing event loop.
- **Timeouts on all external calls.** No bare `httpx.get(url)` without a timeout.

## Logging

- **`logging` module** with a named logger per module: `logger = logging.getLogger(__name__)`.
- **Structured logs** using `extra={...}` dict, not string interpolation.
- **No `print()`** in committed code.

## Error handling

- **Explicit exceptions.** No bare `except:` or `except Exception: pass`.
- **Custom exception classes** for domain errors (`InsufficientDataError`, `InvalidSymbolError`) that routers translate to `HTTPException`.
- **Validate at boundaries**: API endpoints, external data ingestion. Internal trusted code doesn't need paranoid guards.

## Testing (see testing.md for cross-stack standards)

- **pytest** with `pytest-asyncio` for async.
- **Fixtures** scoped narrowly (function-scoped by default for isolation).
- **`httpx.AsyncClient`** with `ASGITransport(app=app)` for FastAPI endpoint tests. NOT `TestClient` for async routes.
- **`respx` or `pytest-httpx`** for mocking external HTTP calls.
- **Name pattern**: `test_<function>_<scenario>`.
- **Numerical tests**: assert on shapes, dtypes, and value ranges. Use explicit `atol`/`rtol` for float comparisons.

## Common pitfalls

- Pydantic v1 patterns (`@validator`, `Config` inner class) — this is v2
- `TestClient` for async routes (use `httpx.AsyncClient` + `ASGITransport`)
- camelCase response fields (consumer expects snake_case)
- Naked `dict` responses (use Pydantic models)
- Silent `except: pass` (explicit exception types only)
- Bare `httpx` calls without timeout
- `print()` in committed code
- `np.allclose(a, b)` with default tolerances (specify explicitly)
- Naive datetimes (always tz-aware)
