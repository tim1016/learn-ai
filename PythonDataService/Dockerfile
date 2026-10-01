FROM python:3.12-slim AS builder

WORKDIR /build

# Use a real venv — eliminates pip warnings
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip and ensure setuptools is present (polygon-api-client needs pkg_resources).
# Pinned exactly: an unpinned upgrade moves the installed pip on every
# cache-skipping rebuild, which moves the sweep environment digest and
# refuses Finish on every interrupted study (#2604). The versions match the
# image already in service. The range pins in requirements-light.txt still
# re-resolve on a rebuild that skips the cache, so finish interrupted studies
# before rebuilding.
RUN pip install --upgrade "pip==26.2.1" "setuptools==80.10.2"

# Layer 1: Heavy/stable libs (scipy, numpy, pandas, statsmodels)
# Pre-built binary wheels only — no source compilation needed.
# This layer is cached and only rebuilt when requirements-heavy.txt changes.
COPY requirements-heavy.txt .
RUN pip install --only-binary :all: -r requirements-heavy.txt

# Layer 2: Light/frequently-changing libs
COPY requirements-light.txt .
RUN pip install -r requirements-light.txt

# --- Runtime stage ---
FROM python:3.12-slim AS runtime

ARG GIT_SHA=unknown
ENV GIT_SHA="${GIT_SHA}"

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
COPY ./scripts ./scripts

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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
