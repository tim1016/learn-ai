Run both local linters and report results.

1. **Python** (Ruff), from the repo root:
   ```bash
   ruff check PythonDataService/app/ PythonDataService/tests/
   ```

2. **Angular/TypeScript** (ESLint — what CI runs):
   ```bash
   cd Frontend && npx eslint src/ --max-warnings 0 && npx eslint tests/e2e --max-warnings 0
   ```

Run both. Report a summary: linter name, errors found, warnings.
If a tool is not installed, note it and skip. CI runs `dotnet format`; there is no `dotnet` on the owner's Mac.
