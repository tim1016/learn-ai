Run all three test suites and report results.

1. **Frontend** (Vitest — runs in container):
   ```bash
   podman exec my-frontend npm test
   ```

2. **Backend** (.NET xUnit — fast suite):
   ```bash
   cd Backend.Tests && dotnet test --filter "Category!=PostgresIntegration"
   ```

3. **Python** (pytest — host venv, 120-second hard limit):
   ```bash
   cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_fast_tests
   ```

Run all three. Report a summary table: suite name, pass/fail count, any failures.
If a container is not running, note it and skip that suite.
