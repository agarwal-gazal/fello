# One image for the web app and the weekly job. Build context is the repository root:  docker build -f deploy/Dockerfile .
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY deploy/requirements.txt deploy/requirements.txt
RUN pip install --no-cache-dir -r deploy/requirements.txt
COPY . .
RUN useradd --system --uid 10001 --home /app app && chown -R app /app
USER app
ENV PYTHONUNBUFFERED=1 DB_DRIVER=psycopg HOST=0.0.0.0 PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'))"
CMD ["python", "-m", "ui.server"]
