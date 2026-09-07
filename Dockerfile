FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ASTRA_STATE_DIR=/app/var
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY astra_focus/ ./astra_focus/
COPY wsgi.py gunicorn.conf.py ./
COPY scripts/ ./scripts/
COPY FF_app/ ./FF_app/
RUN useradd --create-home --uid 10001 focus && mkdir -p /app/var /app/FF_app/web_flask/data && chown -R focus:focus /app
USER focus
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/readyz',timeout=8)"
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
