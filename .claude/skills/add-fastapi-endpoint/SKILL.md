---
name: add-fastapi-endpoint
description: Add a new FastAPI endpoint in PythonDataService that exposes engine output (indicators, backtest results, market data) to the frontend or backend. Use when user says "add an endpoint", "expose this via FastAPI", "create a route for", "I need the frontend to get", or asks to wire up new Python logic to HTTP.
---

# Add FastAPI Endpoint

Add a new FastAPI endpoint in `PythonDataService/` that exposes engine output to HTTP consumers (the .NET backend or, in some cases, the Angular frontend directly).

## When to use

- User wants new Python logic reachable over HTTP
- User has existing Python code (a port, a calculation) and wants to expose it
- User asks to modify an existing endpoint significantly

## Prerequisites to check

Before adding, verify:

1. Does a similar endpoint already exist? Search `PythonDataService/app/routers/` first. Don't duplicate.
2. Is the underlying engine logic implemented and tested? If not, build that first, then expose it.
3. Does the response shape need to match a consumer's existing deserialization? The .NET backend uses `JsonNamingPolicy.SnakeCaseLower`, so Python responses must use `snake_case` keys.

## Execution

### 1. Design the contract first

Write the Pydantic request and response models before the handler.

- **Request model**: inputs needed, types, validators, defaults. Prefer explicit over implicit — `symbol: str` should probably be `symbol: Literal["SPY", "QQQ", ...]` or validated via `Field(pattern=...)` if it's a known universe.
- **Response model**: exact output shape. Include units in field names where non-obvious (`duration_seconds` not `duration`, `price_usd` not `price`).
- Use Pydantic v2 patterns: `model_validator`, `field_validator`, not the v1 `@validator` decorator.
- `from __future__ import annotations` at the top of the module for forward references.

Place schemas in `PythonDataService/app/schemas/<domain>.py`, not inline in the router.

### 2. Write the router

Follow the existing router pattern in `PythonDataService/app/routers/`.

```python
from fastapi import APIRouter, Depends, HTTPException
from app.schemas.indicators import EmaRequest, EmaResponse
from app.services.indicator_service import IndicatorService, get_indicator_service

router = APIRouter(prefix="/indicators", tags=["indicators"])

@router.post("/ema", response_model=EmaResponse)
async def compute_ema(
    request: EmaRequest,
    service: IndicatorService = Depends(get_indicator_service),
) -> EmaResponse:
    try:
        result = await service.compute_ema(
            symbol=request.symbol,
            period=request.period,
            start=request.start,
            end=request.end,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return EmaResponse.from_engine_result(result)
```

Rules:

- **Always `async def`** for route handlers.
- **Use `Depends()`** for service injection. Services are module-level singletons, exposed via a `get_<service>` factory.
- **Explicit `HTTPException`** with meaningful status codes. No bare `raise`.
- **Response construction in a `from_engine_result` classmethod** on the Pydantic model — keeps the router thin.
- **Register the router** in `PythonDataService/app/main.py` via `app.include_router(...)`.

### 3. Separate the service from the endpoint

The endpoint is transport. The service is logic. They live in different files.

- `app/routers/indicators.py` — HTTP concerns only
- `app/services/indicator_service.py` — engine calls, data loading, computation
- Service methods are testable without a running FastAPI instance.

### 4. Write tests

Two layers of test:

**Service-level unit tests** (`tests/unit/services/test_indicator_service.py`):
- Mock external data (Polygon, Postgres) at the HTTP or connection layer using `respx` or a DB fixture.
- Assert on the engine's output shape and values.

**Endpoint-level integration tests** (`tests/integration/routers/test_indicators.py`):
- Use `httpx.AsyncClient` with `ASGITransport(app=app)` — NOT `TestClient` for async routes.
- Hit the endpoint with a realistic request, assert on status code, response shape, and at least one value.
- Mock the service layer for endpoint tests; don't mock at the HTTP layer again.

### 5. Wire to the consumer

The endpoint usually isn't the final product — it has a consumer. Depending on who consumes it:

- **.NET backend**: Add or update a typed client in `Backend/` that calls this endpoint. Use `JsonNamingPolicy.SnakeCaseLower` for deserialization. Handoff: tell the user the endpoint is live, and either delegate the .NET client work to a follow-up task or invoke the `write-graphql-resolver` skill if the endpoint output is being exposed via GraphQL.
- **Angular frontend (rare)**: Usually the frontend goes through the .NET GraphQL gateway, not directly to Python. Confirm with the user if they want direct access.

## Output

Report:

- Endpoint path and method
- Request/response schema summary
- Files created or modified (router, schemas, service, tests)
- Test coverage (which tests pass, what they cover)
- Whether a downstream client (.NET, Angular) needs to be updated, and whether you're doing that now or deferring

## Anti-patterns to avoid

- Logic inside the route handler (put it in a service)
- `TestClient` for async FastAPI routes (use `httpx.AsyncClient` + `ASGITransport`)
- Pydantic v1 patterns (`@validator`, `Config` inner class) — this project is v2
- Naked `dict` responses instead of Pydantic models
- Silent exception handlers that swallow engine errors into 200 responses
- `camelCase` field names in responses (the .NET consumer expects `snake_case`)
- Duplicating schemas across routers instead of sharing them
