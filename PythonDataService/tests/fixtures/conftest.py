# Intentionally minimal — prevents pytest from loading the parent
# tests/conftest.py (which imports the full FastAPI app) when only
# running fixture validation tests that have no app dependency.
