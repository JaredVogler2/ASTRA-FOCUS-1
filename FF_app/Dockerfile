# FF_app — Tanzu-ready container image (12-factor: config via env, logs to stdout)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user (platform requirement); app state on ephemeral disk is demo-only.
RUN useradd --create-home --uid 10001 ffapp \
    && chown -R ffapp:ffapp /app
USER ffapp

EXPOSE 8080

# Tanzu/CF injects PORT; fall back to 8080 for local docker run.
CMD gunicorn -b 0.0.0.0:${PORT:-8080} 'ff.web.app:create_app()'
