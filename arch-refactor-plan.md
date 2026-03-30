# Python Service: Dependency Cleanup & Build Optimization Plan

**Date:** 2026-03-29
**Goal:** Remove unused packages, fix Podman build warnings, make container builds faster and smaller.

---

## 1. Import Audit Results

Grep of every `import` in `PythonDataService/app/` (production code only — tests and notebooks excluded).

### Actually Imported (KEEP)

| Package | Import Location(s) | Purpose |
|---------|-------------------|---------|
| fastapi | main.py, all routers | Web framework |
| uvicorn | main.py (CMD) | ASGI server |
| pydantic | models/*.py | Request/response validation |
| pydantic-settings | config.py | Env-based settings |
| httpx | services/fred_service.py | Async HTTP client for FRED |
| pandas | ~30 files | DataFrames everywhere |
| numpy | ~20 files | Numerical ops |
| scipy | strategy_engine.py, research/options/bs_solver.py, research/validation/*.py, research/signal/*.py | Black-Scholes, stats, interpolation |
| statsmodels | ml/preprocessing/stationarity.py | ADF/KPSS stationarity tests |
| scikit-learn | ml/preprocessing/scaler.py | StandardScaler, RobustScaler, MinMaxScaler |
| matplotlib | ml/evaluation/visualization.py | Server-side plots (1 file) |
| polygon-api-client | services/polygon_client.py | Polygon.io SDK |
| pandas-ta | services/ta_service.py, services/dataset_service.py | Technical indicators |
| pandas-dq | services/sanitizer.py | Data quality Fix_DQ |
| python-dotenv | config.py | Local .env loading |
| python-multipart | FastAPI file upload dep | Transitive (required by FastAPI) |

### NOT Imported Anywhere (REMOVE)

These are installed in the container but **zero imports** exist in production code:

| Package | Current File | Est. Size | Why It's There |
|---------|-------------|-----------|----------------|
| **numba** | requirements-heavy.txt | ~150 MB | Was in heavy layer explicitly — removed, but pandas-ta 0.4.71b0 pulls it back as a transitive dep |
| **llvmlite** | requirements-heavy.txt | ~50 MB | numba dependency — same as above |
| **tensorflow** | requirements-lock.txt | ~500-800 MB | Manually pip-installed in container, captured by freeze |
| **keras** | requirements-lock.txt | ~50-100 MB | tensorflow transitive |
| **tensorboard** | requirements-lock.txt | ~50 MB | tensorflow transitive |
| **h5py** | requirements-lock.txt | ~30 MB | tensorflow transitive |
| **protobuf** | requirements-lock.txt | ~30 MB | tensorflow transitive |
| **grpcio** | requirements-lock.txt | ~50 MB | tensorflow transitive |
| **libclang** | requirements-lock.txt | ~50 MB | tensorflow transitive |
| **ipython** | requirements-light.txt | ~20 MB | Dev/debug tool, not needed in prod image |
| **requests** | requirements-light.txt | ~1 MB | httpx is used instead; `requests` is a transitive dep of polygon-api-client |
| **rich** | requirements-light.txt | ~5 MB | Not imported |
| **tqdm** | requirements-light.txt | ~1 MB | Not imported |
| **Werkzeug** | requirements-light.txt | ~2 MB | Not imported |
| **PyYAML** | requirements-light.txt | ~1 MB | Not imported |
| **click** | requirements-light.txt | ~1 MB | Not imported (transitive of uvicorn) |
| **pillow** | requirements-light.txt | ~10 MB | Not imported |
| **setuptools** | requirements.txt | ~2 MB | Build tool, not a runtime dep |
| **wheel** | requirements-light.txt | ~1 MB | Build tool |

**Total removable bloat: ~1.0 - 1.5 GB**

---

## 2. Current Build Problems

### 2.1 Dockerfile Issues

```dockerfile
# Current approach — installs globally, no venv
RUN pip install --no-cache-dir --prefix=/install -r requirements-heavy.txt
```

| Problem | Impact |
|---------|--------|
| `--prefix=/install` instead of venv | pip warns "not in a virtual environment" on every build |
| numba + llvmlite in heavy layer | +200 MB for zero imports |
| requirements-lock.txt has tensorflow tree | If lock file is ever used to install, adds ~1 GB |
| No `.dockerignore` | Build context includes `.git/`, `__pycache__/`, `tests/`, `notebooks/`, `trained_models/` |

### 2.2 Requirements File Confusion

- **5 requirements files** with overlapping and contradictory contents
- `requirements.txt` declares `scikit-learn>=1.5.0` and `requirements-heavy.txt` pins `scikit-learn==1.8.0` — which wins depends on install order
- `requirements-light.txt` has 69 packages, most are transitive deps that pip resolves automatically
- `requirements-lock.txt` was generated with `pip freeze` after manual tensorflow install — it's corrupted

### 2.3 Compose

```yaml
# Current: volume mount for hot-reload
volumes:
  - ./PythonDataService/app:/app/app:z
command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

The volume mount + `--reload` works but is a legacy pattern. Podman Compose now supports `develop.watch` for cleaner hot-reload.

---

## 3. Refactor Plan

### Step 1: Remove Unused Packages

**requirements-heavy.txt** — remove numba and llvmlite:
```
scikit-learn==1.8.0
scipy==1.17.1
numpy==2.2.6
pandas==3.0.1
matplotlib==3.10.8
statsmodels==0.14.6
```

**requirements-light.txt** — trim to only direct deps (remove all transitive):
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
pydantic-settings==2.1.0
python-dotenv==1.0.0
polygon-api-client==1.12.5
pandas-dq>=1.29
pandas-ta==0.4.71b0
httpx==0.28.1
python-multipart==0.0.20
```

**requirements-lock.txt** — delete entirely. Regenerate only after a clean build if needed.

**requirements-dev.txt** — add ipython here:
```
-r requirements.txt
pytest
pytest-asyncio
ipython
jupyter
```

**requirements.txt** — remove setuptools, it's a build dep not runtime:
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
pandas>=2.2.0
numpy>=2.2.6
pydantic==2.5.0
pydantic-settings==2.1.0
python-dotenv==1.0.0
polygon-api-client==1.12.5
pandas-dq>=1.29
pandas-ta
scipy>=1.14.0
httpx
statsmodels>=0.14.0
scikit-learn>=1.5.0
matplotlib>=3.9.0
```

### Step 2: Add `.dockerignore`

Create `PythonDataService/.dockerignore`:
```
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.coverage
htmlcov/
*.egg-info/
venv/
.venv/
notebooks/
trained_models/
tests/
.git/
requirements-dev.txt
requirements-lock.txt
```

Create `Backend/.dockerignore`:
```
bin/
obj/
*.user
*.suo
.vs/
```

### Step 3: Fix Dockerfile (venv + cleanup)

```dockerfile
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Use a real venv — eliminates pip warnings
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Layer 1: Heavy/stable libs (cached unless requirements-heavy.txt changes)
COPY requirements-heavy.txt .
RUN pip install --no-cache-dir -r requirements-heavy.txt

# Layer 2: Light/frequently-changing libs
COPY requirements-light.txt .
RUN pip install --no-cache-dir -r requirements-light.txt

# --- Runtime stage ---
FROM python:3.12-slim

WORKDIR /app

# curl for healthcheck, tzdata for ZoneInfo("US/Eastern")
RUN apt-get update && apt-get install -y \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY ./app ./app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Step 4: Compose Improvements (Optional)

Add resource limits and consider `develop.watch`:
```yaml
python-service:
  build: ./PythonDataService
  container_name: polygon-data-service
  restart: always
  ports:
    - "8000:8000"
  deploy:
    resources:
      limits:
        memory: 2G
  environment:
    - POLYGON_API_KEY=${POLYGON_API_KEY}
    - FRED_API_KEY=${FRED_API_KEY}
    - HOST=0.0.0.0
    - PORT=8000
    - ALLOWED_ORIGINS=http://backend:8080,http://localhost:5000,http://localhost:4200
  command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
  develop:
    watch:
      - action: sync
        path: ./PythonDataService/app
        target: /app/app
```

---

## 4. Expected Impact

| Metric | Before | After |
|--------|--------|-------|
| Image size (est.) | ~2.5 GB | ~1.0 - 1.3 GB |
| Packages in heavy layer | 8 (incl. numba) | 6 |
| Packages in light layer | 69 | 10 |
| Build context | Entire repo | Only app code + requirements |
| pip venv warning | Yes | No |
| requirements files | 5 (conflicting) | 4 (clean separation) |
| Lock file corruption | tensorflow tree | Deleted / regenerated clean |

---

## 5. Execution Order

```
1. Delete requirements-lock.txt
2. Trim requirements-heavy.txt (remove numba, llvmlite)
3. Trim requirements-light.txt (direct deps only)
4. Remove setuptools from requirements.txt
5. Move ipython to requirements-dev.txt
6. Add .dockerignore files
7. Replace Dockerfile with venv-based version
8. Rebuild: podman compose down python-service && podman compose up -d --build python-service
9. Verify: podman exec polygon-data-service pip list (confirm no tensorflow/numba)
10. Run tests: podman exec polygon-data-service pytest (confirm nothing broke)
```
