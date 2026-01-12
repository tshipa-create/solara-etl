FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache \
    curl \
    gcc \
    musl-dev \
    postgresql-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV AWS_REGION=af-south-1 \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
