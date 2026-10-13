FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY backend/requirements.txt backend/requirements.lock.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/requirements.lock.txt && groupadd --gid 1000 appuser && useradd --uid 1000 --gid 1000 --create-home appuser && mkdir /app/data && chown appuser:appuser /app/data
COPY backend/ /app/backend/
COPY knowledge/ /app/knowledge/
COPY --from=frontend /ui/dist/ /app/frontend/dist/
USER appuser
EXPOSE 8270
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8270/readyz',timeout=2)" || exit 1
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8270", "--no-access-log"]
