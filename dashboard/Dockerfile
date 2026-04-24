# Multi-stage build: 1) compile the SPA with node, 2) ship a slim
# Python image that serves the static bundle alongside the FastAPI API.

FROM node:20-alpine AS web
WORKDIR /web
COPY dashboard/web/package.json dashboard/web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY dashboard/web/ ./
RUN npm run build


FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install dashboard deps + bot deps (we import bot.* as a library).
COPY requirements.txt ./bot-requirements.txt
COPY dashboard/requirements.txt ./dashboard-requirements.txt
RUN pip install --no-cache-dir -r ./bot-requirements.txt -r ./dashboard-requirements.txt

# Bot package is imported read-only by the dashboard for config/models/scoring.
COPY bot/ ./bot/
COPY dashboard/ ./dashboard/

# Built SPA from the web stage. Path matches DASHBOARD_STATIC_DIR default.
COPY --from=web /web/dist ./dashboard/web/dist

EXPOSE 8080

CMD ["uvicorn", "dashboard.app.main:app", "--host", "0.0.0.0", "--port", "8080"]
