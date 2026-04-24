FROM python:3.11-slim

# System deps: we only need sqlite3 for the CLI and the stdlib.
RUN apt-get update \
  && apt-get install -y --no-install-recommends sqlite3 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Mutable state goes to /state (mount a volume) so image is immutable.
RUN mkdir -p /state /logs
ENV PYTHONUNBUFFERED=1

# Observability server
EXPOSE 9090

ENTRYPOINT ["python", "-m", "bot.main"]
CMD ["--config", "/app/bot/config.yaml"]
