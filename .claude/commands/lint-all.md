Run all three linters and report results.

1. **Angular/TypeScript** (ESLint):
   ```bash
   npx eslint Frontend/src/ --max-warnings 0
   ```

2. **Python** (Ruff):
   ```bash
   ruff check PythonDataService/app/ PythonDataService/tests/
   ```

3. **.NET** (dotnet format):
   ```bash
   dotnet format podman.sln --verify-no-changes
   ```

Run all three. Report a summary: linter name, errors found, warnings.
If a tool is not installed, note it and skip.
