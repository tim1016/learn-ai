FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools for native extensions (numpy, scipy, etc.)
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Use a real venv — eliminates pip warnings
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Layer 1: Heavy/stable libs (scipy, scikit-learn, etc.)
# This layer is cached and only rebuilt when requirements-heavy.txt changes.
COPY requirements-heavy.txt .
RUN pip install --no-cache-dir -r requirements-heavy.txt

# Layer 2: Light/frequently-changing libs
COPY requirements-light.txt .
RUN pip install --no-cache-dir -r requirements-light.txt

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

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
