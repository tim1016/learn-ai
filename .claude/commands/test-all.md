Run all three test suites and report results.

1. **Frontend** (Vitest — runs in container):
   ```bash
   podman exec my-frontend npx ng test
   ```

2. **Backend** (.NET xUnit — runs locally, needs DB + Python containers):
   ```bash
   cd Backend.Tests && dotnet test
   ```

3. **Python** (pytest — runs in container):
   ```bash
   podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
   ```

Run all three. Report a summary table: suite name, pass/fail count, any failures.
If a container is not running, note it and skip that suite.
