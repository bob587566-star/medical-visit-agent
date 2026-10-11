FROM node:22-alpine AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd --create-home appuser && mkdir /app/data && chown appuser:appuser /app/data
COPY backend/ /app/backend/
COPY knowledge/ /app/knowledge/
COPY --from=frontend /ui/dist/ /app/frontend/dist/
USER appuser
EXPOSE 8270
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8270/readyz',timeout=2)" || exit 1
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8270"]
