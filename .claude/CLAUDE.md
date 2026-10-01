# Development

## Quick Start

```bash
./restart.sh                 # Rebuild all containers
./restart.sh --no-cache      # Full rebuild from scratch
```

## Services

| Service  | URL                          | Container            | Logs                                |
|----------|------------------------------|----------------------|-------------------------------------|
| Frontend | http://localhost:4200        | my-frontend          | `podman logs -f my-frontend`        |
| Backend  | http://localhost:5000        | my-backend           | `podman logs -f my-backend`         |
| GraphQL  | http://localhost:5000/graphql| my-backend           |                                     |
| Python   | http://localhost:8000        | polygon-data-service | `podman logs -f polygon-data-service`|
| Postgres | localhost:5432               | my-postgres          | `podman logs -f my-postgres`        |

## Running tests

Run the test that proves your change; CI runs the rest.

```bash
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/<path>::<test>   # Python, host venv
cd Frontend && npx ng test --include='src/app/<path>/<name>.spec.ts'                                   # Frontend, host, one exact spec file
# .NET: no dotnet on the owner's Mac — CI runs Backend.Tests and `dotnet format`
```

## Linting

```bash
ruff check PythonDataService/app/ PythonDataService/tests/   # Python
cd Frontend && npx eslint src/ && npx eslint tests/e2e --max-warnings 0   # Frontend (what CI runs)
```

## Container Management

```bash
podman compose ps                                    # Status
podman compose up -d                                 # Start all
podman compose down                                  # Stop all
podman exec -it my-postgres psql -U postgres         # DB shell
```
