FROM python:3.12-slim AS builder

WORKDIR /build

# Use a real venv — eliminates pip warnings
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip and ensure setuptools is present (polygon-api-client needs pkg_resources).
# Pinned exactly: an unpinned upgrade moves the installed pip on every
# cache-skipping rebuild, which moves the sweep environment digest and
# refuses Finish on every interrupted study (#2604). The versions match the
# image already in service, so pinning itself moves nothing.
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

# --- One-shot qualification stage ---
# Keep pytest and developer tooling out of the always-on data-plane image. This
# target is selected only by the Compose ``qualification`` profile, where the
# test tree and authenticated fixture are mounted read-only.
#
# This is the LAST stage, which is what a build that names no ``--target``
# produces. Every Compose service built from this context therefore names its
# ``target:`` explicitly -- ``runtime`` for everything always-on -- and
# tests/contracts/test_data_plane_image_stage.py fails the contract test for a
# service that forgets (#2576).
FROM runtime AS qualification

# requirements-dev.txt pulls in the heavy and light pins with ``-r``, so all
# three go in together; the runtime stage ships none of them (nothing there
# reads a requirement file). tests/contracts/test_data_plane_image_stage.py
# pins this COPY to the ``-r`` lines of requirements-dev.txt.
COPY requirements-heavy.txt requirements-light.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
# Pytest and ruff config: pytest needs ``asyncio_mode = auto`` from its file, or
# it defaults to strict mode and any un-decorated async test errors out at
# collection; ruff needs its file to run under the same ruleset as CI. Only
# this stage is used to run either, so only this stage ships their config.
COPY ./pytest.ini ./pytest.ini
COPY ./ruff.toml ./ruff.toml
RUN mkdir -p \
    /app/contracts \
    /app/evidence \
    /app/artifacts/alpaca_clerk/qualification \
    /app/production-alpaca-clerk
