FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first for build cache reuse
COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root user (uid 1000)
RUN useradd --create-home --uid 1000 appuser \
 && chown -R appuser:appuser /app
USER appuser

COPY --chown=appuser:appuser . .

CMD ["python", "-u", "main.py"]
