# Build stage: Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Build stage: Backend. requirements.txt pins every version, and its index line
# brings the CPU build of torch.
FROM python:3.11-slim AS backend-builder
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim
WORKDIR /app

# Install only runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The server runs as this account, never as root.
RUN useradd --create-home --uid 10001 app

# Copy backend from builder
COPY --from=backend-builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY backend/ ./backend/

# Copy built frontend from frontend builder
COPY --from=frontend-builder /app/dist/ ./frontend/dist/

# Price history is mounted here (see docker-compose.yml); the price sync writes to it.
RUN mkdir -p /app/data_archive/parquet_storage && chown -R app:app /app/data_archive

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PORT=8000

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker only: see backend/run.py.
CMD ["python", "backend/run.py"]
