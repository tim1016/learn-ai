---
paths:
  - "PythonDataService/**"
---

# Python rules

Targets Python 3.12+ (the Dockerfile, CI and ruff `target-version` are all 3.12; PEP 695 generics are in use) with FastAPI, Pydantic v2, pandas.

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
- **ruff** for linting. Enforce zero warnings — and run at **project scope**, not file scope. Before committing or pushing any Python change run:
  ```
  ruff check PythonDataService/app/ PythonDataService/tests/
  ```
  This is the same scope CI uses. The pre-commit hook (`lint-staged`) only lints staged paths, so cross-file drift slips through — unused imports left after a refactor, sort order broken by a new import, dead `# noqa` directives. Per-file `ruff check <one_file>.py` is *not* a substitute. Fix issues outside files you touched in a separate commit and surface it; do not silently re-format unrelated code as part of your task.

## Adding a Python dependency

The runtime deps are split into two files:

- `PythonDataService/requirements-heavy.txt` — large or slow-to-install binaries (numpy, pandas, scipy, statsmodels, pyarrow, ...). Cached as a separate Docker layer.
- `PythonDataService/requirements-light.txt` — everything else.

**A `requirements.txt` file exists but is deprecated** — it does not list any deps and is not consumed by Dockerfile, CI, or `requirements-dev.txt`. Do **not** add deps there. (It used to be a hand-maintained concat, drifted silently from heavy/light, and broke CI when CI was reading it as the source of truth.)

When adding a dep:
1. Decide heavy vs light by install time and binary size — if in doubt, light.
2. Pin a version (`==`) for app-critical deps; lower-bound + upper-bound (`>=X,<Y`) for dev-aligned packages where minor drift is fine.
3. If the container is running, `podman exec polygon-data-service pip install <pkg>` to test immediately, but **also commit the file change** — the local pip install evaporates on rebuild.
4. CI installs `requirements-heavy.txt + requirements-light.txt + requirements-dev.txt` directly. There is no "regenerate the convenience file" step anymore.

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
- **`DatetimeIndex` with timezone**. Trading logic is `America/New_York`
  when wall-clock semantics matter; storage, wire, and serialized artifacts
  use `int64 ms UTC`. Never naive datetimes.
- **Vectorized operations** preferred. Fall back to `.apply()` only when necessary and document why.
- **Copy-on-write is the default in pandas 3**: be explicit about when you're mutating vs returning a new DataFrame.

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

## Common pitfalls

- Naked `dict` responses (use Pydantic models)
