FROM python:3.12-slim AS builder

WORKDIR /build

# Use a real venv — eliminates pip warnings
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip and ensure setuptools is present (polygon-api-client needs pkg_resources)
RUN pip install --upgrade pip "setuptools>=70,<81"

# Layer 1: Heavy/stable libs (scipy, numpy, pandas, statsmodels)
# Pre-built binary wheels only — no source compilation needed.
# This layer is cached and only rebuilt when requirements-heavy.txt changes.
COPY requirements-heavy.txt .
RUN pip install --only-binary :all: -r requirements-heavy.txt

# Layer 2: Light/frequently-changing libs
COPY requirements-light.txt .
RUN pip install -r requirements-light.txt

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

# Copy application code
COPY ./app ./app

# Pytest config — the runtime image does not ship pytest, but operators
# install it ad-hoc into the running container (`podman exec ... pytest
# tests/`) and pytest needs ``asyncio_mode = auto`` from this file.
# Without it, pytest defaults to strict mode and any un-decorated async
# test errors out at collection.
COPY ./pytest.ini ./pytest.ini

# Golden-fixture manifest — read at runtime by ``GET /api/golden-fixtures``
# (see app/routers/golden_fixtures.py). Lives under tests/ for historical
# reasons (the fixtures themselves are test inputs) but the catalog endpoint
# is a production read path, so the file must be in the runtime image. The
# rest of tests/ is intentionally excluded — test data is large and not
# needed at runtime.
COPY ./tests/fixtures/golden/manifest.json ./tests/fixtures/golden/manifest.json

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
