# MarketScope Code Review — Top 3 Improvement Requirements

**Date:** 2026-03-31
**Scope:** Stock-related code, Podman container robustness, dependency leanness
**Context:** Local dev project, moderate image-size sensitivity, ML/research features used occasionally

---

## 1. Harden compose.yaml for Reliable Local Dev

**Problem:** The current `compose.yaml` has several patterns that make the dev loop fragile and insecure even for local use.

### 1a. Hardcoded secrets in the compose file

The Postgres password `mysecretpassword` is committed in plaintext in two places:

```yaml
# compose.yaml line 7
- POSTGRES_PASSWORD=mysecretpassword
# compose.yaml line 54
- ConnectionStrings__DefaultConnection=Host=db;...Password=mysecretpassword
```

Even for local dev, this is risky because `compose.yaml` is checked into version control and the `.env` file pattern is already partially in place (you have `.env.example` for the Polygon key). If the repo ever becomes public or shared, the password leaks.

**Fix:** Move all secrets to `.env` and reference them with variable interpolation:

```yaml
# compose.yaml
environment:
  - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
  # ...
  - ConnectionStrings__DefaultConnection=Host=db;Database=postgres;Username=postgres;Password=${POSTGRES_PASSWORD}
```

```dotenv
# .env (gitignored)
POSTGRES_PASSWORD=mysecretpassword
POLYGON_API_KEY=poly_xxx
FRED_API_KEY=fred_xxx
```

Update `.env.example` to document all required variables with placeholder values.

### 1b. Backend container uses the full SDK image at runtime

```yaml
# compose.yaml line 45
image: mcr.microsoft.com/dotnet/sdk:10.0
```

The `sdk:10.0` image is ~900MB. You already have a proper multi-stage `Backend/Dockerfile` that produces a lean `aspnet:10.0` runtime image (~220MB), but `compose.yaml` ignores it entirely — it mounts the source and runs `dotnet watch`. This is fine for hot-reload during dev, but means the Dockerfile is never exercised and could silently rot.

**Fix:** Add a `backend-prod` profile (or a separate `compose.prod.yaml`) that uses the Dockerfile so you can periodically validate it builds:

```yaml
backend:
  profiles: ["dev"]
  image: mcr.microsoft.com/dotnet/sdk:10.0
  # ... existing dev config

backend-prod:
  profiles: ["prod"]
  build: ./Backend
  # ... production config using the Dockerfile
```

### 1c. No resource limits on any container

None of the three services have memory or CPU limits. A runaway pandas operation or a large EF Core query can consume all host memory and freeze your machine.

**Fix:** Add conservative limits, especially to the Python service which loads heavy dataframes:

```yaml
python-service:
  deploy:
    resources:
      limits:
        memory: 2G
        cpus: "2.0"
      reservations:
        memory: 512M

backend:
  deploy:
    resources:
      limits:
        memory: 1G

db:
  deploy:
    resources:
      limits:
        memory: 512M
```

### 1d. Missing healthcheck on the backend service

The `db` and `python-service` have healthchecks, but the `backend` service does not. Since it's running `dotnet watch`, a crash-loop would go undetected.

**Fix:** Add a healthcheck endpoint to the .NET backend (e.g., `/health` via `app.MapHealthChecks()`) and add a compose healthcheck:

```yaml
backend:
  healthcheck:
    test: ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"]
    interval: 10s
    timeout: 5s
    retries: 3
    start_period: 15s
```

---

## 2. Trim and Organize Python Dependencies

**Problem:** The Python service image carries ~600MB of scientific libraries, some of which are only used in niche research modules. The dependency files also have version drift between `requirements.txt` (loose ranges) and the layered files (pinned versions).

### 2a. Three requirements files with conflicting versions

You have `requirements.txt`, `requirements-heavy.txt`, and `requirements-light.txt` with overlapping packages at different version specs:

| Package | requirements.txt | requirements-heavy.txt | requirements-light.txt |
|---------|-----------------|----------------------|----------------------|
| fastapi | ==0.104.1 | — | ==0.104.1 |
| pandas | >=2.2.0 | ==3.0.1 | — |
| numpy | >=2.2.6 | ==2.2.6 | — |
| scipy | >=1.14.0 | ==1.17.1 | — |

The Dockerfile only installs `requirements-heavy.txt` + `requirements-light.txt`. The base `requirements.txt` is essentially dead weight — it's not used by any build step. Anyone running `pip install -r requirements.txt` locally would get different versions than the container.

**Fix:** Delete `requirements.txt` and keep only the two layered files as the single source of truth. Add a comment at the top of each explaining the split strategy. If you want a single flat file for local `pip install`, generate it:

```bash
cat requirements-heavy.txt requirements-light.txt > requirements-all.txt
```

### 2b. statsmodels is only imported in one file

`statsmodels` (and its transitive dependency `patsy`) adds ~50MB and is only used in `app/ml/preprocessing/stationarity.py` for the ADF stationarity test. Since you use this occasionally:

**Fix:** Make it a lazy import so the service starts and serves stock data without needing it loaded:

```python
# stationarity.py
def adf_test(series):
    from statsmodels.tsa.stattools import adfuller  # lazy import
    result = adfuller(series)
    ...
```

Then move `statsmodels` from `requirements-heavy.txt` to a `requirements-research.txt` that's only installed when needed (or keep it but document it as optional). This won't save image size if it's still installed, but it improves startup time and makes the dependency explicit.

### 2c. pandas-ta — keep the original, pin explicitly

`pandas-ta==0.4.71b0` was evaluated for replacement with `pandas-ta-classic`, but the classic fork uses a different import name (`pandas_ta_classic` vs `pandas_ta`) making it non-drop-in. The original `pandas-ta` was updated in September 2025 and supports Python 3.12+, so it remains viable. With 30+ indicators used across 5 files and a dynamic dispatch system in `dataset_service.py`, replacing the library would require significant refactoring for minimal benefit.

**Decision:** Keep `pandas-ta==0.4.71b0` pinned in `requirements-light.txt`. Ensure the version is explicitly pinned (not unpinned) so builds are reproducible.

---

## 3. Add Container Lifecycle Documentation and a Production-Ready Compose Profile

**Problem:** The README is comprehensive for features and architecture, but has no section on how to operate the containers — how to rebuild after dependency changes, how to handle data persistence, how to debug a stuck container, or what the difference between the dev compose and the Dockerfiles is.

### 3a. Add a "Container Operations" section to the README

Suggested content:

```markdown
## Container Operations

### First-time setup
1. Copy `.env.example` to `.env` and fill in your API keys
2. Run `podman compose up -d`
3. Wait for healthchecks to pass: `podman compose ps`

### Rebuilding after dependency changes
- Python deps changed: `podman compose build python-service --no-cache`
- .NET deps changed: Container auto-restores on restart (dev mode)
- Schema migration: `podman compose exec backend dotnet ef database update`

### Debugging
- View logs: `podman compose logs -f python-service`
- Shell into container: `podman compose exec python-service bash`
- Check healthcheck status: `podman inspect --format='{{json .State.Health}}' polygon-data-service`

### Data persistence
- PostgreSQL data lives in the `pgdata` named volume
- To reset: `podman compose down -v` (destroys all data)
- To backup: `podman exec my-postgres pg_dump -U postgres postgres > backup.sql`
```

### 3b. Document the dev vs. production container strategy

The current setup has a disconnect: the Dockerfiles are written for production (multi-stage, lean runtime images) but compose.yaml runs everything in dev mode (SDK images, hot-reload, source mounts). This is fine, but it's not documented and could confuse someone (including future-you) who expects `compose up` to use the Dockerfiles.

Add a brief note in the README or a `CONTAINERS.md` explaining:
- `compose.yaml` is the dev config — mounts source, uses SDK images, enables hot-reload
- `Backend/Dockerfile` and `PythonDataService/Dockerfile` are for production builds
- How to validate the production Dockerfiles still build: `podman build -t marketscope-backend ./Backend`

### 3c. Add a .dockerignore for the Backend

The Python service has a `.dockerignore` but the Backend does not. Without one, `dotnet publish` inside the Dockerfile copies everything — including `bin/`, `obj/`, `.git/`, test projects, and IDE files — into the build context. This slows builds and can leak local state into images.

**Fix:** Create `Backend/.dockerignore`:

```
bin/
obj/
*.user
*.suo
.vs/
.vscode/
*.md
```

---

## Summary Table

| # | Requirement | Impact | Effort |
|---|------------|--------|--------|
| 1 | Harden compose.yaml (secrets, limits, healthcheck, prod profile) | High — prevents data loss, OOM kills, secret leaks | Low-Medium |
| 2 | Trim and organize Python dependencies (dedupe files, lazy-load statsmodels, swap pandas-ta → pandas-ta-classic) | Medium — smaller image, faster startup, clearer dep tree | Low |
| 3 | Add container lifecycle docs and .dockerignore | Medium — reduces onboarding friction, prevents silent Dockerfile rot | Low |
