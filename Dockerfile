# FINN live-research API, for container hosts (Google Cloud Run, Koyeb, Fly). GitHub Pages serves the app.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    FINN_CACHE_DIR=/tmp/finn-cache \
    FINN_LIVE_ANNUAL_REPORT=0 \
    FINN_LIVE_TRANSCRIPTS=4

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY finn ./finn
COPY web ./web
COPY portfolio.json ./

RUN useradd --create-home --uid 10001 finn && chown -R finn:finn /app
USER finn

EXPOSE 8080
# GEMINI_API_KEY and FINN_ALLOWED_ORIGINS are injected by the host's environment settings.
CMD ["sh", "-c", "uvicorn finn.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
