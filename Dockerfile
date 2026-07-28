# ---- stage 1: build the React SPA into ui/dist -----------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci
COPY ui/ ./
RUN npm run build            # → /ui/dist

# ---- stage 2: python runtime ----------------------------------------------
FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Microsoft ODBC Driver 18 — required by the mssql+aioodbc SQLAlchemy driver
# used to reach Azure SQL Database (PPN_DATABASE_URL).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates apt-transport-https unixodbc-dev \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Copy the whole repo. settings.py resolves ROOT = parents[2] of the package,
# so the app MUST run from the source tree — hence the editable install below.
COPY . /app
# Bring in the built SPA where app.py expects it (ROOT/ui/dist).
COPY --from=ui /ui/dist /app/ui/dist

# ".[server]" pulls fastapi/uvicorn/sqlalchemy/aiosqlite; asyncpg is the Postgres
# driver and is deliberately NOT in pyproject (see the optional follow-up).
RUN pip install -e ".[server]" aioodbc

# Persisted at runtime via an Azure Files mount; create so first boot succeeds.
RUN mkdir -p /data/drafts /data/research /data/topics /app/.ppn_state \
 && useradd -m app && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["ppn", "serve", "--host", "0.0.0.0", "--port", "8000"]
