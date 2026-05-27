# PythonDataService — FastAPI Data Service

## Commands

| Action     | Command                                                                      |
|------------|------------------------------------------------------------------------------|
| Run        | `podman compose up python-service` (localhost:8000)                          |
| Test       | `podman exec polygon-data-service python -m pytest tests/ -v`                |
| Test (fast)| `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"`  |
| Build      | `podman compose build python-service`                                        |
| Lint       | `ruff check PythonDataService/app/`                                          |
| Format     | `ruff format PythonDataService/app/`                                         |
| Logs       | `podman logs -f polygon-data-service`                                        |
| API docs   | http://localhost:8000/docs (Swagger UI)                                      |

Python service has **no dependencies** — runs standalone.

## LEAN Sidecar launcher (Phase 2a)

`/api/lean-sidecar/*` endpoints in the data plane forward to a
**separate** launcher service that owns Podman API access. The
launcher is NOT inside the `polygon-data-service` container (see
`docs/architecture/lean-sidecar-lab.md` §"Launcher topology" for why).

Run it as a host process alongside `podman compose up`:

```bash
podman pull docker.io/quantconnect/lean:latest
python PythonDataService/scripts/lean_sidecar_pin_image.py
cd PythonDataService
# Bind to 0.0.0.0 so the polygon-data-service container can reach
# the launcher via host.containers.internal. Loopback-only would refuse
# connections from the container's network namespace.
.venv/Scripts/python.exe -m uvicorn app.lean_sidecar.launcher.app:app \
    --host 0.0.0.0 --port 8090
```

The data plane reads `LEAN_LAUNCHER_URL` (default `http://127.0.0.1:8090`
when both processes share localhost) and the optional `LEAN_LAUNCHER_TOKEN`
env var. The data plane inside compose reaches the host launcher via
`host.containers.internal`; that alias is registered as
`extra_hosts: - "host.containers.internal:host-gateway"` on
`python-service` in `compose.yaml`, which maps it to the container's
default gateway on Windows and Linux Podman. `host.docker.internal` is
also registered for Docker/Desktop compatibility and older local envs.
The compose default for the env is `http://host.containers.internal:8090`;
override `LEAN_LAUNCHER_URL` only for non-standard runtimes (remote
launcher, non-default port). Do not pin a machine-specific LAN IP. After
bringing compose up, verify the data-plane container can reach the launcher with
`curl http://localhost:8000/api/lean-sidecar/diagnose` — the report
covers URL config, token resolution, and a live `/healthz` probe.
Containerizing the launcher itself is the Phase 2b / Phase 1c
hardening pass.

## File Structure

```
app/
├── main.py                       # FastAPI app init, router registration
├── config.py                     # Pydantic Settings (env vars)
├── routers/                      # 19 API route modules
│   ├── aggregates.py             # Polygon OHLCV fetching
│   ├── options.py                # Options chain snapshots
│   ├── indicators.py             # SMA, EMA, RSI, MACD, Bollinger Bands
│   ├── engine.py                 # Lean engine integration
│   ├── strategy.py               # Strategy execution
│   ├── backtest.py               # Event replay backtesting
│   ├── research.py               # Batch research experiments
│   ├── sanitize.py               # Gap detection & data cleaning
│   ├── data_quality.py           # Data validation
│   ├── volatility.py             # Volatility surface analysis
│   ├── quantlib_options.py       # Black-Scholes pricing
│   ├── market_monitor.py         # Real-time ticker monitoring
│   └── ...                       # chart, dataset, snapshot, tickers, etc.
├── services/                     # Business logic layer
│   ├── polygon_client.py         # Polygon.io SDK wrapper
│   ├── ta_service.py             # Technical analysis calculations
│   ├── sanitizer.py              # Data sanitization pipeline
│   ├── strategy_engine.py        # Strategy execution engine
│   ├── quantlib_pricer.py        # QuantLib option pricing
│   ├── strategies/               # 7 strategy implementations
│   └── ...
├── engine/                       # Lean Framework backtesting (37 files)
│   ├── consolidators/            # OHLCV bar consolidation
│   ├── data/                     # Data providers
│   ├── execution/                # Trade execution models
│   ├── framework/                # Engine orchestration
│   ├── indicators/               # Technical indicator implementations
│   ├── options/                  # Options Greeks
│   ├── results/                  # Result aggregation
│   ├── strategy/                 # Strategy base classes
│   └── tests/                    # Engine-specific tests
├── research/                     # Research modules (30 files)
│   ├── features/                 # Feature engineering
│   ├── options/                  # Options research
│   ├── signal/                   # Signal research
│   └── validation/               # Validation routines
├── ml/                           # Machine learning preprocessing
├── volatility/                   # Volatility surface analysis
├── models/                       # Pydantic request/response models
└── utils/                        # Shared utility functions
```

## Key Patterns

- **FastAPI router pattern** — `app.include_router(router, prefix=..., tags=[...])`
- **Pydantic v2** models for request/response — use `model_validator` (not `@validator`)
- **`async def`** for all route handlers
- **Module-level singletons** for services (instantiated at import time)
- **pandas + pandas-ta** for indicator calculations
- **Polygon.io SDK v1.12.5** — `list_snapshot_options_chain()` uses `params={}` dict
- Requirements split for Docker caching: `requirements-heavy.txt` (scipy, numpy, pandas — layer 1), `requirements-light.txt` (FastAPI, app deps — layer 2)

## Testing

- **pytest** with `asyncio_mode = auto` (in `pytest.ini`)
- **httpx.AsyncClient** + `ASGITransport` for endpoint tests (not `TestClient`)
- Fixtures in `tests/conftest.py`
- Mock external APIs (Polygon, FRED) at HTTP layer with `respx` or `pytest-httpx`
- Name pattern: `test_<function>_<scenario>`
- Marker `@pytest.mark.slow` for long-running ML/backtest tests

## Gotchas

- Polygon Starter plan: 2-year max history, 15-min delayed, options snapshots only for live contracts
- `DatetimeIndex.astype("int64")` returns **microseconds** in pandas 3.0 (not nanoseconds)
- Polygon 07:00 ET bars can have inflated close prices from late settlement trades
- Volume mount in compose: `./PythonDataService/app:/app/app:z` — only `app/` is hot-reloaded
