FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps for psycopg2 / asyncpg / PDF OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (lean set — avoids multi-GB torch downloads)
COPY requirements-docker.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --default-timeout=300 -r requirements-docker.txt

# Application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini .

EXPOSE 8000

# Tables created on startup via init_db() in app lifespan
CMD ["python", "scripts/run_uvicorn.py"]
