FROM node:20-slim AS frontend
WORKDIR /build
COPY app/frontend/package.json app/frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY app/frontend ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*
COPY deploy/app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY maritime_tip_cue /app/maritime_tip_cue
COPY app/__init__.py /app/app/__init__.py
COPY app/backend /app/app/backend
COPY config.yaml /app/config.yaml
COPY rules /app/rules
COPY --from=frontend /build/dist /app/app/frontend/dist
RUN mkdir -p /app/data/runs /app/reviews && chown -R 1000:1000 /app/data /app/reviews
ARG GIT_COMMIT=
ENV STS_GIT_COMMIT=${GIT_COMMIT}
ENV STS_CONFIG_PATH=/app/config.yaml
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('STS_APP_PORT', '8765') + '/', timeout=4)"
CMD ["python", "-m", "app.backend.server"]
