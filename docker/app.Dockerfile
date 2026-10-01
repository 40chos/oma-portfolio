FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY oma/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY oma/ /app/

COPY docker/entrypoint-app.sh /app/docker-entrypoint-app.sh
RUN chmod +x /app/docker-entrypoint-app.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint-app.sh"]
