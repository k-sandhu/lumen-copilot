FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY lumen_sandbox_runner /app/lumen_sandbox_runner
RUN pip install --no-cache-dir .
RUN mkdir -p /workspace

EXPOSE 8000
CMD ["uvicorn", "lumen_sandbox_runner.main:app", "--host", "0.0.0.0", "--port", "8000"]
